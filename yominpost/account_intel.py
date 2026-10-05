"""账号情报引擎 —— 账号的健康度与背景,连接时采集、后续定期刷新。

动因(2026-07 IG 封号事故):我们对账号本身**一无所知**,直到平台封了才发现是"新号 +
零信任 + 高频 AIGC"。本模块把账号的健康度/背景变成一等公民,存 `Channel.config
['account_profile']`,连接时即建立基线、scheduler 定期刷新、Connections 卡片展示。

**脚本 vs LLM 明确分工**(见 docs/ACCOUNT_INTELLIGENCE_ARCH.md):
- `build_signals` = **脚本**:确定性拉取平台硬信号(followers/media_count/账号年龄/发布
  节奏/风控态)。事实必须精确可复现,零 LLM。
- `score_health` = **脚本(规则)**:可解释、可回归测试的 0-100 评分 + 扣分明细。
- `assess`      = **LLM**(active_provider)+ 规则兜底:综合叙述"什么背景、什么状态、
  下一步",LLM 掉线自动降级规则版(和整套 provider 链一致,离线可跑)。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from . import db, platform_risk
from .models import Channel, PLATFORM_BY_ID

log = logging.getLogger("yominpost.account_intel")

_TIMEOUT = 15
IG_GRAPH = "https://graph.instagram.com/v25.0"
FB_GRAPH = "https://graph.facebook.com/v25.0"

GRADES = ("healthy", "watch", "at_risk", "blocked")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# ① 脚本:硬信号采集(平台 API + 本地 DB 派生)
# --------------------------------------------------------------------------- #
def _platform_metrics(channel: Channel) -> dict:
    """从平台 API 拉粉丝/内容数等硬指标。best-effort:失败返回 {'_error': ...}。
    熔断渠道**绝不联网**(任何调用都在加重风险画像)——交由上层用缓存/DB 兜底。"""
    from .connectors.oauth_conn import _decrypt_cfg, _ensure_fresh_token
    plat = channel.platform
    try:
        token = _ensure_fresh_token(channel)
        _, cfg = _decrypt_cfg(channel)
        if plat == "instagram":
            r = httpx.get(f"{IG_GRAPH}/me", params={
                "fields": "id,username,account_type,media_count,followers_count,follows_count",
                "access_token": token}, timeout=_TIMEOUT)
            d = r.json()
            if d.get("id"):
                return {"followers": d.get("followers_count"), "media_count": d.get("media_count"),
                        "follows": d.get("follows_count"), "account_type": d.get("account_type", "")}
            return {"_error": str((d.get("error") or {}).get("message", "") or f"HTTP {r.status_code}")}
        if plat == "facebook":
            page_id = cfg.get("page_id", "")
            page_token = cfg.get("page_token", "") or token
            if page_id:
                r = httpx.get(f"{FB_GRAPH}/{page_id}", params={
                    "fields": "fan_count,followers_count,name", "access_token": page_token},
                    timeout=_TIMEOUT)
                d = r.json()
                if not d.get("error"):
                    return {"followers": d.get("followers_count") or d.get("fan_count"),
                            "fan_count": d.get("fan_count"), "name": d.get("name", "")}
                return {"_error": str((d.get("error") or {}).get("message", ""))}
            return {"_error": "无 page_id"}
        if plat == "x":
            r = httpx.get("https://api.x.com/2/users/me",
                          params={"user.fields": "public_metrics,created_at"},
                          headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
            u = (r.json() or {}).get("data", {})
            pm = u.get("public_metrics", {}) or {}
            return {"followers": pm.get("followers_count"), "media_count": pm.get("tweet_count"),
                    "created_at": u.get("created_at", "")}
        if plat == "reddit":
            # Reddit Karma:identity scope → /api/v1/me(真实原生账号标准)
            r = httpx.get("https://oauth.reddit.com/api/v1/me",
                          headers={"Authorization": f"Bearer {token}", "User-Agent": "YominPost/1.0"},
                          timeout=_TIMEOUT)
            d = r.json()
            if "name" in d or "id" in d:
                return {"link_karma": d.get("link_karma"), "comment_karma": d.get("comment_karma"),
                        "total_karma": d.get("total_karma"),
                        "has_verified_email": d.get("has_verified_email"),
                        "is_suspended": bool(d.get("is_suspended")),
                        "created_at_epoch": d.get("created_utc")}
            return {"_error": str(d)[:120]}
        if plat == "youtube":
            # 频道状态(good standing)+ 订阅数。status part 需 readonly,upload scope 可能拿不到 → 诚实兜底
            r = httpx.get("https://www.googleapis.com/youtube/v3/channels",
                          params={"part": "statistics,status", "mine": "true"},
                          headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
            items = (r.json() or {}).get("items", [])
            if items:
                st = items[0].get("statistics", {}) or {}
                status = items[0].get("status", {}) or {}
                out = {"subscribers": _int(st.get("subscriberCount")),
                       "media_count": _int(st.get("videoCount"))}
                # status 各 good standing 字段(有则说明 readonly 生效)
                if "overallGoodStanding" in status:
                    out.update({"yt_overall_good_standing": status.get("overallGoodStanding"),
                                "yt_cg_good_standing": status.get("communityGuidelinesGoodStanding"),
                                "yt_copyright_good_standing": status.get("copyrightStrikesGoodStanding")})
                return out
            return {"_error": f"HTTP {r.status_code}"}
        if plat == "mastodon":
            inst = (cfg.get("instance") or "https://mastodon.social").rstrip("/")
            r = httpx.get(f"{inst}/api/v1/accounts/verify_credentials",
                          headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT)
            d = r.json()
            return {"followers": d.get("followers_count"), "media_count": d.get("statuses_count"),
                    "suspended": bool(d.get("suspended")), "limited": bool(d.get("limited"))}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"[:120]}
    return {}                                       # 该平台无硬指标接口


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _publish_stats(channel: Channel) -> dict:
    """从本地 DB 派生发布节奏:近 30 天已发数、周频、最近发布时刻、近期失败数。"""
    posts = db.list_posts(channel.workspace_id)
    posted, failed, last_dt, first_dt = [], 0, None, None
    cutoff = _now() - timedelta(days=30)
    for p in posts:
        tg = [t for t in (p.targets or []) if t.channel_id == channel.id]
        if not tg:
            continue
        st = p.status.value if hasattr(p.status, "value") else str(p.status)
        dt = _parse(p.posted_at) or _parse(p.created_at)
        if any(t.status == "posted" for t in tg):
            if dt:
                if not last_dt or dt > last_dt:
                    last_dt = dt
                if not first_dt or dt < first_dt:
                    first_dt = dt
                if dt > cutoff:
                    posted.append(dt)
        if st == "failed" and dt and dt > cutoff:
            failed += 1
    weeks = 4.3
    return {"posts_30d": len(posted), "cadence_per_week": round(len(posted) / weeks, 1),
            "failed_30d": failed,
            "last_posted_at": last_dt.isoformat(timespec="seconds") if last_dt else "",
            "first_posted_at": first_dt.isoformat(timespec="seconds") if first_dt else ""}


def build_signals(channel: Channel) -> dict:
    """**脚本层**:汇总账号硬信号。熔断渠道跳过联网,用缓存/DB。"""
    frozen = platform_risk.frozen_until(channel) is not None
    stats = _publish_stats(channel)
    # 账号"可观测年龄":连接时刻与最早发帖(含同步导入的历史帖)取更早者 —— 平台创建
    # 日期这些接口拿不到,故用可观测下界,诚实标注。新号风险以此近似判断。
    anchors = [_parse(channel.connected_at), _parse(stats.get("first_posted_at"))]
    anchors = [a for a in anchors if a]
    first_seen = min(anchors) if anchors else _now()
    age_days = max(0, (_now() - first_seen).days)

    metrics = {}
    if frozen:
        # 用上次缓存的硬指标(不联网)
        cached = ((channel.config or {}).get("account_profile") or {}).get("signals", {})
        metrics = {k: cached.get(k) for k in ("followers", "media_count", "account_type")
                   if cached.get(k) is not None}
        metrics["_note"] = "熔断中,未联网,沿用上次缓存指标"
    elif PLATFORM_BY_ID.get(channel.platform) and PLATFORM_BY_ID[channel.platform].real:
        metrics = _platform_metrics(channel)

    # X created_at / Reddit created_utc 给真实账号年龄,优先采用
    created = _parse(metrics.get("created_at")) if metrics.get("created_at") else None
    if created:
        age_days = max(age_days, (_now() - created).days)
    if metrics.get("created_at_epoch"):
        try:
            cepoch = datetime.fromtimestamp(float(metrics["created_at_epoch"]), tz=timezone.utc)
            age_days = max(age_days, (_now() - cepoch).days)
        except (TypeError, ValueError, OSError):
            pass

    risk_flags = []
    cfg = channel.config or {}
    # 平台封锁:即使没走 freeze_channel(仅 config.error 标了),也识别为封控态。
    err = str(cfg.get("error", ""))
    platform_blocked = frozen or platform_risk.is_platform_block(err)
    if frozen:
        risk_flags.append("平台风控熔断中")
    elif platform_blocked:
        risk_flags.append("平台封锁(API access blocked,需人工解封)")
    if cfg.get("error") and not platform_blocked:
        risk_flags.append("渠道有错误标记(需人工)")
    pol = platform_risk.policy_for(channel.platform)
    if age_days < pol.get("warmup_days", 0):
        risk_flags.append(f"新账号(可观测 {age_days}d < warm-up {pol['warmup_days']}d)")

    out = {
        "checked_at": _now().isoformat(timespec="seconds"),
        "followers": metrics.get("followers"),
        "media_count": metrics.get("media_count"),
        "account_type": metrics.get("account_type", ""),
        "account_age_days": age_days,
        "posts_30d": stats["posts_30d"],
        "cadence_per_week": stats["cadence_per_week"],
        "failed_30d": stats["failed_30d"],
        "last_posted_at": stats["last_posted_at"],
        "frozen": frozen,
        "platform_blocked": platform_blocked,
        "risk_flags": risk_flags,
        "metrics_error": metrics.get("_error", ""),
        "metrics_note": metrics.get("_note", ""),
    }
    # 平台原生信号透传(Reddit karma / YouTube good standing / Mastodon 审核态 …)
    for k in ("link_karma", "comment_karma", "total_karma", "has_verified_email", "is_suspended",
              "subscribers", "yt_overall_good_standing", "yt_cg_good_standing",
              "yt_copyright_good_standing", "suspended", "limited", "fan_count", "is_published"):
        if k in metrics:
            out[k] = metrics[k]
    return out


# --------------------------------------------------------------------------- #
# ② 平台原生健康判定 —— 每平台按它自己真实的机制(不是通用幻觉分)
# --------------------------------------------------------------------------- #
_GRADE_SCORE = {"healthy": 90, "watch": 65, "at_risk": 35, "blocked": 0}


def score_health(channel: Channel, signals: dict) -> dict:
    """委托 platform_health.assess 做**平台原生判定**(Reddit karma / YouTube 频道状态 / IG 账户
    状态 …)。封控(frozen/platform_blocked)在此优先覆盖为 blocked。返回统一形状:
    health_grade + native_headline(平台说法)+ standing + data_source + score_breakdown。
    health_score 仅为 rollup 排序保留的粗档映射,不作为对用户展示的"健康分"。"""
    from . import platform_health
    if signals.get("frozen") or signals.get("platform_blocked"):
        return {"health_grade": "blocked", "health_score": 0,
                "native_headline": "平台封锁(API access blocked)", "standing": "发布全停,需人工解封",
                "data_source": "api", "mechanism": platform_health.mechanism(channel.platform),
                "dashboard_hint": "",
                "score_breakdown": [{"factor": "platform_blocked",
                                     "why": "平台封锁,重连无效,须平台侧申诉解除"}]}
    v = platform_health.assess(channel.platform, signals)
    return {"health_grade": v["grade"], "health_score": _GRADE_SCORE.get(v["grade"], 60),
            "native_headline": v["native_headline"], "standing": v["standing"],
            "data_source": v["data_source"], "mechanism": v["mechanism"],
            "dashboard_hint": v.get("dashboard_hint", ""),
            "score_breakdown": [{"factor": b["factor"], "why": b["detail"]} for b in v.get("breakdown", [])]}


# --------------------------------------------------------------------------- #
# ③ LLM(+规则兜底):背景/健康叙述 + 建议动作
# --------------------------------------------------------------------------- #
_FACTOR_ACTIONS = {
    "platform_blocked": "按 docs/PLATFORM_RISK.md 走平台侧申诉,期间勿重试勿重连",
    "is_suspended": "向 Reddit 提交申诉(reddit.com/appeal),解封前勿换号硬发",
    "email_unverified": "去 Reddit 设置验证邮箱 —— 多数 subreddit 的 AutoModerator 据此拒收",
    "low_karma": "先在宽松 subreddit 真诚互动攒 karma,别一上来就发帖带链",
    "young_account": "账号太新,先养 30 天再进严格 subreddit",
    "cg_strike": "查 YouTube 工作室的社区准则警告,申诉或整改后等限制到期",
    "copyright_strike": "处理版权警告(争议/撤单/等 90 天到期)",
    "warmup": "warm-up 期:每天最多 1 帖、纯人工感内容、先别带外链",
    "recent_failures": "排查近期失败原因(内容被拒/限流),先修再发",
    "unpublished": "在 Meta 后台重新发布主页",
    "suspended": "被实例封禁,联系实例管理员申诉",
    "limited": "被实例限制,减少可疑行为并申诉",
    "scope": "重新授权补齐读取 scope 才能读到平台原生状态",
    "no_api": "该平台账号状态无 API,请到平台后台查看",
}


def _rule_assessment(channel: Channel, signals: dict, score: dict) -> dict:
    """确定性叙述兜底(LLM 不可用时用):平台原生说法 + 数据表现 + 建议动作。"""
    grade = score["health_grade"]
    plat = PLATFORM_BY_ID.get(channel.platform)
    label = plat.label if plat else channel.platform
    mech = score.get("mechanism", {})
    parts = [f"{label} 渠道 @{channel.handle or '—'}"]
    if score.get("native_headline"):
        parts.append(str(score["native_headline"]))
    parts.append(f"近 30 天发 {signals.get('posts_30d',0)} 帖(周频 {signals.get('cadence_per_week',0)})")
    head = "、".join(parts) + "。"
    standing = score.get("standing") or {"healthy": "整体健康。", "watch": "需要留意。",
                                         "at_risk": "高风险,建议整改。", "blocked": "已被平台封控。"}[grade]
    src_note = ""
    if score.get("data_source") == "dashboard" and score.get("dashboard_hint"):
        src_note = f"(原生状态需后台查看:{score['dashboard_hint']})"

    acts = [_FACTOR_ACTIONS[b["factor"]] for b in score.get("score_breakdown", [])
            if b.get("factor") in _FACTOR_ACTIONS]
    if not acts:
        acts.append(f"按 {mech.get('term','平台机制')} 维持良好状态,持续监控")
    # 去重保序
    seen, uniq = set(), []
    for a in acts:
        if a not in seen:
            seen.add(a); uniq.append(a)
    return {"assessment": head + standing + src_note, "recommended_actions": uniq[:4], "engine": "rule"}


def assess(channel: Channel, signals: dict, score: dict) -> dict:
    """LLM 叙述(active_provider),失败降级规则版。LLM 只做"判断/叙述/建议",
    不产生任何硬事实(事实全部来自 build_signals)。"""
    rule = _rule_assessment(channel, signals, score)
    # 熔断/离线:直接用规则版,不烧 LLM
    if signals.get("frozen"):
        return rule
    try:
        from .services import active_provider
        prov = active_provider()
        ctx = {"platform": channel.platform, "handle": channel.handle,
               "signals": signals, "score": score,
               "instruction": ("你是社媒账号健康顾问。仅依据给定信号,用中文一段话说明这个"
                               "账号的背景与当前状态,再给 2-4 条可执行建议。不要编造信号里"
                               "没有的数字。输出 JSON:{assessment, recommended_actions:[...]}")}
        r = prov.run("account_assess", ctx) if hasattr(prov, "run") else {}
        txt = (r or {}).get("assessment") or (r or {}).get("text") or ""
        acts = (r or {}).get("recommended_actions")
        if isinstance(txt, str) and len(txt.strip()) >= 12:
            out = {"assessment": txt.strip()[:600],
                   "recommended_actions": [str(a)[:120] for a in (acts or rule["recommended_actions"])][:4],
                   "engine": getattr(prov, "name", "llm")}
            return out
    except Exception as e:  # noqa: BLE001
        log.info("account assess LLM fallback: %s", e)
    return rule


# --------------------------------------------------------------------------- #
# 编排 + 持久化
# --------------------------------------------------------------------------- #
def refresh_profile(channel: Channel, use_llm: bool = True) -> dict:
    """采集信号 → 平台原生判定 → 叙述 → 存 Channel.config['account_profile'] 并落库。"""
    from . import platform_health
    signals = build_signals(channel)
    score = score_health(channel, signals)
    narrative = assess(channel, signals, score) if use_llm else _rule_assessment(channel, signals, score)
    profile = {
        "checked_at": signals["checked_at"],
        "signals": signals,
        **score,
        "assessment": narrative["assessment"],
        "recommended_actions": narrative["recommended_actions"],
        "engine": {"signals": "script", "score": "platform-native", "assessment": narrative.get("engine", "rule")},
    }
    channel.config = dict(channel.config or {})
    channel.config["account_profile"] = profile
    # 定位 + 打法状态:首次给平台默认,已有则保留(用户可改)
    pol = platform_risk.policy_for(channel.platform)
    channel.config.setdefault("positioning", platform_health.default_positioning(channel.platform))
    channel.config.setdefault("play_state",
                              platform_health.default_play_state(signals.get("account_age_days"),
                                                                 pol.get("warmup_days", 0)))
    db.save_channel(channel)
    log.info("account profile refreshed %s/%s grade=%s mech=%s",
             channel.platform, channel.id, profile["health_grade"], (score.get("mechanism") or {}).get("term"))
    return profile


def get_profile(channel: Channel) -> dict | None:
    return (channel.config or {}).get("account_profile")


def refresh_all(workspace_id: str | None = None, use_llm: bool = False) -> dict:
    """定时刷新入口:遍历渠道刷新 profile。**不限于 real 平台**——健康/熔断/发布节奏对
    任何渠道都适用(mock 平台没有粉丝等硬指标,build_signals 自然只给 DB 派生信号)。
    熔断渠道也刷但走缓存/DB 不联网。默认 use_llm=False(批量不烧 LLM,叙述用规则版)。"""
    n = graded = 0
    wss = [workspace_id] if workspace_id else [w.id for w in db.list_workspaces()]
    for ws in wss:
        for ch in db.list_channels(ws):
            try:
                refresh_profile(ch, use_llm=use_llm)
                n += 1
                if ((ch.config or {}).get("account_profile") or {}).get("health_grade") in ("at_risk", "blocked"):
                    graded += 1
            except Exception:  # noqa: BLE001
                log.exception("refresh_profile failed %s", ch.id)
    return {"refreshed": n, "at_risk_or_blocked": graded}
