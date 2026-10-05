"""YominPost Web 应用:REST API + SPA 静态托管 + 产物(海报/字幕)托管。"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import db, services
from ..config import ARTIFACT_DIR
from ..connectors import get_connector
from ..models import (
    Asset,
    AssetUpdate,
    Channel,
    ChannelConnect,
    ComposeRequest,
    ExternalAssetBatch,
    GenerateRequest,
    PLATFORM_CATALOG,
    Post,
    PostCreate,
    PostStatus,
    PostUpdate,
    Topic,
    TopicStatus,
    WorkspaceCreate,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import os

    from .. import auth, scheduler

    # 公网防呆:配了对外域名(YOMINPOST_PUBLIC_URL)却没设访问密钥 → 全站零鉴权暴露。
    # 宁可拒绝启动也不要静默裸奔。本地开发(无 PUBLIC_URL)不受影响。
    if os.environ.get("YOMINPOST_PUBLIC_URL", "").strip() and not auth.enabled():
        raise RuntimeError(
            "拒绝启动:检测到 YOMINPOST_PUBLIC_URL(公网部署)但未设置 YOMINPOST_ACCESS_KEY。"
            "请在 ~/.yominpost.env 配置一个足够长的随机密钥后再启动。"
        )

    # 数据落在源码树内 = 重装/清理代码会连库带密钥一起删掉(令牌永久无法解密)。
    # 生产(设了 PUBLIC_URL)务必用 YOMINPOST_DATA_DIR 指向树外;这里响亮告警。
    import logging as _logging
    from ..config import DATA_DIR, PACKAGE_DIR
    if str(DATA_DIR).startswith(str(PACKAGE_DIR)):
        _logging.getLogger("yominpost").warning(
            "数据目录位于源码树内(%s)。生产请设 YOMINPOST_DATA_DIR 指向树外(如 ~/.yominpost/data),"
            "否则更新/清理代码会丢失数据库与 .secret.key。", DATA_DIR)

    db.init_db()
    services.seed_defaults()
    run_sched = os.environ.get("YOMINPOST_ENABLE_SCHEDULER", "1") != "0"
    if run_sched:
        scheduler.start(app)
    try:
        yield
    finally:
        if run_sched:
            await scheduler.stop(app)


app = FastAPI(title="YominPost", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 操作员鉴权(设置了 access key 才启用;公网必设)
# --------------------------------------------------------------------------- #
_OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/api/health", "/favicon.ico"}

# 演示用内置 IdP + demo 平台默认在生产关闭(避免暴露/被当真实渠道)。设 =1 开启。
DEMO_ENABLED = os.environ.get("YOMINPOST_ENABLE_OAUTH_DEMO", "") == "1"
_DEMO_PLATFORMS = {"oauthdemo", "demopro"}

# 允许无鉴权公开读取的产物扩展名(仅媒体/字幕);其余(如 .py 脚本)一律 404。
_ART_PUBLIC_EXT = {"svg", "png", "jpg", "jpeg", "webp", "gif", "mp4", "srt"}


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    from .. import auth
    if not auth.enabled():
        return await call_next(request)
    path = request.url.path
    # 生产未开 demo → 演示 IdP 直接 404(不暴露反射式 HTML 端点)
    if not DEMO_ENABLED and path.startswith("/oauthtest"):
        return JSONResponse({"error": "not found"}, status_code=404)
    # 产物(海报/图)必须公网可读:IG/FB 发布时 Meta 服务器会 server-side 抓取 image_url,
    # 带不上运营者 cookie。文件名是 uuid/hex 不可猜,只读 GET 放行(否则 IG 发布必 401 失败)。
    # 但只放行媒体扩展名:绝不无鉴权吐出 .py 等脚本(会泄露本地绝对路径)。
    if (request.method in ("GET", "HEAD") and path.startswith("/artifacts/")
            and path.lower().rsplit(".", 1)[-1] in _ART_PUBLIC_EXT):
        return await call_next(request)
    if path in _OPEN_PATHS or auth.check_cookie(request.cookies.get(auth.COOKIE_NAME, "")):
        return await call_next(request)
    if path.startswith(("/api/", "/oauth", "/connect", "/artifacts", "/oauthtest")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


class LoginBody(BaseModel):
    key: str


# —— 登录限流:公网单密钥站点必须挡暴力破解。进程内每 IP 滑窗计数,超限临时锁定。——
import time as _time  # noqa: E402

_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_MAX_FAILS = 8          # 5 分钟窗口内最多 8 次失败
_LOGIN_WINDOW_SEC = 300


def _client_ip(request: Request) -> str:
    # 走 Cloudflare 隧道:真实访客 IP 在 CF-Connecting-IP / X-Forwarded-For
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


def _login_allowed(ip: str) -> bool:
    now = _time.time()
    hits = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW_SEC]
    if hits:
        _LOGIN_FAILS[ip] = hits
    elif ip in _LOGIN_FAILS:
        del _LOGIN_FAILS[ip]
    if len(_LOGIN_FAILS) > 2000:          # 防 IP 轮换撑爆内存:超阈值清掉过期项
        for k in [k for k, v in _LOGIN_FAILS.items() if not any(now - t < _LOGIN_WINDOW_SEC for t in v)]:
            del _LOGIN_FAILS[k]
    return len(hits) < _LOGIN_MAX_FAILS


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    from .. import auth
    ip = _client_ip(request)
    if not _login_allowed(ip):
        raise HTTPException(429, "尝试过于频繁,请 5 分钟后再试")
    if not auth.check_key(body.key):
        _LOGIN_FAILS.setdefault(ip, []).append(_time.time())
        raise HTTPException(401, "密钥错误")
    _LOGIN_FAILS.pop(ip, None)             # 成功即清零该 IP 的失败计数
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE_NAME, auth.make_cookie(), max_age=auth.MAX_AGE,
                    httponly=True, samesite="lax",
                    secure=str(request.base_url).startswith("https"))
    return resp


@app.post("/api/logout")
def logout():
    from .. import auth
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/login")
def login_page():
    return HTMLResponse("""<!doctype html><meta charset=utf-8><title>YominPost · 登录</title>
    <body style="font-family:'PingFang SC',sans-serif;display:grid;place-items:center;height:96vh;background:#f4f2ee;margin:0">
    <form id="f" style="border:2px solid #14121a;border-radius:14px;padding:28px;background:#fff;box-shadow:4px 4px 0 #14121a;min-width:300px">
      <h2 style="margin:0 0 4px">YominPost</h2><p style="color:#666;margin:0 0 16px">操作员登录</p>
      <input id="k" type="password" placeholder="Access Key" autofocus
        style="width:100%;box-sizing:border-box;border:2px solid #14121a;border-radius:10px;padding:11px;font-size:15px"/>
      <div id="e" style="color:#dc2626;font-size:13px;height:18px;margin:6px 2px"></div>
      <button style="width:100%;border:2px solid #14121a;border-radius:10px;padding:11px;background:#6d28d9;color:#fff;font-weight:800;font-size:15px;cursor:pointer">进入</button>
    </form>
    <script>
    document.getElementById('f').onsubmit = async (ev) => {
      ev.preventDefault();
      const r = await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({key:document.getElementById('k').value})});
      if (r.ok) location.href='/'; else document.getElementById('e').textContent='密钥错误';
    };
    </script></body>""")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _art_url(path: str) -> str:
    if not path:
        return ""
    return f"/artifacts/{Path(path).name}"


def _ws_or_404(ws_id: str):
    ws = db.get_workspace(ws_id)
    if not ws:
        raise HTTPException(404, "workspace not found")
    return ws


def _channel_out(ch: Channel) -> dict:
    from .. import platform_risk
    d = ch.model_dump()
    # 不泄露凭据。区分「连接错误」(鉴权失效,重连可解 → 连接卡标红)与「健康问题」
    # (平台封控,重连无效 → 归账号管理,连接卡不标红)。IA:Connections 只管链接。
    err = ch.config.get("error")
    health_alert = bool(err) and (platform_risk.is_platform_block(str(err))
                                  or platform_risk.frozen_until(ch) is not None)
    conn_err = bool(err) and not health_alert
    d["config"] = {"error": err} if conn_err else {}
    d["has_error"] = conn_err               # 只有真·连接/鉴权错误才让连接卡标红
    d["health_alert"] = health_alert        # 平台封控:连接卡中性提示 → 账号管理
    d["language"] = ch.config.get("language", "auto")   # 渠道输出语言(auto/en/zh)
    # 账号健康摘要(Connections 卡片用;完整 profile 走 /profile 端点)
    prof = (ch.config or {}).get("account_profile") or {}
    if prof:
        d["health"] = {"grade": prof.get("health_grade"), "score": prof.get("health_score"),
                       "checked_at": prof.get("checked_at"),
                       "followers": (prof.get("signals") or {}).get("followers")}
    # 广告链接摘要(有无该渠道广告)
    ad = (ch.config or {}).get("ad_account") or {}
    if ad:
        d["ads"] = {"provider": ad.get("provider"), "status": ad.get("status"),
                    "account_id": ad.get("ad_account_id")}
    return d


def _post_out(p: Post) -> dict:
    d = p.model_dump(mode="json")
    d["visual_url"] = _art_url(p.visual_svg)
    d["video_url"] = _art_url(p.video_srt)
    d["media_urls"] = [_art_url(m) for m in (p.media or [])]
    d["text"] = p.content.as_text()
    # 列表缩略图:本地图优先;本地丢了/账号同步来的用平台图 URL(同步时回填 meta)
    local = next((u for u in d["media_urls"] if u), "") or d["visual_url"]
    d["thumb_url"] = local or (p.meta or {}).get("platform_media_url", "")
    d["synced_from"] = (p.meta or {}).get("synced_from", "")
    return d


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    from .. import auth
    return {"ok": True, "service": "yominpost", "auth_enabled": auth.enabled()}


@app.get("/api/notifications")
def notifications():
    """全局通知,按 kind 分流:health(平台封控→账号管理)vs connection(重连→Connections)。
    前端据此把横幅/红点指向正确的 tab —— 连接卡不再背健康问题。"""
    chans = services.channel_notifications()
    health = [c for c in chans if c.get("kind") == "health"]
    conn = [c for c in chans if c.get("kind") != "health"]
    return {"channel_errors": chans, "count": len(chans),
            "health_alerts": health, "connection_errors": conn,
            "health_count": len(health), "connection_count": len(conn)}


@app.get("/api/platforms")
def platforms():
    cat = PLATFORM_CATALOG if DEMO_ENABLED else [p for p in PLATFORM_CATALOG if p.id not in _DEMO_PLATFORMS]
    return [p.model_dump() for p in cat]


# --------------------------------------------------------------------------- #
# 生成驱动设置(Template / Claude Code / Codex / Anthropic)
# --------------------------------------------------------------------------- #
class SettingsBody(BaseModel):
    provider: str | None = None
    openai_api_key: str | None = None      # 真·GPT 生图用(gpt-image)
    openai_image_model: str | None = None  # gpt-image-1 / gpt-image-2
    jimeng_api_key: str | None = None


@app.get("/api/settings")
def get_settings():
    import os as _os

    from ..config import default_provider_name, provider_availability
    from ..providers.cli_provider import DEFAULT_CLI_TASKS

    current = db.get_setting("provider") or default_provider_name()
    # 只回布尔,不回 key 明文
    return {
        "provider": current,
        "available": provider_availability(),
        "cli_tasks": _os.environ.get("YOMINPOST_CLI_TASKS", DEFAULT_CLI_TASKS),
        "openai_key_set": bool(_os.environ.get("OPENAI_API_KEY", "").strip()
                               or db.get_secret_setting("openai_api_key")),
        "openai_image_model": db.get_setting("openai_image_model") or _os.environ.get(
            "OPENAI_IMAGE_MODEL", "gpt-image-1"),
    }


@app.post("/api/settings")
def set_settings(body: SettingsBody):
    if body.provider:
        db.set_setting("provider", body.provider)
    if body.openai_api_key is not None:
        db.set_secret_setting("openai_api_key", body.openai_api_key.strip())  # 加密存储
    if body.openai_image_model:
        db.set_setting("openai_image_model", body.openai_image_model.strip())
    if body.jimeng_api_key is not None:
        db.set_secret_setting("jimeng_api_key", body.jimeng_api_key.strip())  # 加密存储
    return {"ok": True, "provider": db.get_setting("provider")}


# --------------------------------------------------------------------------- #
# 能力池(workspace 级驱动配置:文本/图像/视频)
# --------------------------------------------------------------------------- #
class CapToggle(BaseModel):
    id: str
    enabled: bool


@app.get("/api/capabilities")
def get_capabilities():
    from .. import capabilities
    return capabilities.pool()


@app.post("/api/capabilities")
def toggle_capability(body: CapToggle):
    from .. import capabilities
    capabilities.set_enabled(body.id, body.enabled)
    return {"ok": True, "pool": capabilities.pool()}


@app.post("/api/settings/test")
def test_driver():
    """用当前驱动真实跑一次文案生成,返回驱动名 / hook / 耗时。"""
    import time

    from ..models import GenerateRequest as _GR

    wss = db.list_workspaces()
    if not wss:
        raise HTTPException(400, "no workspace")
    t0 = time.time()
    r = services.generate_content(
        wss[0], _GR(topic="用一句话介绍你的产品价值", platform="x", optimize=False)
    )
    return {"driver": r["driver"], "hook": r["content"]["hook"],
            "seconds": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces")
def get_workspaces():
    return [w.model_dump() for w in db.list_workspaces()]


@app.post("/api/workspaces")
def post_workspace(body: WorkspaceCreate):
    return db.create_workspace(body.name, body.brand_name).model_dump()


@app.get("/api/workspaces/{ws_id}")
def get_workspace(ws_id: str):
    return _ws_or_404(ws_id).model_dump()


# --------------------------------------------------------------------------- #
# Channels(连接器)
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces/{ws_id}/channels")
def get_channels(ws_id: str):
    _ws_or_404(ws_id)
    return [_channel_out(c) for c in db.list_channels(ws_id)]


@app.post("/api/workspaces/{ws_id}/channels/connect")
def connect_channel(ws_id: str, body: ChannelConnect):
    _ws_or_404(ws_id)
    ch = Channel(id=db.new_id("ch"), workspace_id=ws_id, platform=body.platform,
                 display_name=body.display_name)
    creds = dict(body.config or {})
    creds.setdefault("handle", body.display_name)
    ch = get_connector(body.platform).connect(ch, creds)
    ch.connected_at = db.now_iso()
    db.save_channel(ch)
    services._profile_on_connect(ch)         # 连接即建立账号健康基线(mock/真实一致)
    return _channel_out(db.get_channel(ch.id) or ch)


# ------------------------- OAuth 应用凭据配置 ------------------------- #
class AppCreds(BaseModel):
    client_id: str
    client_secret: str = ""


@app.get("/api/apps/{platform}")
def app_status(platform: str):
    return {"platform": platform, "configured": services.app_configured(platform)}


@app.post("/api/apps/{platform}")
def app_config(platform: str, body: AppCreds):
    services.set_app_creds(platform, body.client_id, body.client_secret)
    return {"ok": True, "configured": True}


# ------------------------- OAuth 授权 / 回调 ------------------------- #
def _base_url(request: Request) -> str:
    # 部署到公网(如 https://post.example.com)时设 YOMINPOST_PUBLIC_URL,
    # OAuth 回调与产物 URL 都会用它,而不是本地 127.0.0.1。
    import os
    pub = os.environ.get("YOMINPOST_PUBLIC_URL", "").strip().rstrip("/")
    return pub or str(request.base_url).rstrip("/")


class OAuthStart(BaseModel):
    instance: str = ""


@app.post("/api/workspaces/{ws_id}/channels/{platform}/oauth/start")
def oauth_start(ws_id: str, platform: str, request: Request, body: OAuthStart = Body(default=OAuthStart())):
    ws = _ws_or_404(ws_id)
    try:
        url = services.start_oauth(ws, platform, _base_url(request), instance=body.instance)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    return {"authorize_url": url}


@app.get("/connect/{platform}")
def connect_redirect(platform: str, request: Request, ws: str = "", instance: str = ""):
    """TryPost 式一键连接:直接 302 跳到平台真实授权页(凭据来自运营者 env / settings)。"""
    wss = db.list_workspaces()
    ws_obj = (db.get_workspace(ws) if ws else None) or (wss[0] if wss else None)
    if not ws_obj:
        raise HTTPException(400, "no workspace")
    try:
        url = services.start_oauth(ws_obj, platform, _base_url(request), instance=instance)
    except Exception as e:  # noqa: BLE001 —— 运营者未配置应用:给出一次性配置指引
        from ..config import ENV_APP_CREDS
        names = ENV_APP_CREDS.get(platform)
        env_hint = (f"设置服务器环境变量 <code>{names[0]}</code> 和 <code>{names[1]}</code>,"
                    "或在 Settings 页填一次。") if names else str(e)
        return HTMLResponse(
            f"""<!doctype html><meta charset=utf-8>
            <body style="font-family:sans-serif;display:grid;place-items:center;height:90vh;background:#f4f2ee">
            <div style="max-width:520px;text-align:center;border:2px solid #14121a;border-radius:14px;
              padding:26px;background:#fff;box-shadow:4px 4px 0 #14121a">
              <h2>需要先配置 {platform} 应用(仅运营者一次)</h2>
              <p style="color:#555">要像 TryPost 一样一键授权,运营者需在各平台注册一个 OAuth 应用,
              回调地址填 <code>{_base_url(request)}/oauth/callback</code>,然后{env_hint}</p>
              <a href="/#settings" style="display:inline-block;padding:10px 18px;border:2px solid #14121a;
                border-radius:10px;background:#6d28d9;color:#fff;font-weight:700;text-decoration:none">去 Settings 配置</a>
            </div></body>""", status_code=400)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h3>授权被拒绝:{error}</h3><a href='/'>返回</a>", status_code=400)
    try:
        ch = services.complete_oauth(state, code, _base_url(request))
        from ..models import ChannelStatus
        if ch.status == ChannelStatus.error:
            warn = (ch.config or {}).get("error", "连接已建立但当前不可发布")
            msg = f"⚠️ {ch.platform} 已授权但不可用:{warn}"
        else:
            msg = f"✅ 已连接 {ch.platform} @{ch.handle or ch.display_name}"
    except Exception as e:  # noqa: BLE001
        msg = f"❌ 连接失败:{e}"
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
        <body style="font-family:sans-serif;display:grid;place-items:center;height:90vh">
        <div style="text-align:center"><h2>{msg}</h2>
        <p>可以关闭本页返回 YominPost。</p>
        <a href="/" style="padding:10px 18px;border:2px solid #14121a;border-radius:10px;
           background:#6d28d9;color:#fff;font-weight:700;text-decoration:none">返回应用</a></div>
        <script>try{{window.opener&&window.opener.postMessage('oauth-done','*');}}catch(e){{}}</script>
        </body>""")


# ------------------------- 内置本地 OAuth 测试 IdP ------------------------- #
# 仅用于端到端验收 oauthdemo 平台;走的是与真实平台完全相同的通用 OAuth 代码路径。
_IDP_CODES: dict[str, dict] = {}
_IDP_TOKENS: dict[str, str] = {}
_IDP_POSTS: list[dict] = []


@app.get("/oauthtest/authorize")
def idp_authorize(redirect_uri: str, state: str = "", code_challenge: str = "",
                  client_id: str = "", scope: str = ""):
    code = secrets.token_urlsafe(10)
    _IDP_CODES[code] = {"challenge": code_challenge}
    approve = f"{redirect_uri}?code={code}&state={state}"
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
        <body style="font-family:sans-serif;display:grid;place-items:center;height:90vh;background:#f4f2ee">
        <div style="text-align:center;border:2px solid #14121a;border-radius:14px;padding:28px;background:#fff;box-shadow:4px 4px 0 #14121a">
          <h2>OAuth Demo 授权</h2>
          <p>应用 <b>{client_id or 'YominPost'}</b> 请求权限:<code>{scope or 'publish'}</code></p>
          <a id="approve" href="{approve}" style="display:inline-block;padding:12px 22px;border:2px solid #14121a;
             border-radius:10px;background:#16a34a;color:#fff;font-weight:800;text-decoration:none">✅ 授权并继续</a>
        </div></body>""")


@app.post("/oauthtest/token")
async def idp_token(request: Request):
    form = await request.form()
    code = form.get("code", "")
    verifier = form.get("code_verifier", "")
    rec = _IDP_CODES.pop(code, None)
    if rec is None:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # 校验 PKCE(S256)
    challenge = rec.get("challenge", "")
    if challenge:
        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if calc != challenge:
            return JSONResponse({"error": "invalid_pkce"}, status_code=400)
    token = secrets.token_urlsafe(16)
    _IDP_TOKENS[token] = "demo_user"
    return {"access_token": token, "refresh_token": secrets.token_urlsafe(12),
            "token_type": "bearer", "expires_in": 3600}


@app.get("/oauthtest/me")
def idp_me(authorization: str = Header(default="")):
    tok = authorization.replace("Bearer ", "").strip()
    if tok not in _IDP_TOKENS:
        raise HTTPException(401, "invalid token")
    return {"handle": "demo_user"}


@app.post("/oauthtest/publish")
def idp_publish(request: Request, body: dict = Body(default={}), authorization: str = Header(default="")):
    tok = authorization.replace("Bearer ", "").strip()
    if tok not in _IDP_TOKENS:
        raise HTTPException(401, "invalid token")
    pid = secrets.token_hex(8)
    _IDP_POSTS.append({"id": pid, "text": body.get("text", ""), "user": _IDP_TOKENS[tok]})
    return {"id": pid, "permalink": f"{_base_url(request)}/oauthtest/post/{pid}"}


@app.post("/oauthtest/delete")
def idp_delete(body: dict = Body(default={}), authorization: str = Header(default="")):
    tok = authorization.replace("Bearer ", "").strip()
    if tok not in _IDP_TOKENS:
        raise HTTPException(401, "invalid token")
    pid = body.get("id", "")
    before = len(_IDP_POSTS)
    _IDP_POSTS[:] = [p for p in _IDP_POSTS if p["id"] != pid]
    return {"ok": len(_IDP_POSTS) < before}


@app.get("/oauthtest/posts")
def idp_posts():
    return _IDP_POSTS


@app.post("/api/channels/{ch_id}/disconnect")
def disconnect_channel(ch_id: str):
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    ch = get_connector(ch.platform).disconnect(ch)
    db.save_channel(ch)
    return _channel_out(ch)


class ChannelUpdate(BaseModel):
    display_name: str | None = None
    language: str | None = None      # auto | en | zh — 该渠道的内容输出语言
    positioning: str | None = None   # 定位:漏斗阶段(awareness/consideration/conversion/engagement/advocacy)
    play_state: str | None = None    # 打法状态:warmup/growth/maintenance/paused


@app.patch("/api/channels/{ch_id}")
def update_channel(ch_id: str, body: ChannelUpdate):
    """更新渠道偏好(显示名/输出语言/定位/打法状态)。凭据不经此口。"""
    from .. import platform_health
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    if body.display_name is not None:
        ch.display_name = body.display_name
    if body.language is not None:
        if body.language not in ("auto", "en", "zh"):
            raise HTTPException(400, "language 只支持 auto/en/zh")
        ch.config["language"] = body.language
    if body.positioning is not None:
        if body.positioning not in platform_health.POSITIONING:
            raise HTTPException(400, "positioning 非法")
        ch.config["positioning"] = body.positioning
    if body.play_state is not None:
        if body.play_state not in platform_health.PLAY_STATE:
            raise HTTPException(400, "play_state 非法")
        ch.config["play_state"] = body.play_state
    db.save_channel(ch)
    return _channel_out(ch)


@app.get("/api/channel-taxonomy")
def channel_taxonomy():
    """定位/打法状态的可选项(前端下拉用)。"""
    from .. import platform_health
    return {"positioning": platform_health.POSITIONING, "play_state": platform_health.PLAY_STATE}


@app.delete("/api/channels/{ch_id}")
def remove_channel(ch_id: str):
    db.delete_channel(ch_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 账号情报(健康度 + 背景;连接时采集、定期刷新)
# --------------------------------------------------------------------------- #
@app.get("/api/channels/{ch_id}/profile")
def get_channel_profile(ch_id: str):
    """读取账号健康画像(缓存)。没有则触发一次规则版快速刷新(不烧 LLM)。"""
    from .. import account_intel
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    prof = account_intel.get_profile(ch)
    if not prof:
        prof = account_intel.refresh_profile(ch, use_llm=False)
    return prof


@app.post("/api/channels/{ch_id}/profile/refresh")
def refresh_channel_profile(ch_id: str):
    """手动刷新账号画像(用 LLM 叙述)。熔断渠道内部会跳过联网、走缓存/DB。"""
    from .. import account_intel
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    return account_intel.refresh_profile(ch, use_llm=True)


# --------------------------------------------------------------------------- #
# 运营状态总览(渠道/发布/数据/广告 在账号维度聚合)
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces/{ws_id}/operations")
def workspace_operations(ws_id: str):
    from .. import operations
    ws = _ws_or_404(ws_id)
    return operations.workspace_operations(ws)


# --------------------------------------------------------------------------- #
# 广告账户链接(AIGC → GTM):把广告账户接到社媒渠道
# --------------------------------------------------------------------------- #
class AdLinkBody(BaseModel):
    ad_account_id: str
    provider: str | None = None


@app.get("/api/channels/{ch_id}/ads/status")
def channel_ads_status(ch_id: str):
    from .. import ads_link
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    return ads_link.status(ch)


@app.post("/api/channels/{ch_id}/ads/link")
def channel_ads_link(ch_id: str, body: AdLinkBody):
    from .. import ads_link
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    try:
        return ads_link.link(ch, body.ad_account_id, body.provider)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/channels/{ch_id}/ads/link")
def channel_ads_unlink(ch_id: str):
    from .. import ads_link
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    ads_link.unlink(ch)
    return {"ok": True}


@app.get("/api/channels/{ch_id}/ads/report")
def channel_ads_report(ch_id: str):
    from .. import ads_link
    ch = db.get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    return ads_link.report(ch)


# --------------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------------- #
class TopicBody(BaseModel):
    title: str
    angle: str = ""
    pillar: str = ""
    priority: float = 0.5


@app.get("/api/workspaces/{ws_id}/topics")
def get_topics(ws_id: str):
    _ws_or_404(ws_id)
    return [t.model_dump() for t in db.list_topics(ws_id)]


@app.post("/api/workspaces/{ws_id}/topics")
def post_topic(ws_id: str, body: TopicBody):
    _ws_or_404(ws_id)
    t = Topic(id=db.new_id("topic"), workspace_id=ws_id, title=body.title,
              angle=body.angle, pillar=body.pillar, priority=body.priority,
              status=TopicStatus.idea, created_at=db.now_iso())
    db.save_topic(t)
    return t.model_dump()


@app.delete("/api/topics/{topic_id}")
def remove_topic(topic_id: str):
    db.delete_topic(topic_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Assets(素材库)
# --------------------------------------------------------------------------- #
def _asset_dict(a: Asset) -> dict:
    """把 Asset 展开为前端字典:补可访问 URL,并派生分类(source)与文件夹(folder)。"""
    d = a.model_dump()
    d["url"] = _art_url(a.path) or a.url
    d["source"] = services.asset_source(a)
    d["folder"] = services.asset_folder(a)
    return d


def _asset_haystack(d: dict) -> str:
    m = d.get("meta") or {}
    return " ".join(str(x) for x in (
        d.get("title"), m.get("summary"), m.get("site"), m.get("channel"),
        d.get("url"), " ".join(m.get("tags") or []))).lower()


@app.get("/api/workspaces/{ws_id}/assets")
def get_assets(ws_id: str, source: str | None = None, folder: str | None = None,
               kind: str | None = None, channel: str | None = None,
               q: str | None = None):
    """列出素材库。可选 ?source / ?folder / ?kind(内容形式) / ?channel(渠道) / ?q(全文搜索) 过滤。"""
    _ws_or_404(ws_id)
    out = [_asset_dict(a) for a in db.list_assets(ws_id)]
    if source:
        out = [d for d in out if d["source"] == source]
    if folder:
        out = [d for d in out
               if d["folder"] == folder or d["folder"].startswith(folder + "/")]
    if kind:
        out = [d for d in out if d.get("kind") == kind]
    if channel:
        out = [d for d in out if (d.get("meta") or {}).get("channel") == channel]
    if q:
        terms = [t for t in q.lower().split() if t]
        out = [d for d in out if all(t in _asset_haystack(d) for t in terms)]
    return out


@app.post("/api/workspaces/{ws_id}/assets/recommend")
def recommend_assets(ws_id: str, body: dict = Body(...)):
    """按一段素材/主题文本,给运营者推荐最相关的库内素材(竞品参考 + 自有图)。"""
    _ws_or_404(ws_id)
    text = str(body.get("text") or "")
    limit = int(body.get("limit") or 8)
    source = body.get("source")            # 可选:只在某来源里推荐
    items = [_asset_dict(a) for a in db.list_assets(ws_id)]
    if source:
        items = [d for d in items if d["source"] == source]
    ranked = services.rank_assets_by_relevance(text, items, limit=limit)
    return {"items": ranked}


@app.get("/api/workspaces/{ws_id}/assets/folders")
def get_asset_folders(ws_id: str):
    """素材库文件夹概览:三大分类计数 + external 下各竞品/渠道子目录计数。"""
    _ws_or_404(ws_id)
    by_source: dict[str, int] = {"published": 0, "uploaded": 0, "external": 0}
    ext_folders: dict[str, int] = {}
    for a in db.list_assets(ws_id):
        src = services.asset_source(a)
        by_source[src] = by_source.get(src, 0) + 1
        if src == "external":
            fld = services.asset_folder(a) or "(未分类)"
            ext_folders[fld] = ext_folders.get(fld, 0) + 1
    return {"sources": by_source, "external_folders": ext_folders,
            "total": sum(by_source.values())}


@app.post("/api/workspaces/{ws_id}/assets/external")
def add_external_assets(ws_id: str, body: ExternalAssetBatch):
    """批量登记『从别处获取』的引用卡(竞品调研等)。按 url 去重,只新增不覆盖。"""
    _ws_or_404(ws_id)
    seen = {a.url for a in db.list_assets(ws_id)
            if services.asset_source(a) == "external" and a.url}
    added, skipped = [], 0
    for it in body.items:
        url = (it.url or "").strip()
        if url and url in seen:
            skipped += 1
            continue
        folder = "/".join(
            x for x in ((it.site or "").strip("/"), (it.channel or "").strip("/")) if x)
        a = db.save_asset(Asset(
            id=db.new_id("asset"), workspace_id=ws_id,
            kind=it.kind or "link", source="external", folder=folder,
            title=it.title or url, url=url, created_at=db.now_iso(),
            meta={"source": "external", "site": it.site, "channel": it.channel,
                  "summary": it.summary, "tags": it.tags, "thumb": it.thumb,
                  "author": it.author, "published_at": it.published_at},
        ))
        if url:
            seen.add(url)
        added.append(a.id)
    return {"added": len(added), "skipped": skipped, "ids": added}


@app.patch("/api/workspaces/{ws_id}/assets/{asset_id}")
def update_asset(ws_id: str, asset_id: str, body: AssetUpdate):
    """整理素材:改所属分类 / 文件夹 / 标题。"""
    _ws_or_404(ws_id)
    a = db.get_asset(asset_id)
    if not a or a.workspace_id != ws_id:
        raise HTTPException(404, "素材不存在")
    if body.source is not None:
        if body.source not in services.ASSET_SOURCES:
            raise HTTPException(400, "source 非法")
        a.source = body.source
    if body.folder is not None:
        a.folder = body.folder
    if body.title is not None:
        a.title = body.title
    db.save_asset(a)
    return _asset_dict(a)


@app.delete("/api/workspaces/{ws_id}/assets/{asset_id}")
def remove_asset(ws_id: str, asset_id: str):
    """从素材库删除一条(引用卡/上传等)。生成海报由发帖对账管理,一般不在此删。"""
    _ws_or_404(ws_id)
    a = db.get_asset(asset_id)
    if not a or a.workspace_id != ws_id:
        raise HTTPException(404, "素材不存在")
    db.delete_asset(asset_id)
    return {"deleted": True}


@app.post("/api/workspaces/{ws_id}/assets/upload")
async def upload_asset(ws_id: str, file: UploadFile = File(...)):
    """上传图片到素材库(供发帖配图)。返回本地路径 + 公网 URL。"""
    import uuid
    _ws_or_404(ws_id)
    ext = (Path(file.filename or "img").suffix.lower() or ".png")
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise HTTPException(400, "仅支持图片")
    name = f"up_{uuid.uuid4().hex[:12]}{ext}"
    dest = Path(ARTIFACT_DIR) / name
    # 流式落盘 + 大小上限(默认 15MB),超限即中止并删除半成品
    max_bytes = int(os.environ.get("YOMINPOST_MAX_UPLOAD_MB", "15")) * 1024 * 1024
    total = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"文件过大(上限 {max_bytes // (1024*1024)}MB)")
                out.write(chunk)
    except HTTPException:
        raise
    a = db.save_asset(Asset(id=db.new_id("asset"), workspace_id=ws_id, kind="image",
                            source="uploaded", title=file.filename or name,
                            path=str(dest), created_at=db.now_iso()))
    return {"id": a.id, "path": str(dest), "url": _art_url(str(dest)), "title": a.title}


# --------------------------------------------------------------------------- #
# 内容生成(8 能力槽)
# --------------------------------------------------------------------------- #
@app.post("/api/workspaces/{ws_id}/generate")
def generate(ws_id: str, body: GenerateRequest):
    ws = _ws_or_404(ws_id)
    result = services.generate_content(ws, body)
    result["visual_url"] = _art_url(result.get("visual_svg", ""))
    result["video_url"] = _art_url(result.get("video_srt", ""))
    return result


def _enrich_drafts(drafts: list[dict]) -> list[dict]:
    """给每条草稿补上可访问的产物 URL(海报/视频)。"""
    for d in drafts:
        d["visual_url"] = d.get("ext_image") or _art_url(d.get("visual_svg", ""))
        d["video_url"] = _art_url(d.get("video_srt", ""))
    return drafts


@app.post("/api/workspaces/{ws_id}/compose")
def compose(ws_id: str, body: ComposeRequest):
    """同步 compose(兼容/测试)。生产 UI 走 /compose/start + 轮询。"""
    ws = _ws_or_404(ws_id)
    result = services.compose(ws, body)
    _enrich_drafts(result.get("drafts", []))
    return result


@app.post("/api/workspaces/{ws_id}/compose/start")
def compose_start(ws_id: str, body: ComposeRequest):
    """启动 Post Creating Loop(后台执行),立即返回 run_id 供轮询。"""
    ws = _ws_or_404(ws_id)
    try:
        return {"run_id": services.start_compose(ws, body)}
    except ValueError as e:                 # 并发上限
        raise HTTPException(429, str(e))


@app.get("/api/workspaces/{ws_id}/compose-runs")
def list_compose_runs(ws_id: str, limit: int = 20):
    """历史生成记录(用于 UI「恢复上次结果」)。"""
    _ws_or_404(ws_id)
    out = []
    for r in db.list_compose_runs(ws_id, limit=limit):
        out.append({"id": r.id, "created_at": r.created_at, "status": r.status,
                    "driver": r.driver, "material_preview": r.material_preview,
                    "fallback": r.fallback, "channels": len(r.drafts)})
    return out


@app.get("/api/compose-runs/{run_id}")
def get_compose_run(run_id: str):
    r = db.get_compose_run(run_id)
    if not r:
        raise HTTPException(404, "compose run not found")
    return {"run_id": r.id, "workspace_id": r.workspace_id, "created_at": r.created_at,
            "status": r.status, "progress": r.progress, "error": r.error,
            "driver": r.driver, "capabilities": r.capabilities,
            "analysis": r.analysis, "trace": r.trace, "fallback": r.fallback,
            "material_preview": r.material_preview, "material_assets": r.material_assets,
            "request": r.request, "drafts": _enrich_drafts(r.drafts)}


# --------------------------------------------------------------------------- #
# Posts
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces/{ws_id}/posts")
def get_posts(ws_id: str, status: str | None = None):
    _ws_or_404(ws_id)
    return [_post_out(p) for p in db.list_posts(ws_id, status)]


@app.post("/api/workspaces/{ws_id}/sync-posts")
def sync_posts(ws_id: str):
    """从已连接的 FB/IG 账号拉取真实已发帖,对账回填 + 导入账号真实内容。"""
    ws = _ws_or_404(ws_id)
    return services.sync_all_channels(ws)


@app.post("/api/workspaces/{ws_id}/channel-sync")
def channel_sync(ws_id: str):
    """渠道数据同步(手动刷新按钮):账号状态画像 + 内容事实一起拉,与每日自动同步同一入口。"""
    ws = _ws_or_404(ws_id)
    return services.sync_channel_data(ws)


@app.post("/api/workspaces/{ws_id}/posts")
def post_post(ws_id: str, body: PostCreate):
    ws = _ws_or_404(ws_id)
    try:
        return _post_out(services.create_post(ws, body))
    except ValueError as e:                 # 非法排期时间等 → 400,别静默错发
        raise HTTPException(400, str(e))


@app.get("/api/posts/{post_id}")
def get_post(post_id: str):
    p = db.get_post(post_id)
    if not p:
        raise HTTPException(404, "post not found")
    return _post_out(p)


@app.patch("/api/posts/{post_id}")
def patch_post(post_id: str, body: PostUpdate):
    p = db.get_post(post_id)
    if not p:
        raise HTTPException(404, "post not found")
    # 正在发布中的帖不接受并发编辑:否则一次 get→改→save 会把 publishing 覆盖回旧态,
    # 让调度器/recovery 误判并可能重复投递。请求方应稍后重试。
    if p.status == PostStatus.publishing:
        raise HTTPException(409, "帖子正在发布中,请稍后再编辑")
    # 已发布帖不可改写:PATCH 只改本地不改平台,会造成「所存≠所发」;且画布全体重写链路
    # (cvHandleAdjust→cvPersistDraft/cvPublish 旧 postId)可静默触达。要改请「复制为草稿」。
    if p.status == PostStatus.posted:
        raise HTTPException(409, "已发布帖不可修改;请用「复制为草稿」另起一版")
    data = body.model_dump(exclude_none=True)
    # 人手编辑过的草稿即独立作品:清 dedupe_key,防止重开画布贴同一素材时
    # cvPersistDraft 的去重逻辑把用户手工精修的内容静默整体覆盖。
    if p.meta and "dedupe_key" in p.meta:
        p.meta = {k: v for k, v in p.meta.items() if k != "dedupe_key"}
    # 绝不允许客户端直接改 status —— 状态迁移只由服务端按 scheduled_at 白名单推导,
    # 否则前端可把 draft 直接 PATCH 成 posted(绕过发布)或把 posted 改回 scheduled(重发)。
    data.pop("status", None)
    # content 用 body 上的强类型 PostContent 对象设置,绝不把 dict setattr 进去
    # (否则 p.content 变成 dict,后续 _post_out 调 p.content.as_text() → AttributeError 500)
    if body.content is not None:
        p.content = body.content
        data.pop("content", None)
    if "channel_ids" in data:
        from ..models import PublishTarget
        # 保留已存在 target 的发布态(status/external_id/permalink),否则编辑一次
        # 就把 posted 句柄抹掉 → 重发/对账全丢,还会二次真发。只新增/删除渠道。
        existing = {t.channel_id: t for t in (p.targets or [])}
        targets = []
        for ch_id in data.pop("channel_ids"):
            if ch_id in existing:
                targets.append(existing[ch_id])
                continue
            ch = db.get_channel(ch_id)
            if ch:
                targets.append(PublishTarget(channel_id=ch.id, platform=ch.platform))
        p.targets = targets
    if "scheduled_at" in data:
        try:
            data["scheduled_at"] = services.normalize_dt(data["scheduled_at"])
        except ValueError as e:
            raise HTTPException(400, str(e))
        # 排期/清空排期时同步 status(镜像 create_post services.py:660),否则:
        #   编辑 draft 点「排期」→ 只写 scheduled_at、status 仍 draft → 永不自动发;
        #   编辑 scheduled 点「存草稿」→ 清空 scheduled_at、status 仍 scheduled → 幽灵定时帖;
        #   failed 点「排期」→ scheduled_at 写入但 status 恒 failed → UI 称已排期却永不发(幽灵)。
        # 白名单:draft/scheduled/failed 间按 scheduled_at 转(重试排期是合法迁移;
        # _publish_targets 幂等跳过已发 target,不会重复投递),posted/publishing 绝不动。
        if "status" not in data and p.status in (PostStatus.draft, PostStatus.scheduled, PostStatus.failed):
            data["status"] = PostStatus.scheduled if data["scheduled_at"] else PostStatus.draft
    for k, v in data.items():
        setattr(p, k, v)
    db.save_post(p)
    return _post_out(p)


@app.get("/api/posts/{post_id}/risk-check")
def post_risk_check(post_id: str):
    """发布前风险预检:逐渠道返回 platform_risk 问题清单(block 会在发布时被硬拦)。"""
    from .. import platform_risk
    p = db.get_post(post_id)
    if not p:
        raise HTTPException(404, "post not found")
    out, has_block = {}, False
    for tgt in (p.targets or []):
        ch = db.get_channel(tgt.channel_id)
        if not ch or tgt.status == "posted":
            continue
        issues = platform_risk.pre_publish_issues(ch, p)
        if issues:
            out[ch.platform] = issues
            has_block = has_block or any(i["level"] == "block" for i in issues)
    return {"issues": out, "has_block": has_block}


@app.post("/api/posts/{post_id}/publish")
def publish(post_id: str):
    p = db.get_post(post_id)
    if not p:
        raise HTTPException(404, "post not found")
    return _post_out(services.publish_post(p))


@app.delete("/api/posts/{post_id}")
def remove_post(post_id: str, force: bool = False):
    p = db.get_post(post_id)
    if not p:
        db.delete_post(post_id)
        return {"ok": True, "platform_deletes": []}
    # 发布中禁止删除(镜像 patch_post 的 409,force 也不豁免):发布线程持有内存中的 Post,
    # 每发完一个 target 就 save_post(INSERT OR REPLACE)——在途删除会被复活,且平台照发已删内容。
    if p.status == PostStatus.publishing:
        raise HTTPException(409, "帖子正在发布中,请等发布结束后再删除")
    # 删本地的同时,把平台上已发布的真帖也删掉(全链路对齐)。
    # force=1:远端删除临时失败时,允许用户强制只删本地记录(前端"仅删本地"入口)。
    return services.delete_post(p, force=force)


@app.post("/api/workspaces/{ws_id}/tick")
def tick(ws_id: str):
    _ws_or_404(ws_id)
    published = services.run_due_posts(ws_id)
    return {"published": [_post_out(p) for p in published]}


@app.get("/api/workspaces/{ws_id}/calendar")
def calendar(ws_id: str):
    _ws_or_404(ws_id)
    # Calendar 是唯一的 post 管理面(周视图+列表):全部状态都返回,展示粒度交给前端筛选。
    out = []
    for p in db.list_posts(ws_id):
        d = _post_out(p)
        # 统一分桶日期:排期 > 已发 > 创建(草稿/失败帖多无排期,用创建日落位)。
        d["cal_date"] = p.scheduled_at or p.posted_at or p.created_at
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 静态托管(放最后,避免遮蔽 API)
# --------------------------------------------------------------------------- #
_ART_MEDIA = {
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".mp4": "video/mp4", ".srt": "text/plain; charset=utf-8",
}


@app.get("/artifacts/{name}")
def artifact(name: str):
    # 防路径穿越:只允许简单文件名
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    # 只服务媒体扩展名:绝不吐出 .py 等脚本(泄露本地路径 / 代码)。
    if name.lower().rsplit(".", 1)[-1] not in _ART_PUBLIC_EXT:
        raise HTTPException(404, "artifact not found")
    path = Path(ARTIFACT_DIR) / name
    if not path.exists():
        raise HTTPException(404, "artifact not found")
    media = _ART_MEDIA.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/")
def index():
    """动态返回首页,给 JS/CSS 附上基于文件 mtime 的版本号,自动破 Cloudflare 缓存。"""
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        raise HTTPException(404, "index not found")
    try:
        v = str(int(max((STATIC_DIR / f).stat().st_mtime for f in ("app.js", "styles.css"))))
    except OSError:
        v = "0"
    html = idx.read_text(encoding="utf-8")
    html = html.replace('href="/styles.css"', f'href="/styles.css?v={v}"')
    html = html.replace('src="/app.js"', f'src="/app.js?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
