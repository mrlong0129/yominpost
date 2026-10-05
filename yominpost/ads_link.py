"""广告账户链接 —— 把社媒内容账号接到它的广告账户,打通 AIGC → GTM。

设计(见 docs/ACCOUNT_INTELLIGENCE_ARCH.md 第六节):广告不是独立孤岛,而是**挂在同一个
社媒 Channel 上的能力**。一个已连的 IG/FB 渠道天然带着一个 Meta 广告账户(共用 Meta app +
Marketing API);X 渠道带 X Ads。链接信息存 `Channel.config['ad_account']`。

接口为 status + report,真实 API 未授权/未就绪时**诚实返回
pending 而非假数据**。这是框架层:连接与状态
真实可用,深度报表随各平台广告权限审批逐步接入。

引擎分工:status/report 的探测是**脚本**(确定性 API 探针);投放决策不在本模块
(只做探测,不做决策)。
"""
from __future__ import annotations

import logging

import httpx

from . import db
from .models import Channel

log = logging.getLogger("yominpost.ads_link")

_TIMEOUT = 15
FB_GRAPH = "https://graph.facebook.com/v25.0"

# 社媒平台 → 可链接的广告 provider。一个 Meta 广告账户同时投 IG + FB。
PLATFORM_AD_PROVIDER = {
    "instagram": "meta_ads",
    "facebook": "meta_ads",
    "x": "x_ads",
}

AD_PROVIDERS = {
    "meta_ads": {"label": "Meta Ads (Instagram + Facebook)", "covers": ["instagram", "facebook"],
                 "account_hint": "act_<广告账户ID>", "api": "Meta Marketing API"},
    "x_ads": {"label": "X Ads", "covers": ["x"], "account_hint": "<ads account id>",
              "api": "X Ads API"},
}


def provider_for(platform: str) -> str | None:
    return PLATFORM_AD_PROVIDER.get(platform)


def link(channel: Channel, ad_account_id: str, provider: str | None = None) -> dict:
    """把广告账户链接到该社媒渠道(存 config.ad_account)。返回链接后的即时 status。"""
    prov = provider or provider_for(channel.platform)
    if not prov:
        raise ValueError(f"{channel.platform} 暂不支持广告链接")
    channel.config = dict(channel.config or {})
    channel.config["ad_account"] = {
        "provider": prov, "ad_account_id": ad_account_id.strip(),
        "linked_at": db.now_iso(), "status": "pending", "config": {},
    }
    db.save_channel(channel)
    st = status(channel)                              # 立即探一次真实状态
    channel.config["ad_account"]["status"] = st["status"]
    db.save_channel(channel)
    return st


def unlink(channel: Channel) -> None:
    if (channel.config or {}).get("ad_account"):
        channel.config = dict(channel.config)
        channel.config.pop("ad_account", None)
        db.save_channel(channel)


def summary(channel: Channel) -> dict:
    """给 operations/Connections 的轻量摘要(不联网)。"""
    ad = (channel.config or {}).get("ad_account") or {}
    prov = ad.get("provider") or provider_for(channel.platform)
    return {"linked": bool(ad.get("ad_account_id")), "linkable": bool(prov),
            "provider": prov, "status": ad.get("status", "not_configured"),
            "account_id": ad.get("ad_account_id", "")}


# --------------------------------------------------------------------------- #
# 状态探测(脚本):真实 API 探针,未授权/未就绪诚实回 pending
# --------------------------------------------------------------------------- #
def status(channel: Channel) -> dict:
    """探测广告账户可用性。返回 {status: connected|pending|not_configured, reason, ...}。
    - not_configured:未填广告账户 id。
    - pending:填了但 API 未授权(缺 ads_management scope / 权限待批)—— 诚实,不假造。
    - connected:探针拿到广告账户信息。
    熔断的社媒渠道不联网(沿用账号情报纪律)。"""
    from . import platform_risk
    ad = (channel.config or {}).get("ad_account") or {}
    prov = ad.get("provider") or provider_for(channel.platform)
    acct = ad.get("ad_account_id", "")
    if not prov:
        return {"status": "not_supported", "provider": None,
                "reason": f"{channel.platform} 暂不支持广告链接"}
    if not acct:
        return {"status": "not_configured", "provider": prov,
                "reason": "未填写广告账户 ID", "hint": AD_PROVIDERS.get(prov, {}).get("account_hint", "")}
    if platform_risk.frozen_until(channel):
        return {"status": "pending", "provider": prov, "account_id": acct,
                "reason": "社媒渠道熔断中,暂不探测广告账户"}
    try:
        if prov == "meta_ads":
            return _meta_status(channel, acct)
        if prov == "x_ads":
            return _x_status(channel, acct)
    except Exception as e:  # noqa: BLE001
        return {"status": "pending", "provider": prov, "account_id": acct,
                "reason": f"探测失败:{type(e).__name__}: {e}"[:120]}
    return {"status": "pending", "provider": prov, "account_id": acct, "reason": "未知 provider"}


def _meta_token_of(channel: Channel) -> str:
    """该渠道自己的 Meta token(优先 page_token)。"""
    from .connectors.oauth_conn import _decrypt_cfg, _ensure_fresh_token
    _ensure_fresh_token(channel)
    tok, cfg = _decrypt_cfg(channel)
    return cfg.get("page_token", "") or tok


def _is_connected(c: Channel) -> bool:
    s = c.status.value if hasattr(c.status, "value") else str(c.status)
    return s == "connected"


def _meta_ads_token(channel: Channel) -> tuple[str, str]:
    """取能访问 Meta Marketing API 的 token。IG 广告实际由 Meta 商务广告账户管理,
    ads_management scope 在 **Facebook/Meta 商务 token** 上,而非 IG-login token ——
    故 instagram 渠道优先借用同 workspace 已连 Facebook 渠道的 token。
    返回 (token, source);拿不到返回 ('', 原因标记)。"""
    if channel.platform == "facebook":
        return _meta_token_of(channel), "facebook-self"
    if channel.platform == "instagram":
        for c in db.list_channels(channel.workspace_id):
            if c.platform == "facebook" and _is_connected(c):
                return _meta_token_of(c), "facebook-sibling"
        # 没有可借的 FB 商务 token → IG-login token 通常无广告 scope,标记 pending
        return "", "instagram-self-noads"
    return _meta_token_of(channel), "self"


def _meta_status(channel: Channel, acct: str) -> dict:
    """Meta Marketing API:GET /act_<id>?fields=name,account_status。需 token 带
    ads_management/ads_read scope —— 缺权限会 400,如实回 pending 提示补 scope。"""
    token, src = _meta_ads_token(channel)
    if not token:
        return {"status": "pending", "provider": "meta_ads", "account_id": acct,
                "reason": src or "无可用 Meta token"}
    if src == "instagram-self-noads":
        return {"status": "pending", "provider": "meta_ads", "account_id": acct,
                "reason": "IG 广告经 Meta 商务广告账户管理,请先连接带 ads_management 的 Facebook/Meta 账户后再链接"}
    act = acct if acct.startswith("act_") else f"act_{acct}"
    r = httpx.get(f"{FB_GRAPH}/{act}",
                  params={"fields": "name,account_status,currency,amount_spent", "access_token": token},
                  timeout=_TIMEOUT)
    d = r.json()
    if d.get("error"):
        msg = str((d.get("error") or {}).get("message", ""))
        low = msg.lower()
        need_scope = "permission" in low or "ads_management" in low or "(#100)" in msg or "(#200)" in msg
        return {"status": "pending", "provider": "meta_ads", "account_id": act,
                "reason": ("Meta 广告权限未授权(需给 app 加 ads_management/ads_read scope 并重连)"
                           if need_scope else f"Meta 返回:{msg[:100]}")}
    return {"status": "connected", "provider": "meta_ads", "account_id": act,
            "account_name": d.get("name", ""), "account_status": d.get("account_status"),
            "currency": d.get("currency", ""), "amount_spent": d.get("amount_spent")}


def _x_status(channel: Channel, acct: str) -> dict:
    """X Ads API 需单独的 Ads API 访问审批(OAuth1.0a/2.0 + 广告权限)。当前社媒 token
    不含广告 scope → 诚实 pending。"""
    return {"status": "pending", "provider": "x_ads", "account_id": acct,
            "reason": "X Ads API 需单独申请广告 API 访问权限(当前发帖 token 不含广告 scope)"}


def report(channel: Channel) -> dict:
    """归一化投放报表。未就绪时 {ready:False, reason},就绪后填 campaigns/spend/perf。
    """
    st = status(channel)
    if st["status"] != "connected":
        return {"ready": False, "provider": st.get("provider"), "reason": st.get("reason", ""),
                "status": st["status"], "campaigns": [], "spend": None}
    prov = st["provider"]
    if prov == "meta_ads":
        return _meta_report(channel, st["account_id"])
    return {"ready": False, "provider": prov, "reason": "报表未接入", "campaigns": [], "spend": None}


def _meta_report(channel: Channel, act: str) -> dict:
    """Meta 广告归一化报表:拉 campaigns + insights。权限不足则 ready=False。"""
    token, _src = _meta_ads_token(channel)
    if not token:
        return {"ready": False, "provider": "meta_ads", "reason": "无可用 Meta token",
                "campaigns": [], "spend": None}
    try:
        r = httpx.get(f"{FB_GRAPH}/{act}/campaigns",
                      params={"fields": "name,status,objective,daily_budget", "limit": 25,
                              "access_token": token}, timeout=_TIMEOUT)
        d = r.json()
        if d.get("error"):
            return {"ready": False, "provider": "meta_ads",
                    "reason": str((d.get("error") or {}).get("message", ""))[:120],
                    "campaigns": [], "spend": None}
        camps = [{"id": c.get("id"), "name": c.get("name"), "status": c.get("status"),
                  "objective": c.get("objective"),
                  "daily_budget": c.get("daily_budget")} for c in d.get("data", [])]
        return {"ready": True, "provider": "meta_ads", "account_id": act,
                "campaigns": camps, "campaign_count": len(camps)}
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "provider": "meta_ads", "reason": f"{type(e).__name__}: {e}"[:120],
                "campaigns": [], "spend": None}
