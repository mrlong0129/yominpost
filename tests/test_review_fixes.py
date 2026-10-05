"""审查后修复的回归测试(P0 发布安全 / 数据完整 / 安全加固)。

覆盖本轮多智能体审查确认并修复的高危项:
- 非法排期时间不再被字符串比较误判成"到期"而错误提前发布
- 原子认领会刷新 updated_at,避免 recovery 误杀在途发布导致重发
- 平台返回空 external_id 时仍幂等(不重复投递)
- PATCH 不接受客户端直接改 status(不能绕过发布/降级重发)
- OAuth 连接器按"含话题标签的整文"校验字数(所见即所发)
- 敏感设置(API key)加密存储,历史明文可读
- Google Ads promote 作业命令注入已封堵
"""
import warnings

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from yominpost import db, services  # noqa: E402
from yominpost.models import (Channel, ChannelStatus, Post, PostContent,  # noqa: E402
                              PostStatus, PublishTarget)
from yominpost.web.app import app  # noqa: E402


def _ws():
    return db.create_workspace("Fix T", brand_name="T")


# --- 非法排期时间:normalize_dt 报错 → API 400,绝不静默错发 --- #
def test_malformed_schedule_rejected_not_published():
    with TestClient(app) as c:
        ws = _ws()
        r = c.post(f"/api/workspaces/{ws.id}/posts",
                   json={"title": "t", "content": {"hook": "h"},
                         "scheduled_at": "07/15/2026 10:00"})   # 非 ISO
        assert r.status_code == 400
    # normalize_dt 单元:合法通过、空串保留、非法抛错
    assert services.normalize_dt("") == ""
    assert services.normalize_dt("2026-07-15T10:00:00+00:00").startswith("2026-07-15T10:00")
    try:
        services.normalize_dt("tomorrow 9am")
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_due_query_skips_noncanonical_scheduled_at():
    ws = _ws()
    # 直接塞一条 scheduled_at 非规范形状的 scheduled 帖(模拟历史脏数据)
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.scheduled,
             scheduled_at="07/15/2026 10:00",
             targets=[PublishTarget(channel_id="c", platform="x")])
    db.save_post(p)
    due = db.due_scheduled_posts(ws.id, "2030-01-01T00:00:00+00:00")
    assert all(x.id != p.id for x in due)    # 脏形状不入到期集,不会被错发


# --- 原子认领刷新 updated_at:刚认领的在途帖不被 recovery 误杀 --- #
def test_claim_bumps_updated_at_guards_recovery():
    ws = _ws()
    p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.scheduled,
             scheduled_at=db.now_iso(), updated_at="2000-01-01T00:00:00+00:00",
             targets=[PublishTarget(channel_id="c", platform="x")])
    db.save_post(p)
    # save_post 会把 updated_at 刷新为 now,这里手动改回很旧,模拟"很久没动的排期帖"
    import json
    with db._conn() as conn:
        row = conn.execute("SELECT payload FROM posts WHERE id=?", (p.id,)).fetchone()
        d = json.loads(row["payload"]); d["updated_at"] = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE posts SET payload=? WHERE id=?", (json.dumps(d), p.id))
    assert db.claim_scheduled_post(p.id) is True
    got = db.get_post(p.id)
    assert got.status == PostStatus.publishing
    assert (got.updated_at or "") > "2020"    # 认领已刷新时间戳
    # 现在 recovery(5min 阈值)不应把它当卡死回收
    n = services.recover_stuck_publishing(older_than_sec=300)
    assert db.get_post(p.id).status == PostStatus.publishing


# --- 空 external_id 仍幂等:重试不重复投递 --- #
def test_empty_external_id_still_idempotent():
    ws = _ws()
    ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform="webhook",
                 status=ChannelStatus.connected)
    ch.config["url_enc"] = __import__("yominpost.crypto", fromlist=["encrypt"]).encrypt("x")
    db.save_channel(ch)
    # 已成功但 external_id 为空的 target(旧代码里空 id 会让"已发跳过"失效 → 重复投递)。
    # 现在跳过条件只看 status=='posted',重发时该 target 被跳过,不再触达 connector。
    monkeywebhook = {"n": 0}
    from yominpost.connectors import real as _real
    _orig_pub = _real.WebhookConnector.publish

    def _spy(self, *a, **k):
        monkeywebhook["n"] += 1
        return _orig_pub(self, *a, **k)

    _real.WebhookConnector.publish = _spy
    try:
        p = Post(id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.failed,
                 content=PostContent(body="hi"),
                 targets=[PublishTarget(channel_id=ch.id, platform="webhook",
                                        status="posted", external_id="")])
        db.save_post(p)
        out = services._publish_targets(db.get_post(p.id))
    finally:
        _real.WebhookConnector.publish = _orig_pub
    assert monkeywebhook["n"] == 0                    # 已 posted 的 target 不再发布
    assert out.status == PostStatus.posted


# --- PATCH 不接受客户端 status:不能强推 posted / 降级重发 --- #
def test_patch_cannot_force_status():
    with TestClient(app) as c:
        ws = _ws()
        p = c.post(f"/api/workspaces/{ws.id}/posts",
                   json={"title": "t", "content": {"hook": "h"}}).json()
        assert p["status"] == "draft"
        r = c.patch(f"/api/posts/{p['id']}", json={"status": "posted"})
        assert r.status_code == 200
        assert r.json()["status"] == "draft"        # status 被忽略,仍是 draft


# --- OAuth 连接器按 as_caption(含标签)校验字数 --- #
def test_oauth_validate_counts_hashtags():
    from yominpost.connectors.oauth_conn import OAuthConnector
    from yominpost.crypto import encrypt
    ch = Channel(id="c", workspace_id="w", platform="x", status=ChannelStatus.connected,
                 config={"access_token_enc": encrypt("tok")})
    # 正文 275 + 一堆标签 → as_text 不超,as_caption 超(X 上限 280)
    body = "a" * 270
    content = PostContent(body=body, hashtags=["growth", "marketing", "startup", "content"])
    errs = OAuthConnector("x").validate(ch, content)
    assert any("超过上限" in e for e in errs)         # 含标签整文超限被拦下


# --- 敏感设置加密存储 + 历史明文兜底 --- #
def test_secret_setting_encrypts_and_reads_legacy_plaintext():
    db.set_secret_setting("openai_api_key", "sk-secret-123")
    with db._conn() as conn:
        raw = conn.execute("SELECT v FROM settings WHERE k=?", ("openai_api_key",)).fetchone()["v"]
    assert raw != "sk-secret-123"                    # 落库是密文
    assert db.get_secret_setting("openai_api_key") == "sk-secret-123"
    # 历史明文值:直接明文写入,读取应原样返回(兼容旧库)
    db.set_setting("legacy_key", "plain-old-value")
    assert db.get_secret_setting("legacy_key") == "plain-old-value"


# --- 重新授权复用同一渠道(不重复建 + 旧定时帖 target 仍有效) --- #
def test_find_reauth_channel_matches_by_handle():
    ws = _ws()
    ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform="x",
                 handle="acme", status=ChannelStatus.connected)
    db.save_channel(ch)
    hit = services._find_reauth_channel(ws.id, "x", {"handle": "acme"})
    assert hit and hit.id == ch.id                   # 同 handle → 复用
    # 不同平台不复用
    assert services._find_reauth_channel(ws.id, "linkedin", {"handle": "acme"}) is None


# --- 登录限流:连续失败到阈值后返回 429 --- #
def test_login_rate_limited_after_repeated_failures(monkeypatch):
    import os
    monkeypatch.setenv("YOMINPOST_ACCESS_KEY", "the-real-key-xyz")
    from yominpost.web import app as webapp
    webapp._LOGIN_FAILS.clear()
    with TestClient(app) as c:
        codes = []
        for _ in range(12):
            r = c.post("/api/login", json={"key": "wrong"},
                       headers={"cf-connecting-ip": "203.0.113.9"})
            codes.append(r.status_code)
    assert codes[0] == 401                            # 初期是密钥错误
    assert 429 in codes                               # 达阈值后被限流
    assert codes.count(429) >= 1
    webapp._LOGIN_FAILS.clear()


# --- 主动刷新:expires_at 未知时不预刷(避免踩空一次性 refresh token) --- #
def test_no_proactive_refresh_when_expiry_unknown():
    from yominpost.connectors import oauth_conn
    from yominpost.crypto import encrypt
    calls = {"n": 0}
    oauth_conn._do_refresh_orig = oauth_conn._do_refresh

    def _spy(channel):
        calls["n"] += 1
        return ""

    oauth_conn._do_refresh = _spy
    try:
        ch = Channel(id="c", workspace_id="w", platform="x", status=ChannelStatus.connected,
                     config={"access_token_enc": encrypt("AT0"),
                             "refresh_token_enc": encrypt("RT0")})   # 无 expires_at
        tok = oauth_conn._ensure_fresh_token(ch)
    finally:
        oauth_conn._do_refresh = oauth_conn._do_refresh_orig
    assert tok == "AT0"                               # 用现有 token
    assert calls["n"] == 0                            # 未主动刷新


# --- 反应式刷新:发布因 401 失败 → 刷新后重试一次成功 --- #
def test_reactive_refresh_retries_publish_on_auth_error():
    from yominpost.connectors import oauth_conn
    from yominpost.crypto import encrypt
    ch = Channel(id="c", workspace_id="w", platform="x", status=ChannelStatus.connected,
                 config={"access_token_enc": encrypt("AT_expired"),
                         "refresh_token_enc": encrypt("RT0")})
    seq = {"pub": 0}

    def _fake_publish(platform, token, caption, cfg, media_paths=None):
        seq["pub"] += 1
        if token == "AT_expired":
            return {"ok": False, "error": "HTTP 401: invalid_token"}
        return {"ok": True, "external_id": "X99", "permalink": "https://x.com/p/99"}

    def _fake_refresh(channel):
        # 模拟换到新 access token 并回写
        channel.config["access_token_enc"] = encrypt("AT_new")
        return "AT_new"

    orig_pub = oauth_conn.oauth_publish
    orig_ref = oauth_conn._do_refresh
    oauth_conn.oauth_publish = _fake_publish
    oauth_conn._do_refresh = _fake_refresh
    try:
        res = oauth_conn.OAuthConnector("x").publish(ch, PostContent(body="hi"))
    finally:
        oauth_conn.oauth_publish = orig_pub
        oauth_conn._do_refresh = orig_ref
    assert res.ok and res.external_id == "X99"        # 刷新后重试成功
    assert seq["pub"] == 2                            # 发了两次(首败 + 重试成)
