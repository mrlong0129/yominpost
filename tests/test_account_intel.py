"""账号情报引擎:脚本信号 / 规则评分 / LLM 兜底叙述 / 持久化 / 连接时基线 / API。"""
import warnings
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from yominpost import account_intel, db, platform_risk
from yominpost.models import Channel, ChannelStatus

warnings.filterwarnings("ignore")


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def _connect(client, ws_id, platform="tiktok"):
    return client.post(f"/api/workspaces/{ws_id}/channels/connect",
                       json={"platform": platform, "display_name": "T"}).json()


def test_build_signals_new_account_flagged(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    sig = account_intel.build_signals(ch)
    assert sig["account_age_days"] == 0                     # 刚连,可观测年龄 0
    assert any("新账号" in f for f in sig["risk_flags"])    # warm-up 期被标记
    assert sig["frozen"] is False


def test_score_health_new_account_native(client):
    """新 IG 账号:平台原生判定(账户状态)给 watch + warmup 因子,带原生 headline。"""
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    sig = account_intel.build_signals(ch)
    score = account_intel.score_health(ch, sig)
    assert score["health_grade"] == "watch"
    assert any(b["factor"] == "warmup" for b in score["score_breakdown"])
    assert score["mechanism"]["term"] == "账户状态"           # IG 原生说法
    assert score["data_source"] == "dashboard"                # 违规仅 Meta 后台


def test_reddit_karma_native_health():
    """Reddit 用真实 karma 机制判定(用户点名的例子),不是通用分。"""
    from yominpost import platform_health
    # 已验证 + 高 karma + 老账号 → healthy,headline 带 Karma
    hi = platform_health.assess("reddit", {"link_karma": 800, "comment_karma": 1200,
                                           "total_karma": 2000, "has_verified_email": True,
                                           "account_age_days": 400})
    assert hi["grade"] == "healthy" and hi["mechanism"]["term"] == "Karma"
    assert "Karma 2000" in hi["native_headline"]
    # 被封 → blocked
    sus = platform_health.assess("reddit", {"is_suspended": True, "total_karma": 5})
    assert sus["grade"] == "blocked" and "suspend" in sus["standing"].lower() or "封禁" in sus["standing"]
    # 邮箱未验证 → at_risk
    unv = platform_health.assess("reddit", {"has_verified_email": False, "total_karma": 3,
                                            "account_age_days": 2})
    assert unv["grade"] == "at_risk"


def test_youtube_channel_standing_native():
    """YouTube 用 good standing / strikes(全 API 可拉)判定。"""
    from yominpost import platform_health
    good = platform_health.assess("youtube", {"yt_overall_good_standing": True,
                                              "yt_cg_good_standing": True,
                                              "yt_copyright_good_standing": True, "subscribers": 1200})
    assert good["grade"] == "healthy" and "订阅 1200" in good["native_headline"]
    strike = platform_health.assess("youtube", {"yt_overall_good_standing": False,
                                                "yt_cg_good_standing": False,
                                                "yt_copyright_good_standing": True, "subscribers": 5})
    assert strike["grade"] in ("watch", "at_risk")
    assert any(b["factor"] == "cg_strike" for b in strike["breakdown"])


def test_linkedin_ssi_honest_dashboard():
    """LinkedIn SSI 无 API → 诚实标 dashboard,不编造分数。"""
    from yominpost import platform_health
    v = platform_health.assess("linkedin", {"followers": 50})
    assert v["mechanism"]["term"] == "SSI 社交销售指数"
    assert v["data_source"] == "dashboard"
    assert "SSI" in v["native_headline"] or "ssi" in v["native_headline"].lower() or "需" in v["dashboard_hint"]


def test_score_health_frozen_is_blocked(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "pinterest")["id"])
    platform_risk.freeze_channel(ch, "API access blocked.")
    ch = db.get_channel(ch.id)
    sig = account_intel.build_signals(ch)
    score = account_intel.score_health(ch, sig)
    assert score["health_grade"] == "blocked"
    assert score["health_score"] == 0
    assert sig["frozen"] is True
    assert sig.get("metrics_note")                         # 熔断:走缓存不联网


def test_platform_block_via_config_error_is_blocked(client):
    """config.error 是平台封锁(API access blocked)但没走 freeze_channel —— 引擎仍应
    判为 blocked(此即生产 IG 渠道的实况:只标了 error,没设 risk_frozen_until)。"""
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    ch.config = dict(ch.config or {})
    ch.config["error"] = "Meta 已封锁该账号的 API 访问(API access blocked),重新连接无效。"
    db.save_channel(ch)
    sig = account_intel.build_signals(ch)
    assert sig["platform_blocked"] is True
    score = account_intel.score_health(ch, sig)
    assert score["health_grade"] == "blocked" and score["health_score"] == 0


def test_assess_rule_fallback_offline(client, monkeypatch):
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "tiktok")["id"])
    # provider 抛错 → 必须降级到规则版,不炸
    import yominpost.services as svc
    monkeypatch.setattr(svc, "active_provider", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    sig = account_intel.build_signals(ch)
    score = account_intel.score_health(ch, sig)
    a = account_intel.assess(ch, sig, score)
    assert a["engine"] == "rule"
    assert len(a["assessment"]) > 10
    assert a["recommended_actions"]


def test_refresh_profile_persists_and_grades(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "tiktok")["id"])
    prof = account_intel.refresh_profile(ch, use_llm=False)
    assert prof["health_grade"] in account_intel.GRADES
    assert prof["engine"]["signals"] == "script"
    # 已落库
    got = account_intel.get_profile(db.get_channel(ch.id))
    assert got and got["checked_at"] == prof["checked_at"]


def test_connect_time_baseline_via_oauth(client, monkeypatch):
    """complete_oauth 成功即建立健康基线(直接建 connected 渠道模拟)。"""
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    # connect 走的是 mock connect(非 oauth 回调),直接验证 _profile_on_connect 幂等可跑
    from yominpost.services import _profile_on_connect
    _profile_on_connect(ch)
    assert account_intel.get_profile(db.get_channel(ch.id)) is not None


def test_profile_api_endpoints(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"], "tiktok")
    # GET 无缓存时自动快速刷新
    p = client.get(f"/api/channels/{ch['id']}/profile").json()
    assert p["health_grade"] in account_intel.GRADES
    # channel_out 带 health 摘要
    chans = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    mine = next(c for c in chans if c["id"] == ch["id"])
    assert mine.get("health", {}).get("grade") == p["health_grade"]


def test_refresh_all_skips_frozen_network(client):
    ws = client.get("/api/workspaces").json()[0]
    ch = db.get_channel(_connect(client, ws["id"], "instagram")["id"])
    platform_risk.freeze_channel(ch, "API access blocked.")
    out = account_intel.refresh_all(ws["id"], use_llm=False)
    assert out["refreshed"] >= 1
    prof = account_intel.get_profile(db.get_channel(ch.id))
    assert prof["health_grade"] == "blocked"
    assert prof["signals"].get("metrics_note")             # 熔断渠道未联网
