"""运营状态聚合 —— "我的每个渠道现在到底怎么样"的单屏答案(只读)。

把四层(身份/情报/发布/广告)在账号维度合并:渠道状态 + 健康画像 + 发布统计 +
数据表现 + 有无广告。纯聚合,不产生新事实(健康来自 account_intel、发布来自 posts、
广告来自 ads_link),缺的数据诚实省略,绝不假造。
"""
from __future__ import annotations

from . import account_intel, ads_link, db, platform_risk
from .models import ChannelStatus, PLATFORM_BY_ID, Workspace


def _publish_rollup(ws_id: str, channel_id: str) -> dict:
    """该渠道的发布计数 + 数据表现(来自已同步帖的 meta)。"""
    counts = {"posted": 0, "scheduled": 0, "failed": 0, "draft": 0, "publishing": 0}
    perf = {"likes": 0, "comments": 0, "impressions": 0}
    has_perf = False
    last_posted = ""
    for p in db.list_posts(ws_id):
        tg = [t for t in (p.targets or []) if t.channel_id == channel_id]
        if not tg:
            continue
        st = p.status.value if hasattr(p.status, "value") else str(p.status)
        counts[st] = counts.get(st, 0) + 1
        if any(t.status == "posted" for t in tg):
            pa = p.posted_at or p.created_at
            if pa and pa > last_posted:
                last_posted = pa
        m = p.meta or {}
        for k in ("likes", "comments", "impressions"):
            v = m.get(k) or m.get(f"platform_{k}")
            if isinstance(v, (int, float)):
                perf[k] += int(v)
                has_perf = True
    out = {"counts": counts, "last_posted_at": last_posted}
    if has_perf:
        out["performance"] = perf
    return out


def channel_operations(ws_id: str, channel) -> dict:
    """单渠道运营视图。"""
    meta = PLATFORM_BY_ID.get(channel.platform)
    prof = account_intel.get_profile(channel) or {}
    pub = _publish_rollup(ws_id, channel.id)
    alerts = []
    if (channel.config or {}).get("error"):
        alerts.append(str(channel.config["error"])[:160])
    if platform_risk.frozen_until(channel):
        alerts.append("平台风控熔断中(勿重试/重连)")
    ad = ads_link.summary(channel)
    return {
        "channel_id": channel.id,
        "platform": channel.platform,
        "label": meta.label if meta else channel.platform,
        "handle": channel.handle,
        "status": channel.status.value if hasattr(channel.status, "value") else str(channel.status),
        "health": {"grade": prof.get("health_grade"),
                   "checked_at": prof.get("checked_at"),
                   # 平台原生说法(Reddit karma / YouTube 频道状态 / IG 账户状态 …)
                   "native_headline": prof.get("native_headline", ""),
                   "standing": prof.get("standing", ""),
                   "mechanism": prof.get("mechanism", {}),
                   "data_source": prof.get("data_source", ""),
                   "dashboard_hint": prof.get("dashboard_hint", ""),
                   "breakdown": prof.get("score_breakdown", []),
                   "assessment": prof.get("assessment", ""),
                   "actions": prof.get("recommended_actions", []),
                   "followers": (prof.get("signals") or {}).get("followers"),
                   "media_count": (prof.get("signals") or {}).get("media_count"),
                   "account_age_days": (prof.get("signals") or {}).get("account_age_days"),
                   "cadence_per_week": (prof.get("signals") or {}).get("cadence_per_week")},
        "positioning": (channel.config or {}).get("positioning", ""),
        "play_state": (channel.config or {}).get("play_state", ""),
        "publish": pub,
        "ads": ad,
        "alerts": alerts,
    }


def workspace_operations(ws: Workspace) -> dict:
    """workspace 全渠道运营总览 + rollup。"""
    chans = db.list_channels(ws.id)
    rows = [channel_operations(ws.id, c) for c in chans]
    grade_of = lambda r: (r["health"] or {}).get("grade")
    rollup = {
        "channels": len(rows),
        "connected": sum(1 for c in chans if c.status == ChannelStatus.connected),
        "healthy": sum(1 for r in rows if grade_of(r) == "healthy"),
        "watch": sum(1 for r in rows if grade_of(r) == "watch"),
        "at_risk": sum(1 for r in rows if grade_of(r) == "at_risk"),
        "blocked": sum(1 for r in rows if grade_of(r) == "blocked"),
        "ad_linked": sum(1 for r in rows if (r["ads"] or {}).get("linked")),
        "total_posted_30d": sum((r["publish"]["counts"].get("posted", 0)) for r in rows),
    }
    # 需要关注的排前面(blocked > at_risk > watch > healthy > 无画像)
    order = {"blocked": 0, "at_risk": 1, "watch": 2, "healthy": 3, None: 4}
    rows.sort(key=lambda r: order.get(grade_of(r), 4))
    from . import services
    return {"rollup": rollup, "channels": rows, "synced_at": services.last_channel_sync_at()}
