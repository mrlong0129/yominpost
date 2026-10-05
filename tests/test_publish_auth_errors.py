"""发布鉴权错误传播:auth 类失败标记渠道(Connections 出重连)/ 内容与瞬时错误不标 /
成功自愈 / 部分失败重试幂等(已发渠道绝不重发)。"""
import warnings

import pytest
from fastapi.testclient import TestClient

from yominpost import db, platform_risk, services
from yominpost.connectors.base import PublishResult
from yominpost.connectors.oauth_conn import is_auth_error
from yominpost.models import ChannelStatus, PostContent, PostCreate

warnings.filterwarnings("ignore")

# Meta 风控封锁:含 OAuthException 但重连无效 → 归 platform_risk,不是 auth error。
IG_BLOCK = ('HTTP 400: {"error":{"message":"API access blocked.","type":"OAuthException",'
            '"code":200,"fbtrace_id":"AsreR"}}')
# 真·鉴权失败(令牌过期,重连可解):code 190 session expired。
AUTH_ERR = ('HTTP 401: {"error":{"code":190,"message":"Error validating access token: '
            'Session has expired","type":"OAuthException"}}')


@pytest.fixture()
def client():
    from yominpost.web.app import app
    with TestClient(app) as c:
        yield c


def _mk_post(client, ws_id, channel_ids):
    return client.post(f"/api/workspaces/{ws_id}/posts", json={
        "title": "auth-test", "content": {"hook": "h", "body": "b", "cta": "c"},
        "channel_ids": channel_ids}).json()


class _StubConn:
    def __init__(self, result_by_channel):
        self.result_by_channel = result_by_channel
        self.calls = []

    def publish(self, channel, content, media=None):
        self.calls.append(channel.id)
        return self.result_by_channel[channel.id]


def _connect(client, ws_id, platform="pinterest"):
    return client.post(f"/api/workspaces/{ws_id}/channels/connect",
                       json={"platform": platform, "display_name": "T"}).json()


def test_is_auth_error_unit():
    # IG_BLOCK 是平台风控封锁,不是 auth error(重连无效)—— 二者互斥
    assert platform_risk.is_platform_block(IG_BLOCK)
    assert not is_auth_error(IG_BLOCK)
    assert is_auth_error(AUTH_ERR)                          # code 190 token 过期 = auth
    assert is_auth_error("HTTP 401: nope")
    assert is_auth_error('{"error":{"code":190,"type":"OAuthException"}} code":190')
    assert not is_auth_error("post id 4015550 created")     # 裸 401 误配防护
    assert not is_auth_error("HTTP 503: upstream")
    assert not is_auth_error("需要一张公开图片")


def _publish_with_stub(client, monkeypatch, results):
    ws = client.get("/api/workspaces").json()[0]
    chans = {p: _connect(client, ws["id"], p) for p in results}
    stub = _StubConn({chans[p]["id"]: r for p, r in results.items()})
    monkeypatch.setattr(services, "get_connector", lambda platform: stub)
    post = _mk_post(client, ws["id"], [c["id"] for c in chans.values()])
    pub = client.post(f"/api/posts/{post['id']}/publish").json()
    return ws, chans, stub, pub


def test_auth_fail_marks_channel_but_keeps_connected(client, monkeypatch):
    ws, chans, _stub, pub = _publish_with_stub(
        client, monkeypatch, {"pinterest": PublishResult(ok=False, error=AUTH_ERR)})
    assert pub["status"] == "failed"
    ch = db.get_channel(chans["pinterest"]["id"])
    assert str(ch.config.get("error", "")).startswith("发布鉴权失败")
    assert ch.status == ChannelStatus.connected            # 不翻 status(重试闸)
    out = client.get(f"/api/workspaces/{ws['id']}/channels").json()
    mine = next(c for c in out if c["id"] == ch.id)
    assert mine["has_error"] is True                        # Connections 能看见


def test_content_and_transient_fail_do_not_mark(client, monkeypatch):
    for err in ("需要一张公开图片", "HTTP 503: upstream", "request timed out"):
        _ws, chans, _stub, _pub = _publish_with_stub(
            client, monkeypatch, {"threads": PublishResult(ok=False, error=err)})
        ch = db.get_channel(chans["threads"]["id"])
        assert "error" not in (ch.config or {}), err


def test_success_self_heals_channel_error(client, monkeypatch):
    ws = client.get("/api/workspaces").json()[0]
    chan = _connect(client, ws["id"], "xiaohongshu")
    ch = db.get_channel(chan["id"])
    ch.config = dict(ch.config or {}); ch.config["error"] = "发布鉴权失败,请重新连接:old"
    db.save_channel(ch)
    stub = _StubConn({chan["id"]: PublishResult(ok=True, external_id="x1")})
    monkeypatch.setattr(services, "get_connector", lambda platform: stub)
    post = _mk_post(client, ws["id"], [chan["id"]])
    assert client.post(f"/api/posts/{post['id']}/publish").json()["status"] == "posted"
    assert "error" not in (db.get_channel(chan["id"]).config or {})


def test_partial_fail_retry_never_republishes_posted_target(client, monkeypatch):
    ws = client.get("/api/workspaces").json()[0]
    ok_ch = _connect(client, ws["id"], "tiktok")
    bad_ch = _connect(client, ws["id"], "threads")
    stub = _StubConn({ok_ch["id"]: PublishResult(ok=True, external_id="keep-1"),
                      bad_ch["id"]: PublishResult(ok=False, error=AUTH_ERR)})
    monkeypatch.setattr(services, "get_connector", lambda platform: stub)
    post = _mk_post(client, ws["id"], [ok_ch["id"], bad_ch["id"]])
    pub = client.post(f"/api/posts/{post['id']}/publish").json()
    assert pub["status"] == "failed"
    # 重试:改好渠道,再发 —— 已发 target 不得再调 connector
    stub.result_by_channel[bad_ch["id"]] = PublishResult(ok=True, external_id="new-2")
    stub.calls.clear()
    pub2 = client.post(f"/api/posts/{post['id']}/publish").json()
    assert pub2["status"] == "posted"
    assert stub.calls == [bad_ch["id"]]                     # 只发失败渠道
    kept = next(t for t in pub2["targets"] if t["channel_id"] == ok_ch["id"])
    assert kept["external_id"] == "keep-1"                  # 已发 external_id 原封不动


def test_channel_health_marks_and_heals(client, monkeypatch):
    """健康检查:探针鉴权失败 → 标记;探针通过 → 自愈 + 打时间戳;瞬时失败不标。"""
    from yominpost import oauth_specs
    from yominpost.connectors import oauth_conn
    ws = client.get("/api/workspaces").json()[0]
    ch = client.post(f"/api/workspaces/{ws['id']}/channels/connect",
                     json={"platform": "x", "display_name": "hx"}).json()   # x = real oauth
    obj = db.get_channel(ch["id"])                       # 真 OAuth mock-connect 是 disconnected,
    obj.status = ChannelStatus.connected                 # 直接置 connected 模拟已完成授权
    db.save_channel(obj)
    monkeypatch.setattr(oauth_conn, "_ensure_fresh_token", lambda c: "tok")
    monkeypatch.setattr(oauth_conn, "_decrypt_cfg", lambda c: ("tok", dict(c.config or {})))
    # ① 鉴权失败 → 标记
    monkeypatch.setattr(oauth_specs, "fetch_identity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 401: unauthorized")))
    out = services.check_channel_health()
    assert out["marked"] >= 1
    got = db.get_channel(ch["id"])
    assert "健康检查失败" in str(got.config.get("error", ""))
    assert client.get("/api/notifications").json()["count"] >= 1     # 全局通知可见
    # ② 探针恢复 → 自愈
    monkeypatch.setattr(oauth_specs, "fetch_identity", lambda *a, **k: {"handle": "ok"})
    out2 = services.check_channel_health()
    assert out2["healed"] >= 1
    got2 = db.get_channel(ch["id"])
    assert "error" not in (got2.config or {})
    assert got2.config.get("health_checked_at")
    # ③ 瞬时失败不标
    monkeypatch.setattr(oauth_specs, "fetch_identity",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 503: flaky")))
    services.check_channel_health()
    assert "error" not in (db.get_channel(ch["id"]).config or {})
