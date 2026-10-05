"""平台原生账号健康 —— 每个平台按它**自己真实的标准机制**判定,不是通用幻觉分。

设计动因(用户 2026-07-14):健康值必须来自平台真实的结构与规则(如 Reddit 的 karma、
LinkedIn 的 SSI、YouTube 的 Community Guidelines strikes),要有对应平台的说法和机制。
机制与阈值来自 6-agent 联网调研(官方/开发者文档),详见记忆 [[platform-health-mechanisms]]。

分工:本模块只做**平台原生判定**(assess);硬信号采集在 account_intel.build_signals(脚本),
封控识别在 platform_risk(is_platform_block)。数据来源诚实分级:
  api       = 该信号从平台 API 实拉;
  dashboard = 平台有此机制但**不开放 API**(如 LinkedIn SSI / IG 违规),必须提示去后台,绝不编造;
  derived   = 平台无原生信誉机制,只能用送达/节奏等我方可观测代理(明确标注为代理)。
"""
from __future__ import annotations

GRADES = ("healthy", "watch", "at_risk", "blocked")

# 每平台的原生机制说法(term=中文说法,en=平台官方英文名,source=主数据来源,dash=后台入口)
MECHANISM: dict[str, dict] = {
    "reddit":    {"term": "Karma", "en": "Karma", "source": "api"},
    "youtube":   {"term": "频道状态", "en": "Channel Status (good standing + strikes)", "source": "api"},
    "instagram": {"term": "账户状态", "en": "Account Status", "source": "dashboard",
                  "dash": "Instagram App → 设置 → 账户 → 账户状态"},
    "facebook":  {"term": "主页质量", "en": "Page Quality", "source": "dashboard",
                  "dash": "Meta Business Suite → Account Quality"},
    "x":         {"term": "账号状态", "en": "Account restriction ladder", "source": "api"},
    "linkedin":  {"term": "SSI 社交销售指数", "en": "Social Selling Index (0-100)", "source": "dashboard",
                  "dash": "linkedin.com/sales/ssi(仅后台,无 API)"},
    "mastodon":  {"term": "审核状态", "en": "Moderation state (Suspended/Limited)", "source": "api"},
    "bluesky":   {"term": "审核标签", "en": "Moderation Labels", "source": "api"},
    "tiktok":    {"term": "账号状态", "en": "Account Status + strikes", "source": "dashboard",
                  "dash": "TikTok App → 设置 → 账号状态"},
    "pinterest": {"term": "分发状态", "en": "Account distribution standing", "source": "dashboard",
                  "dash": "Pinterest → 商务中心 → 账号状态"},
    "threads":   {"term": "账户状态", "en": "Shared IG standing + publishing quota", "source": "dashboard"},
    "telegram":  {"term": "投递状态", "en": "No reputation — delivery only", "source": "derived"},
    "discord":   {"term": "投递状态", "en": "No reputation — delivery only", "source": "derived"},
    "webhook":   {"term": "投递状态", "en": "Delivery only", "source": "derived"},
}


def mechanism(platform: str) -> dict:
    return MECHANISM.get(platform, {"term": "投递状态", "en": "Delivery only", "source": "derived"})


def _v(grade: str, headline: str, standing: str, source: str,
       breakdown: list[dict] | None = None, dashboard_hint: str = "") -> dict:
    m = MECHANISM.get("_", {})
    return {"grade": grade, "native_headline": headline, "standing": standing,
            "data_source": source, "breakdown": breakdown or [], "dashboard_hint": dashboard_hint}


def _b(factor: str, detail: str) -> dict:
    return {"factor": factor, "detail": detail}


# --------------------------------------------------------------------------- #
# 逐平台原生判定
# --------------------------------------------------------------------------- #
def _reddit(s: dict) -> dict:
    """Reddit Karma + 账号标准(is_suspended / has_verified_email / karma / age)。"""
    link = s.get("link_karma"); comment = s.get("comment_karma")
    total = s.get("total_karma")
    if total is None and (link is not None or comment is not None):
        total = (link or 0) + (comment or 0)
    verified = s.get("has_verified_email")
    age = s.get("account_age_days")
    head = f"Karma {total if total is not None else '—'}"
    if link is not None or comment is not None:
        head += f"(贴 {link or 0} · 评 {comment or 0})"
    if age is not None:
        head += f" · 账号 {age} 天"
    if s.get("is_suspended"):
        return _v("blocked", head, "已被 Reddit 封禁(suspended)", "api",
                  [_b("is_suspended", "站点级封禁,发帖/评论/投票/私信全停,需向 Reddit 申诉")])
    bd = []
    if verified is False:
        bd.append(_b("email_unverified", "邮箱未验证 —— 很多 subreddit 的 AutoModerator 会据此拒收"))
    if total is not None and total < 10:
        bd.append(_b("low_karma", f"karma {total} 偏低,难过多数 subreddit 的 karma 门槛(常见 ≥7-10)"))
    if age is not None and age < 30:
        bd.append(_b("young_account", f"账号 {age} 天,不少 subreddit 要求 ≥30 天"))
    if verified is False or (total is not None and total < 10):
        return _v("at_risk", head, "受限:未验证邮箱或 karma 过低,会被 karma/age 门槛拒收", "api", bd)
    if (total is not None and total < 100) or (age is not None and age < 30):
        return _v("watch", head, "可发但 karma/账号龄偏低,部分严格 subreddit 会拦", "api", bd)
    return _v("healthy", head, "良好:karma 与账号龄可过多数 subreddit 门槛", "api", bd)


def _youtube(s: dict) -> dict:
    """YouTube 频道状态:overall/CG/copyright good standing + 订阅数(全 API 可拉)。"""
    subs = s.get("subscribers")
    good = s.get("yt_overall_good_standing")
    cg = s.get("yt_cg_good_standing")
    cr = s.get("yt_copyright_good_standing")
    head = f"频道状态 {'良好' if good else ('异常' if good is False else '需授权 readonly')}"
    if subs is not None:
        head += f" · 订阅 {subs}"
    if good is None:
        return _v("watch", head, "频道状态需 youtube.readonly 授权才能读(当前只有 upload)", "dashboard",
                  [_b("scope", "重新授权加 youtube.readonly 即可读频道 good standing")],
                  "YouTube 工作室 → 设置 → 频道 → 功能资格")
    bd = []
    if cg is False:
        bd.append(_b("cg_strike", "社区准则违规(Community Guidelines strike)—— 7 天上传/直播限制"))
    if cr is False:
        bd.append(_b("copyright_strike", "版权警告(copyright strike)"))
    if not good:
        grade = "at_risk" if (cg is False and cr is False) else "watch"
        return _v(grade, head, "频道非良好状态:有社区准则/版权警告", "api", bd)
    return _v("healthy", head, "良好:无社区准则/版权警告", "api", bd)


def _instagram(s: dict) -> dict:
    """IG 账户状态:粉丝/内容 API 可拉;违规/触达限制仅 Meta 后台。封控由 platform_risk 处理。"""
    f = s.get("followers"); mc = s.get("media_count")
    head = f"粉丝 {f if f is not None else '—'}"
    if mc is not None:
        head += f" · 内容 {mc}"
    head += " · 账户状态需 Meta 后台确认"
    m = MECHANISM["instagram"]
    # 无 API 违规信号:以 API 可得(粉丝/发布节奏)+ 新号 warm-up 给保守判定,并诚实提示后台
    age = s.get("account_age_days")
    if age is not None and age < 14:
        return _v("watch", head, "新账号 warm-up 期,建议低频养号", "dashboard",
                  [_b("warmup", f"账号 {age} 天,IG 新号高频 API 发布易触发限制")], m["dash"])
    return _v("healthy", head, "API 指标正常;违规/触达限制请在账户状态页确认", "dashboard",
              [], m["dash"])


def _facebook(s: dict) -> dict:
    f = s.get("followers"); fan = s.get("fan_count")
    head = f"粉丝 {f if f is not None else (fan if fan is not None else '—')} · 主页质量需 Meta 后台"
    if s.get("is_published") is False:
        return _v("blocked", head, "主页未发布(unpublished),无法投放/发帖", "dashboard",
                  [_b("unpublished", "主页被下线,需在 Meta 后台恢复")], MECHANISM["facebook"]["dash"])
    return _v("healthy", head, "API 指标正常;主页质量违规请在 Account Quality 确认", "dashboard",
              [], MECHANISM["facebook"]["dash"])


def _x(s: dict) -> dict:
    """X 无统一评分;native=受限阶梯。用 public_metrics + 我方失败率给保守提示。"""
    f = s.get("followers")
    head = f"粉丝 {f if f is not None else '—'} · X 无平台评分,看受限状态"
    bd = []
    if (s.get("failed_30d") or 0) >= 2:
        bd.append(_b("recent_failures", "近期发布失败,可能进入 limited/只读受限阶梯"))
        return _v("watch", head, "近期有发布失败,留意是否被限流/只读", "api", bd)
    return _v("healthy", head, "无异常信号(X 受限状态无公开 API,仅能据行为推断)", "api", bd)


def _mastodon(s: dict) -> dict:
    f = s.get("followers")
    head = f"关注者 {f if f is not None else '—'} · 审核状态"
    if s.get("suspended"):
        return _v("blocked", head, "实例已封禁(suspended)", "api", [_b("suspended", "被实例管理员封禁")])
    if s.get("limited"):
        return _v("at_risk", head, "被限制(limited):藏在警告屏后,触达骤降", "api", [_b("limited", "实例限制")])
    return _v("healthy", head, "无 suspended/limited 审核动作", "api")


def _generic(platform: str, s: dict) -> dict:
    """无 token / 无原生信誉机制的平台:诚实给机制说明 + 我方可观测代理(送达/节奏)。"""
    m = mechanism(platform)
    dash = m.get("dash", "")
    src = m.get("source", "derived")
    posts = s.get("posts_30d", 0)
    head = f"{m['term']}"
    if src == "dashboard":
        head += "(需平台后台确认)"
    if platform == "tiktok":
        head += " · 未过审 client 仅能发私密(SELF_ONLY)"
    bd = [_b("no_api", f"{platform} 的账号标准无开放 API,以下为我方可观测代理,非平台官方评分")]
    # 代理:近期有失败/封控由上层处理;这里只据发布节奏给中性判定
    grade = "watch" if src == "dashboard" else "healthy"
    standing = ("平台有原生机制但无 API,请在后台查看" if src == "dashboard"
                else "无平台信誉机制,只看送达与节奏")
    return _v(grade, head, standing, src, bd, dash)


_DISPATCH = {
    "reddit": _reddit, "youtube": _youtube, "instagram": _instagram,
    "facebook": _facebook, "x": _x, "mastodon": _mastodon,
}


def assess(platform: str, signals: dict) -> dict:
    """平台原生健康判定。返回 {grade, native_headline, standing, data_source, breakdown, dashboard_hint,
    mechanism:{term,en,source}}。封控(frozen/platform_blocked)在 account_intel 层优先覆盖。"""
    fn = _DISPATCH.get(platform)
    out = fn(signals) if fn else _generic(platform, signals)
    m = mechanism(platform)
    out["mechanism"] = {"term": m["term"], "en": m["en"], "source": m["source"]}
    return out


# --------------------------------------------------------------------------- #
# 定位(漏斗阶段)+ 打法状态(生命周期)—— 每渠道两个独立标签(非健康分)
# 框架:Sprout Social/Walker Sands 社媒漏斗 + 常规账号生命周期。
# --------------------------------------------------------------------------- #
POSITIONING = {
    "awareness": "认知(拉新触达)",
    "consideration": "考虑(种草互动)",
    "conversion": "转化(引导行动)",
    "engagement": "互动(社群经营)",
    "advocacy": "拥护(口碑裂变)",
}
PLAY_STATE = {
    "warmup": "养号(低频建信任)",
    "growth": "增长(加量提频)",
    "maintenance": "维护(稳定节奏)",
    "paused": "暂停",
}

# 每平台的默认定位(可被渠道自定义覆盖)
_DEFAULT_POSITIONING = {
    "x": "awareness", "instagram": "consideration", "facebook": "awareness",
    "linkedin": "consideration", "reddit": "engagement", "youtube": "awareness",
    "tiktok": "awareness", "pinterest": "consideration", "threads": "engagement",
    "telegram": "conversion", "discord": "engagement", "mastodon": "engagement",
    "bluesky": "engagement",
}


def default_positioning(platform: str) -> str:
    return _DEFAULT_POSITIONING.get(platform, "awareness")


def default_play_state(account_age_days: int | None, warmup_days: int) -> str:
    if account_age_days is not None and warmup_days and account_age_days < warmup_days:
        return "warmup"
    return "growth"
