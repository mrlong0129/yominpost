"""运营状态聚合 + 广告账户链接框架。"""
import warnings

import pytest
from fastapi.testclient import TestClient

from yominpost import ads_link, db, operations, platform_risk

warnings.filterwarnings("ignore")


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def _connect(client, ws_id, platform):
    return client.post(f"/api/workspaces/{ws_id}/channels/connect",
                       json={"platform": platform, "display_name": platform.upper()}).json()


# ---------------- operations ----------------
def test_workspace_operations_rollup(client):
    ws = client.get("/api/workspaces").json()[0]
    _connect(client, ws["id"], "instagram")
    _connect(client, ws["id"], "tiktok")
    ops = client.get(f"/api/workspaces/{ws['id']}/operations").json()
    assert ops["rollup"]["channels"] >= 2
    assert set(("healthy", "watch", "at_risk", "blocked")).issubset(ops["rollup"].keys())
    # 每渠道有健康/发布/广告三块
    row = ops["channels"][0]
    assert "health" in row and "publish" in row and "ads" in row


def test_operations_sorts_risky_first(client):
    ws = client.get("/api/workspaces").json()[0]
    good = db.get_channel(_connect(client, ws["id"], "tiktok")["id"])
    bad = db.get_channel(_connect(client, ws["id"], "pinterest")["id"])
    platform_risk.freeze_channel(bad, "API access blocked.")
    from yominpost import account_intel
    account_intel.refresh_all(ws["id"], use_llm=False)
    ops = client.get(f"/api/workspaces/{ws['id']}/operations").json()
    # blocked 渠道排最前
    assert ops["channels"][0]["health"]["grade"] == "blocked"
    assert ops["rollup"]["blocked"] >= 1


# ---------------- ads_link ----------------
def test_platform_block_is_health_not_connection_error(client):
    """IA:平台封控是健康问题不是连接问题 —— channel_out 不标 has_error(连接卡不红),
    标 health_alert;通知按 kind=health 分流到账号管理,不算 connection 重连。"""
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    from yominpost.models import ChannelStatus
    ch.status = ChannelStatus.connected
    ch.config = dict(ch.config or {})
    ch.config["error"] = "Meta 已封锁该账号的 API 访问(API access blocked),重新连接无效。"
    db.save_channel(ch)
    # channel_out:连接卡不标红,只标 health_alert
    mine = next(c for c in client.get(f"/api/workspaces/{ws['id']}/channels").json() if c["id"] == ch.id)
    assert mine["has_error"] is False
    assert mine["health_alert"] is True
    # 通知分流:进 health 桶,不进 connection 桶
    n = client.get("/api/notifications").json()
    assert any(a["platform"] == "instagram" for a in n["health_alerts"])
    assert not any(a["platform"] == "instagram" for a in n["connection_errors"])
    assert n["health_count"] >= 1


def test_channel_sync_endpoint_unifies_content_and_status(client):
    """手动刷新 = 一个入口同时同步内容事实 + 账号状态画像,并记录 synced_at。"""
    ws = client.get("/api/workspaces").json()[0]
    _connect(client, ws["id"], "tiktok")
    r = client.post(f"/api/workspaces/{ws['id']}/channel-sync").json()
    assert "content" in r and "profiles" in r and r["synced_at"]
    assert r["profiles"]["refreshed"] >= 1                 # 画像被刷新
    # operations 暴露 synced_at,前端据此显示"上次同步"
    ops = client.get(f"/api/workspaces/{ws['id']}/operations").json()
    assert ops["synced_at"] == r["synced_at"]


def test_daily_channel_sync_all_workspaces():
    from yominpost import services
    out = services.daily_channel_sync()
    assert "content" in out and "profiles" in out and out["synced_at"]
    assert services.last_channel_sync_at() == out["synced_at"]


def test_ad_provider_mapping():
    assert ads_link.provider_for("instagram") == "meta_ads"
    assert ads_link.provider_for("facebook") == "meta_ads"
    assert ads_link.provider_for("x") == "x_ads"
    assert ads_link.provider_for("tiktok") is None


def test_ads_status_not_configured_then_link(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"], "instagram")
    st = client.get(f"/api/channels/{ch['id']}/ads/status").json()
    assert st["status"] == "not_configured"
    assert st["provider"] == "meta_ads"
    # 链接一个广告账户 → 变 pending(社媒 token 无广告 scope,诚实 pending 不假 connected)
    linked = client.post(f"/api/channels/{ch['id']}/ads/link",
                         json={"ad_account_id": "act_123456"}).json()
    assert linked["status"] in ("pending", "connected")
    assert linked["provider"] == "meta_ads"
    # channel_out 带 ads 摘要
    chans = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    mine = next(c for c in chans if c["id"] == ch["id"])
    assert mine["ads"]["provider"] == "meta_ads"


def test_ads_link_unsupported_platform(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"], "tiktok")
    r = client.post(f"/api/channels/{ch['id']}/ads/link", json={"ad_account_id": "x"})
    assert r.status_code == 400


def test_ads_report_pending_when_not_connected(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"], "x")
    client.post(f"/api/channels/{ch['id']}/ads/link", json={"ad_account_id": "9999"})
    rep = client.get(f"/api/channels/{ch['id']}/ads/report").json()
    assert rep["ready"] is False                       # X Ads 需单独审批 → 诚实 pending
    assert rep["provider"] == "x_ads"
    assert rep["campaigns"] == []


def test_ig_ads_borrows_fb_sibling_token(client, monkeypatch):
    """IG 广告经 Meta 商务广告账户管理:instagram 渠道应借同 workspace 已连 Facebook
    渠道的 token(ads_management 在 FB 商务 token 上,不在 IG-login token)。"""
    ws = client.get("/api/workspaces").json()[0]
    ig = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    # 无 FB 兄弟渠道 → 标记 instagram-self-noads(拿不到广告 token)
    tok, src = ads_link._meta_ads_token(ig)
    assert src == "instagram-self-noads" and tok == ""
    # 造一个 connected 的 FB 渠道 + token
    fb = db.get_channel(_connect(client, ws["id"], "facebook")["id"])
    from yominpost.models import ChannelStatus
    fb.status = ChannelStatus.connected
    from yominpost.crypto import encrypt
    fb.config = dict(fb.config or {}); fb.config["page_token_enc"] = encrypt("FBADS_TOKEN")
    db.save_channel(fb)
    tok2, src2 = ads_link._meta_ads_token(db.get_channel(ig.id))
    assert src2 == "facebook-sibling" and tok2 == "FBADS_TOKEN"


def test_ads_summary_frozen_channel_no_probe(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    ads_link.link(ch, "act_777")
    platform_risk.freeze_channel(db.get_channel(ch.id), "API access blocked.")
    st = ads_link.status(db.get_channel(ch.id))
    assert st["status"] == "pending"
    assert "熔断" in st["reason"]
