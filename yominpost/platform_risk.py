"""平台风控层 —— AIGC 发布的系统性风险防护。

背景(2026-07-14 实锤教训):IG 新账号在开号第 1 天就走 API 发 AIGC 营销内容,
30 小时 3 帖、8 个 hashtag、每帖同域外链、诱导互动 CTA("Comment or DM …"),
且第 1 帖把 LLM prompt 原文当 caption 发了出去 —— 数日内被 Meta 全端点
"API access blocked."(重新授权无效,只能 Meta 侧解封)。

三道防线:
  1. **发布前风险门** pre_publish_issues():内容 lint(prompt 泄漏/诱导互动/
     hashtag/链接堆叠)+ 节奏(24h 上限/最小间隔/新渠道 warm-up)+ 与近期已发
     内容的重复度。block 级问题直接拦下不发;warn 级放行但记录。
  2. **平台封锁分类器** is_platform_block():发布错误命中平台风控特征。
     与鉴权错误(is_auth_error,重连可解)本质不同:风控封锁重连无效。
  3. **熔断** freeze_channel():config.risk_frozen_until,冻结期内发布/健康
     探针一律跳过 —— 对已被标记的账号继续调 API 只会加重自动化画像,
     唯一正确动作是停手 + 全局通知人工介入。

策略数值集中在 POLICY,按平台覆盖 _default;数值来源:各平台官方 API 限额 +
成熟排期工具(Buffer/Later/Postiz)的保守实践,可按研究结论继续收紧/放宽。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from . import db
from .models import Channel, ChannelStatus, Post

log = logging.getLogger("yominpost.platform_risk")


def gate_enabled() -> bool:
    """软风控门(内容 lint / 节奏 / 重复度)总开关。生产默认开;测试(conftest)关,
    避免共享渠道跨用例累积触发节奏上限、污染无关发布测试。硬安全停(熔断 + 平台封锁
    分类)不受此开关影响,始终生效。"""
    return os.environ.get("YOMINPOST_RISK_GATE", "1") != "0"

# --------------------------------------------------------------------------- #
# 策略表(hard 数值;None = 该维度不限)
# --------------------------------------------------------------------------- #
POLICY: dict[str, dict] = {
    # 保守默认:适用于未单列的平台
    "_default": {"max_per_day": 4, "min_interval_min": 45, "hashtag_max": 8,
                 "warmup_days": 7, "warmup_max_per_day": 1},
    # Meta 系:自动化/spam 分类器最激进,新账号必须爬坡
    "instagram": {"max_per_day": 2, "min_interval_min": 180, "hashtag_max": 5,
                  "warmup_days": 14, "warmup_max_per_day": 1},
    "facebook": {"max_per_day": 4, "min_interval_min": 60, "hashtag_max": 5,
                 "warmup_days": 7, "warmup_max_per_day": 2},
    "threads": {"max_per_day": 4, "min_interval_min": 60, "hashtag_max": 3,
                "warmup_days": 7, "warmup_max_per_day": 2},
    # X free tier 写额度极低(~17/24h/user),且对营销外链敏感
    "x": {"max_per_day": 6, "min_interval_min": 30, "hashtag_max": 3,
          "warmup_days": 5, "warmup_max_per_day": 2},
    "tiktok": {"max_per_day": 3, "min_interval_min": 120, "hashtag_max": 5,
               "warmup_days": 7, "warmup_max_per_day": 1},
    "pinterest": {"max_per_day": 6, "min_interval_min": 30, "hashtag_max": 6,
                  "warmup_days": 5, "warmup_max_per_day": 2},
    "linkedin": {"max_per_day": 3, "min_interval_min": 120, "hashtag_max": 5,
                 "warmup_days": 5, "warmup_max_per_day": 1},
    "reddit": {"max_per_day": 2, "min_interval_min": 240, "hashtag_max": 0,
               "warmup_days": 14, "warmup_max_per_day": 1},
    "youtube": {"max_per_day": 2, "min_interval_min": 240, "hashtag_max": 8,
                "warmup_days": 7, "warmup_max_per_day": 1},
    # 自有/联邦渠道:平台风控弱,只防节奏失控
    "mastodon": {"max_per_day": 10, "min_interval_min": 10, "hashtag_max": 8,
                 "warmup_days": 0, "warmup_max_per_day": 0},
    "bluesky": {"max_per_day": 10, "min_interval_min": 10, "hashtag_max": 8,
                "warmup_days": 0, "warmup_max_per_day": 0},
    "telegram": {"max_per_day": 20, "min_interval_min": 3, "hashtag_max": 10,
                 "warmup_days": 0, "warmup_max_per_day": 0},
    "discord": {"max_per_day": 20, "min_interval_min": 3, "hashtag_max": 10,
                "warmup_days": 0, "warmup_max_per_day": 0},
}

FREEZE_HOURS_DEFAULT = 72          # 熔断默认冻结时长(平台侧解封普遍 24-72h)
_DUP_BLOCK, _DUP_WARN = 0.85, 0.6  # 与近期已发文重复度阈值(token Jaccard)
_RECENT_N = 5                      # 重复度/同域链比对的近期帖数

# 平台「发布能力」硬约束(与内容无关的静态告警,预检时提示一次)。数据源见
# docs/PLATFORM_RISK.md:各平台官方文档 2026-07 核实。
PLATFORM_CAVEATS: dict[str, str] = {
    "tiktok": ("TikTok 内容发布 API:未通过官方审核的应用只能发 SELF_ONLY(私密)且 "
               "24h 最多 5 个用户 —— 公开发布需先过 TikTok client audit,否则帖子仅自己可见"),
    "x": ("X API v2 免费层写额度极低(约 17 帖/24h/用户,2026-02 起新开发者无免费额度)—— "
          "把 X 当稀缺渠道用,别刷量"),
    "instagram": ("Instagram 官方上限 100 帖/24h,但新账号实际远低于此;可用 "
                  "GET /{ig-user-id}/content_publishing_limit 查真实剩余额度"),
    "reddit": "Reddit 社区对自我推广极敏感,同一链接跨 subreddit 连发易被版主/反垃圾封禁",
}


def policy_for(platform: str) -> dict:
    p = dict(POLICY["_default"])
    p.update(POLICY.get(platform, {}))
    return p


# --------------------------------------------------------------------------- #
# 内容 lint
# --------------------------------------------------------------------------- #
# prompt 泄漏:生成指令/脚手架残留出现在成文里(IG 首帖事故的直接特征)
_PROMPT_LEAK = re.compile(
    r"写一条|写一篇|帮我写|请生成|请以|以下是|as an ai|as a language model"
    r"|\[标题\]|\[正文\]|\[hook\]|\[body\]|\[cta\]|here is a post|here's a post"
    r"|^\s*[·•]\s*为.{0,40}(写|生成)", re.I | re.M)
# 诱导互动(Meta engagement-bait 政策明令;IG 事故帖 CTA 的直接特征)
_ENGAGEMENT_BAIT = re.compile(
    r"comment\b.{0,60}\b(dm|send|get|receive)|dm\b.{0,30}(me|\"|“)"
    r"|tag\s+\d+|tag\s+(a|your)\s+friend|like\s*(&|and)\s*share.{0,20}(win|get)"
    r"|评论.{0,12}(送|领|获得|私信)|私信.{0,8}(领|送)|点赞.{0,10}(抽奖|送)"
    r"|转发.{0,10}(抽奖|送)|关注.{0,8}(抽奖|送)", re.I)
_URL = re.compile(r"https?://([^\s/]+)", re.I)


def _issue(level: str, code: str, msg: str) -> dict:
    return {"level": level, "code": code, "msg": msg}


def lint_content(platform: str, post: Post) -> list[dict]:
    """纯内容检查(不查库):prompt 泄漏 / 诱导互动 / hashtag / 链接堆叠。"""
    pol = policy_for(platform)
    c = post.content
    text = "\n".join(x for x in (c.hook, c.body, c.cta) if x)
    issues: list[dict] = []
    m = _PROMPT_LEAK.search(text)
    if m:
        issues.append(_issue("block", "prompt_leak",
                             f"疑似 AI 生成指令残留在成文中(「{m.group(0)[:24]}…」)——"
                             "发出去=向平台自证批量 AIGC,先修文案"))
    m = _ENGAGEMENT_BAIT.search(text)
    if m:
        lvl = "block" if platform in ("instagram", "facebook", "threads") else "warn"
        issues.append(_issue(lvl, "engagement_bait",
                             f"诱导互动话术(「{m.group(0)[:30]}…」)——Meta 系平台明令打击,"
                             "会直接压低分发并累积账号风险画像"))
    tags = [t for t in (c.hashtags or []) if t]
    if pol["hashtag_max"] is not None and len(tags) > pol["hashtag_max"]:
        issues.append(_issue("warn", "hashtag_over",
                             f"hashtag {len(tags)} 个,超过 {platform} 建议上限 "
                             f"{pol['hashtag_max']}(堆标签是 spam 分类器的强信号)"))
    urls = _URL.findall(text)
    if len(urls) > 1:
        issues.append(_issue("warn", "multi_link", f"正文含 {len(urls)} 条链接,建议 ≤1"))
    return issues


# --------------------------------------------------------------------------- #
# 节奏 + 重复度(查库)
# --------------------------------------------------------------------------- #
def _recent_posted(channel: Channel) -> list[Post]:
    """该渠道最近已发的帖(新→旧),用于节奏与重复度。"""
    out = []
    for p in db.list_posts(channel.workspace_id):
        if p.status.value != "posted" or not p.posted_at:
            continue
        if any(t.channel_id == channel.id and t.status == "posted" for t in (p.targets or [])):
            out.append(p)
    out.sort(key=lambda p: str(p.posted_at), reverse=True)
    return out


def _parse_ts(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def check_cadence(channel: Channel, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    pol = policy_for(channel.platform)
    recent = _recent_posted(channel)
    issues: list[dict] = []
    # 24h 上限(warm-up 期用更严的爬坡上限)
    day_ago = now - timedelta(hours=24)
    today = [p for p in recent if (_parse_ts(p.posted_at) or day_ago) > day_ago]
    cap = pol["max_per_day"]
    connected = _parse_ts(channel.connected_at)
    if (pol["warmup_days"] and connected
            and now - connected < timedelta(days=pol["warmup_days"])):
        cap = min(cap or 999, pol["warmup_max_per_day"] or cap)
        if len(today) >= cap:
            issues.append(_issue("block", "warmup_cap",
                                 f"渠道接入未满 {pol['warmup_days']} 天(warm-up 期),"
                                 f"24h 内已发 {len(today)} 条,爬坡上限 {cap} 条/天——"
                                 "新账号第一周高频 API 发布是封号的头号诱因"))
            return issues
    if cap is not None and len(today) >= cap:
        issues.append(_issue("block", "daily_cap",
                             f"24h 内该渠道已发 {len(today)} 条,达 {channel.platform} "
                             f"安全上限 {cap} 条/天"))
        return issues
    # 最小间隔
    if recent and pol["min_interval_min"]:
        last = _parse_ts(recent[0].posted_at)
        if last:
            gap = (now - last).total_seconds() / 60
            if gap < pol["min_interval_min"]:
                issues.append(_issue("block", "min_interval",
                                     f"距上一条仅 {int(gap)} 分钟,{channel.platform} "
                                     f"最小安全间隔 {pol['min_interval_min']} 分钟"
                                     "(短时连发≈机器行为特征)"))
    return issues


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}|[一-鿿]{2,}", (text or "").lower()))


def check_duplicate(channel: Channel, post: Post) -> list[dict]:
    """与该渠道近期已发内容的重复度 + 同域外链连发。"""
    mine = _tokens(post.content.as_text())
    if not mine:
        return []
    issues: list[dict] = []
    recent = _recent_posted(channel)[:_RECENT_N]
    best = 0.0
    for p in recent:
        theirs = _tokens(p.content.as_text())
        if not theirs:
            continue
        j = len(mine & theirs) / len(mine | theirs)
        best = max(best, j)
    if best >= _DUP_BLOCK:
        issues.append(_issue("block", "duplicate",
                             f"与近期已发内容重复度 {best:.0%}——同文重发是最典型的 spam 信号"))
    elif best >= _DUP_WARN:
        issues.append(_issue("warn", "near_duplicate",
                             f"与近期已发内容相似度 {best:.0%},建议改写角度/结构"))
    # 同域外链连发:近 N 帖 + 本帖都指向同一域名
    dom = {d.lower() for d in _URL.findall(post.content.as_text())}
    if dom:
        streak = sum(1 for p in recent
                     if dom & {d.lower() for d in _URL.findall(p.content.as_text())})
        if streak >= 2:
            issues.append(_issue("warn", "link_streak",
                                 f"近 {len(recent)} 帖中已有 {streak} 帖带相同域名外链,"
                                 "每帖同域导流是营销 spam 画像的核心特征"))
    return issues


def pre_publish_issues(channel: Channel, post: Post,
                       now: datetime | None = None) -> list[dict]:
    """发布前风险门(单渠道):熔断态 > 内容 lint > 节奏 > 重复度。"""
    frozen = frozen_until(channel, now)
    if frozen:
        return [_issue("block", "frozen",
                       f"渠道被平台风控熔断,冻结至 {frozen:%m-%d %H:%M} UTC——"
                       "继续调 API 会加重账号风险画像,请先人工处理(见 Connections)")]
    issues = lint_content(channel.platform, post)
    issues += check_cadence(channel, now)
    issues += check_duplicate(channel, post)
    caveat = PLATFORM_CAVEATS.get(channel.platform)
    if caveat:                                     # 平台发布能力硬约束(信息级提醒)
        issues.append(_issue("warn", "platform_caveat", caveat))
    return issues


# --------------------------------------------------------------------------- #
# 平台封锁分类器 + 熔断
# --------------------------------------------------------------------------- #
# 风控/封禁类错误特征:与鉴权(token 过期,重连可解)不同,这类重试/重连均无效
_PLATFORM_BLOCK_HINTS = (
    "api access blocked", "action blocked", "temporarily blocked", "account is restricted",
    "restricted from", "flagged for", "unusual activity", "spam", "policy violation",
    "violates our", "suspended", "feature is temporarily", "we restrict certain activity",
    "reduced distribution", "advertising access", "checkpoint_required",
    "此功能已被暂停", "操作过于频繁", "违反社区", "账号被限制", "疑似异常",
)


def is_platform_block(err: str | None) -> bool:
    e = (err or "").lower()
    return any(h in e for h in _PLATFORM_BLOCK_HINTS)


def frozen_until(channel: Channel, now: datetime | None = None) -> datetime | None:
    ts = _parse_ts((channel.config or {}).get("risk_frozen_until"))
    if not ts:
        return None
    now = now or datetime.now(timezone.utc)
    return ts if ts > now else None


def freeze_channel(channel: Channel, err: str, hours: int = FREEZE_HOURS_DEFAULT) -> None:
    """熔断:冻结渠道 + 挂全局通知(config.error 即通知数据源)。不翻 status。"""
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    channel.config = dict(channel.config or {})
    channel.config["risk_frozen_until"] = until.isoformat(timespec="seconds")
    channel.config["error"] = (f"⛔ 平台风控封锁,已熔断 {hours}h(勿重试/勿重连,"
                               f"重复调用会加重风险画像):{(err or '')[:110]}")
    db.save_channel(channel)
    log.warning("platform-risk FREEZE %s/%s until %s: %s",
                channel.platform, channel.id, until.isoformat(), (err or "")[:160])
