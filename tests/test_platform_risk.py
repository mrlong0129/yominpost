"""平台风控层:内容 lint / 节奏 warm-up / 重复度 / 发布硬闸 / 封锁熔断 / 预检 API。
用 2026-07-14 IG 封号事故的真实内容作为回归样本。"""
import warnings
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from yominpost import db, platform_risk, services
from yominpost.connectors.base import PublishResult
from yominpost.models import Post, PostContent

warnings.filterwarnings("ignore")

IG_BLOCK_ERR = ('HTTP 400: {"error":{"message":"API access blocked.","type":"OAuthException",'
                '"code":200,"fbtrace_id":"AMKC"}}')
# 事故帖 1:prompt 原文进 caption
LEAKED = "· 为 Acme AI 的 Listing Optimizer(亚马逊 listing 优化工具)写一条 Instagram 帖子"
# 事故帖 3:诱导互动 CTA
BAIT = "Comment or DM \"ASIN\" and I'll send a free first answerability audit."


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def _post_model(hook="h", body="clean body about product design", cta="", tags=None):
    return Post(id="p_t", workspace_id="ws_t", title="t",
                content=PostContent(hook=hook, body=body, cta=cta, hashtags=tags or []))


def test_lint_prompt_leak_blocks():
    issues = platform_risk.lint_content("instagram", _post_model(body=LEAKED))
    assert any(i["code"] == "prompt_leak" and i["level"] == "block" for i in issues)


def test_lint_engagement_bait_blocks_on_meta_warns_elsewhere():
    ig = platform_risk.lint_content("instagram", _post_model(cta=BAIT))
    assert any(i["code"] == "engagement_bait" and i["level"] == "block" for i in ig)
    pin = platform_risk.lint_content("pinterest", _post_model(cta=BAIT))
    assert any(i["code"] == "engagement_bait" and i["level"] == "warn" for i in pin)


def test_lint_hashtag_over_cap_warns():
    tags = [f"#t{i}" for i in range(9)]
    issues = platform_risk.lint_content("instagram", _post_model(tags=tags))
    assert any(i["code"] == "hashtag_over" for i in issues)
    assert all(i["level"] != "block" for i in issues if i["code"] == "hashtag_over")


def _connect(client, ws_id, platform="tiktok"):
    return client.post(f"/api/workspaces/{ws_id}/channels/connect",
                       json={"platform": platform, "display_name": "T"}).json()


class _StubConn:
    def __init__(self, result):
        self.result, self.calls = result, []

    def publish(self, channel, content, media=None):
        self.calls.append(channel.id)
        return self.result


def _mk_post(client, ws_id, ch_ids, body="A fresh take on product QA workflows today.",
             cta="", tags=None):
    return client.post(f"/api/workspaces/{ws_id}/posts", json={
        "title": "risk-test",
        "content": {"hook": "", "body": body, "cta": cta, "hashtags": tags or []},
        "channel_ids": ch_ids}).json()


def test_publish_gate_blocks_prompt_leak_without_touching_connector(client, monkeypatch):
    monkeypatch.setenv("YOMINPOST_RISK_GATE", "1")     # 显式开软风控门(conftest 默认关)
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"])
    stub = _StubConn(PublishResult(ok=True, external_id="x"))
    monkeypatch.setattr(services, "get_connector", lambda p: stub)
    post = _mk_post(client, ws["id"], [ch["id"]], body=LEAKED)
    pub = client.post(f"/api/posts/{post['id']}/publish").json()
    assert pub["status"] == "failed"
    assert "风控拦截" in (pub["targets"][0]["error"] or "")
    assert stub.calls == []                       # 根本没出手


def test_warmup_cadence_blocks_second_post_same_day(client, monkeypatch):
    monkeypatch.setenv("YOMINPOST_RISK_GATE", "1")
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"])               # 刚接入 → warm-up 期,上限 1 条/天
    stub = _StubConn(PublishResult(ok=True, external_id="x1"))
    monkeypatch.setattr(services, "get_connector", lambda p: stub)
    p1 = _mk_post(client, ws["id"], [ch["id"]])
    assert client.post(f"/api/posts/{p1['id']}/publish").json()["status"] == "posted"
    p2 = _mk_post(client, ws["id"], [ch["id"]],
                  body="Second, very different angle on shipping fast without breaking trust.")
    pub2 = client.post(f"/api/posts/{p2['id']}/publish").json()
    assert pub2["status"] == "failed"
    assert "风控拦截" in (pub2["targets"][0]["error"] or "")
    assert stub.calls == [ch["id"]]               # 只有第一条真的发出


def test_duplicate_detection_unit(client, monkeypatch):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"])
    stub = _StubConn(PublishResult(ok=True, external_id="x1"))
    monkeypatch.setattr(services, "get_connector", lambda p: stub)
    body = "Most Amazon listings do not lose the sale on design they lose it on unanswered questions"
    p1 = _mk_post(client, ws["id"], [ch["id"]], body=body)
    client.post(f"/api/posts/{p1['id']}/publish")
    chan = db.get_channel(ch["id"])
    dup = platform_risk.check_duplicate(chan, _post_model(body=body))
    assert any(i["code"] == "duplicate" and i["level"] == "block" for i in dup)


def test_platform_block_freezes_channel_and_stops_retries(client, monkeypatch):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"], "pinterest")
    stub = _StubConn(PublishResult(ok=False, error=IG_BLOCK_ERR))
    monkeypatch.setattr(services, "get_connector", lambda p: stub)
    p1 = _mk_post(client, ws["id"], [ch["id"]])
    pub = client.post(f"/api/posts/{p1['id']}/publish").json()
    assert pub["status"] == "failed"
    got = db.get_channel(ch["id"])
    assert got.config.get("risk_frozen_until")                       # 已熔断
    assert "熔断" in str(got.config.get("error", ""))
    assert platform_risk.frozen_until(got) is not None
    # 全局通知可见
    assert any(n["platform"] == "pinterest" for n in services.channel_notifications())
    # 重试:熔断态无条件拦下,connector 不再被调用(避免加重画像)。
    # 熔断是硬安全停,即使软风控门关闭(conftest 默认)也生效。
    stub.calls.clear()
    pub2 = client.post(f"/api/posts/{p1['id']}/publish").json()
    assert "熔断" in (pub2["targets"][0]["error"] or "")
    assert stub.calls == []


def test_health_check_skips_frozen_channel(client, monkeypatch):
    from yominpost import oauth_specs
    from yominpost.connectors import oauth_conn
    from yominpost.models import ChannelStatus
    ws = client.get("/api/workspaces").json()[0]
    ch = client.post(f"/api/workspaces/{ws['id']}/channels/connect",
                     json={"platform": "x", "display_name": "hx"}).json()   # x = real oauth
    obj = db.get_channel(ch["id"])
    obj.status = ChannelStatus.connected
    until = datetime.now(timezone.utc) + timedelta(hours=48)
    obj.config = dict(obj.config or {})
    obj.config["risk_frozen_until"] = until.isoformat(timespec="seconds")
    db.save_channel(obj)
    monkeypatch.setattr(oauth_conn, "_ensure_fresh_token", lambda c: "tok")
    monkeypatch.setattr(oauth_conn, "_decrypt_cfg", lambda c: ("tok", dict(c.config or {})))
    monkeypatch.setattr(oauth_specs, "fetch_identity", lambda *a, **k: {"handle": "probed"})
    services.check_channel_health()
    frozen = db.get_channel(ch["id"])
    assert "health_checked_at" not in (frozen.config or {})   # 冻结渠道被跳过,没打健康戳
    assert frozen.config.get("risk_frozen_until")             # 冻结标记仍在
    # 反证:解冻后同一渠道会被探针(证明跳过确由熔断导致,而非其它原因)
    frozen.config = dict(frozen.config); frozen.config.pop("risk_frozen_until")
    db.save_channel(frozen)
    services.check_channel_health()
    assert db.get_channel(ch["id"]).config.get("health_checked_at")


def test_risk_check_endpoint(client, monkeypatch):
    ws = client.get("/api/workspaces").json()[0]
    ch = _connect(client, ws["id"])
    post = _mk_post(client, ws["id"], [ch["id"]], body=LEAKED, tags=[f"#t{i}" for i in range(9)])
    rc = client.get(f"/api/posts/{post['id']}/risk-check").json()
    assert rc["has_block"] is True
    codes = {i["code"] for i in rc["issues"]["tiktok"]}
    assert "prompt_leak" in codes and "hashtag_over" in codes
