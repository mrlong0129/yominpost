"""操作员鉴权:未设 key 时放行;设 key 后未登录拦截、登录后放行。"""
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")


@pytest.fixture()
def app():
    from yominpost.web.app import app as a
    return a


def test_auth_disabled_when_no_key(app, monkeypatch):
    monkeypatch.delenv("YOMINPOST_ACCESS_KEY", raising=False)
    from yominpost import auth
    monkeypatch.setattr(auth.db, "get_setting", lambda *a, **k: None)
    with TestClient(app) as c:
        assert c.get("/api/workspaces").status_code == 200  # 放行


def test_auth_blocks_and_login_flow(app, monkeypatch):
    monkeypatch.setenv("YOMINPOST_ACCESS_KEY", "secret123")
    with TestClient(app) as c:
        # API 未登录 → 401
        assert c.get("/api/workspaces").status_code == 401
        # UI 未登录 → 302 跳 /login
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"
        # /login 与 /api/health 放行
        assert c.get("/login").status_code == 200
        assert c.get("/api/health").status_code == 200
        # 错误密钥 → 401
        assert c.post("/api/login", json={"key": "wrong"}).status_code == 401
        # 正确密钥 → 200 + set-cookie,之后可访问
        ok = c.post("/api/login", json={"key": "secret123"})
        assert ok.status_code == 200
        assert c.get("/api/workspaces").status_code == 200
        # 登出后再访问 → 401
        c.post("/api/logout")
        assert c.get("/api/workspaces").status_code == 401


def test_cookie_signature_and_expiry(monkeypatch):
    monkeypatch.setenv("YOMINPOST_ACCESS_KEY", "k")
    from yominpost import auth
    good = auth.make_cookie()
    assert auth.check_cookie(good) is True
    assert auth.check_cookie("garbage") is False
    assert auth.check_cookie("123.deadbeef") is False
    # 换 key 后旧 cookie 失效
    monkeypatch.setenv("YOMINPOST_ACCESS_KEY", "k2")
    assert auth.check_cookie(good) is False
