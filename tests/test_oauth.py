"""OAuth 框架单元测试(不发网络):PKCE / authorize URL / 令牌加密 / 应用凭据。"""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from yominpost import crypto, services
from yominpost.oauth_flow import build_authorize_url, gen_pkce, gen_state
from yominpost.oauth_specs import OAUTH_SPECS, resolve_urls


def test_pkce_s256_relationship():
    verifier, challenge = gen_pkce()
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expect
    assert "=" not in challenge


def test_build_authorize_url_has_all_params():
    url = build_authorize_url("https://x.example/auth", client_id="cid",
                              redirect_uri="https://app/cb", scope="a b", state="st",
                              code_challenge="chal", extra={"duration": "permanent"})
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://app/cb"]
    assert q["scope"] == ["a b"]
    assert q["state"] == ["st"]
    assert q["code_challenge"] == ["chal"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["duration"] == ["permanent"]
    assert q["response_type"] == ["code"]


def test_token_encryption_roundtrip():
    enc = crypto.encrypt("super-secret-token")
    assert enc != "super-secret-token"
    assert crypto.decrypt(enc) == "super-secret-token"
    assert crypto.decrypt("") == ""


def test_resolve_urls_instance_and_base():
    a, t = resolve_urls("mastodon", instance="https://mas.to")
    assert a == "https://mas.to/oauth/authorize"
    assert t == "https://mas.to/oauth/token"
    a2, t2 = resolve_urls("oauthdemo", base="http://127.0.0.1:8300")
    assert a2 == "http://127.0.0.1:8300/oauthtest/authorize"


def test_oauth_specs_shape():
    for pid in ("x", "linkedin", "reddit", "mastodon", "oauthdemo"):
        s = OAUTH_SPECS[pid]
        assert s["authorize"] and s["token"] and s["scope"]
    assert OAUTH_SPECS["x"]["pkce"] is True
    assert OAUTH_SPECS["reddit"]["basic"] is True


def test_env_app_creds_enable_one_click(monkeypatch):
    """服务器 env 配好凭据 → 视为已配置(用户无需填任何东西)。"""
    monkeypatch.setenv("X_CLIENT_ID", "env-x-id")
    monkeypatch.setenv("X_CLIENT_SECRET", "env-x-secret")
    cid, csec = services.get_app_creds("x")
    assert cid == "env-x-id" and csec == "env-x-secret"
    assert services.app_configured("x") is True


def test_connect_redirect_is_302_to_provider():
    """GET /connect/{platform} 直接 302 跳到平台授权页(TryPost 式一键)。"""
    import warnings
    from fastapi.testclient import TestClient
    warnings.filterwarnings("ignore")
    from yominpost.web.app import app
    with TestClient(app) as c:
        ws = c.get("/api/workspaces").json()[0]
        r = c.get(f"/connect/oauthdemo?ws={ws['id']}", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "/oauthtest/authorize" in r.headers["location"]
        assert "code_challenge" in r.headers["location"]  # PKCE


def test_new_oauth_platforms_wired():
    from yominpost.models import PLATFORM_BY_ID
    from yominpost.oauth_specs import OAUTH_SPECS
    for pid in ("facebook", "instagram", "youtube", "discord"):
        assert PLATFORM_BY_ID[pid].auth == "oauth", pid
        assert PLATFORM_BY_ID[pid].needs_app is True
        assert pid in OAUTH_SPECS
    assert "webhook.incoming" in OAUTH_SPECS["discord"]["scope"]
    assert "youtube.upload" in OAUTH_SPECS["youtube"]["scope"]
    # Instagram 直连(Instagram Login)用 instagram_business_* 权限
    assert "instagram_business_content_publish" in OAUTH_SPECS["instagram"]["scope"]
    assert OAUTH_SPECS["instagram"].get("ig_login") is True
    assert "instagram.com/oauth/authorize" in OAUTH_SPECS["instagram"]["authorize"]


def test_connect_redirect_facebook_after_config():
    import warnings
    from fastapi.testclient import TestClient
    warnings.filterwarnings("ignore")
    from yominpost.web.app import app
    with TestClient(app) as c:
        ws = c.get("/api/workspaces").json()[0]
        c.post("/api/apps/facebook", json={"client_id": "fbid", "client_secret": "fbsec"})
        r = c.get(f"/connect/facebook?ws={ws['id']}", follow_redirects=False)
        assert r.status_code in (302, 307)
        loc = r.headers["location"]
        assert "facebook.com/v25.0/dialog/oauth" in loc
        assert "pages_manage_posts" in loc
        assert "client_id=fbid" in loc


def test_mastodon_is_zero_config_oauth():
    from yominpost.models import PLATFORM_BY_ID
    m = PLATFORM_BY_ID["mastodon"]
    assert m.auth == "oauth" and m.needs_app is False
    assert services.app_configured("mastodon") is True


def test_app_creds_roundtrip_and_configured():
    services.set_app_creds("x", "my-client-id", "my-client-secret")
    cid, csec = services.get_app_creds("x")
    assert cid == "my-client-id"
    assert csec == "my-client-secret"
    assert services.app_configured("x") is True
    # oauthdemo 无需手填应用即视为已配置
    assert services.app_configured("oauthdemo") is True
