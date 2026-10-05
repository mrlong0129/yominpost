"""各平台 OAuth2 规格 + 真实身份获取 / 发帖实现。

参数取自 Postiz 源码(gitroomhq/postiz-app)的各 provider,改写为最小可用形态。
需要用户自有开发者应用凭据的平台:x / linkedin / reddit。
可自助的:mastodon(程序化注册应用)、oauthdemo(内置本地 IdP,用于端到端验收)。
"""
from __future__ import annotations

import json
import os

import httpx

TIMEOUT = 20
UA = "YominPost/0.1"


# spec 字段:authorize/token(可含 {instance} 或 {base} 占位)、scope、pkce、basic、
# extra_auth(附加 authorize 参数)、instance_based、self_register、needs_creds
OAUTH_SPECS: dict[str, dict] = {
    "x": {
        "authorize": "https://x.com/i/oauth2/authorize",
        "token": "https://api.x.com/2/oauth2/token",
        "scope": "tweet.read tweet.write users.read offline.access",
        "pkce": True, "basic": True, "needs_creds": True,
    },
    "linkedin": {
        "authorize": "https://www.linkedin.com/oauth/v2/authorization",
        "token": "https://www.linkedin.com/oauth/v2/accessToken",
        "scope": "openid profile w_member_social",
        "pkce": False, "basic": False, "needs_creds": True,
    },
    "reddit": {
        "authorize": "https://www.reddit.com/api/v1/authorize",
        "token": "https://www.reddit.com/api/v1/access_token",
        "scope": "identity submit", "extra_auth": {"duration": "permanent"},
        "pkce": False, "basic": True, "needs_creds": True,
    },
    "youtube": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": ("https://www.googleapis.com/auth/youtube.upload "
                  "https://www.googleapis.com/auth/youtube.readonly "
                  "https://www.googleapis.com/auth/userinfo.profile"),
        "extra_auth": {"access_type": "offline", "prompt": "consent"},
        "pkce": False, "basic": False, "needs_creds": True,
    },
    "facebook": {
        "authorize": "https://www.facebook.com/v25.0/dialog/oauth",
        "token": "https://graph.facebook.com/v25.0/oauth/access_token",
        # ads_read,ads_management:让同一 Meta token 可读/管广告账户(Marketing API),
        # 支撑 IG+FB 广告链接(AIGC→GTM)。IG 广告也走这个 Meta 商务广告账户。
        "scope": ("pages_show_list,pages_manage_posts,pages_read_engagement,"
                  "business_management,ads_read,ads_management"),
        "pkce": False, "basic": False, "needs_creds": True,
    },
    # Instagram 直连(Instagram API with Instagram Login,参考 TryPost):
    # 不走 Facebook 主页,账号=专业号(创作者/商业)。用独立的 Instagram App ID/Secret。
    "instagram": {
        "authorize": "https://www.instagram.com/oauth/authorize",
        "token": "https://api.instagram.com/oauth/access_token",
        "scope": "instagram_business_basic,instagram_business_content_publish",
        "extra_auth": {"enable_fb_login": "0"},   # 强制纯 IG 登录(否则 token 无法在 graph.instagram.com 换长效)
        "pkce": False, "basic": False, "needs_creds": True, "ig_login": True,
    },
    "discord": {
        "authorize": "https://discord.com/oauth2/authorize",
        "token": "https://discord.com/api/oauth2/token",
        "scope": "webhook.incoming", "pkce": False, "basic": False, "needs_creds": True,
    },
    "mastodon": {
        "authorize": "{instance}/oauth/authorize",
        "token": "{instance}/oauth/token",
        "scope": "read:accounts write:statuses",
        "pkce": False, "basic": False, "instance_based": True, "self_register": True,
        "needs_creds": False,
    },
    "oauthdemo": {
        "authorize": "{base}/oauthtest/authorize",
        "token": "{base}/oauthtest/token",
        "scope": "publish", "pkce": True, "basic": False,
        "self_register": True, "needs_creds": False,
    },
    # 本地"需运营者配置"沙盒:走内置 IdP,但需先在 Settings/env 填 client_id/secret,
    # 用于完整演示「运营者配置 → 用户一键连接」两阶段流程(无需真实外部应用)。
    "demopro": {
        "authorize": "{base}/oauthtest/authorize",
        "token": "{base}/oauthtest/token",
        "scope": "publish", "pkce": True, "basic": False,
        "self_register": False, "needs_creds": True,
    },
}


def resolve_urls(platform: str, *, instance: str = "", base: str = "") -> tuple[str, str]:
    spec = OAUTH_SPECS[platform]
    a, t = spec["authorize"], spec["token"]
    inst = (instance or "https://mastodon.social").rstrip("/")
    a = a.replace("{instance}", inst).replace("{base}", base.rstrip("/"))
    t = t.replace("{instance}", inst).replace("{base}", base.rstrip("/"))
    return a, t


def exchange_long_lived(platform: str, access_token: str,
                        client_id: str, client_secret: str) -> str:
    """把 Facebook/Instagram 的短时(~1h)用户令牌换成长时(~60天)令牌;
    由此派生的主页令牌基本不过期。失败则原样返回。"""
    if platform not in ("facebook", "instagram") or not access_token:
        return access_token
    try:
        r = httpx.get("https://graph.facebook.com/v25.0/oauth/access_token",
                      params={"grant_type": "fb_exchange_token", "client_id": client_id,
                              "client_secret": client_secret,
                              "fb_exchange_token": access_token}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("access_token", access_token)
    except Exception:  # noqa: BLE001
        return access_token


IG_GRAPH = "https://graph.instagram.com/v25.0"


def instagram_exchange(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """Instagram Login:code → 短效令牌(multipart)→ 长效令牌(~60天)。
    返回 {access_token, user_id}。参数与 TryPost/Meta 文档一致(必须 multipart;长效需带 client_id)。"""
    r = httpx.post("https://api.instagram.com/oauth/access_token", files={
        "client_id": (None, client_id),
        "client_secret": (None, client_secret),
        "grant_type": (None, "authorization_code"),
        "redirect_uri": (None, redirect_uri),
        "code": (None, code.split("#", 1)[0]),
    }, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    short = d["access_token"]
    uid = str(d.get("user_id", ""))
    # 换长效令牌
    access = short
    try:
        lr = httpx.get("https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token", "client_id": client_id,
            "client_secret": client_secret, "access_token": short,
        }, timeout=TIMEOUT)
        if lr.status_code < 300:
            access = lr.json().get("access_token", short)
    except Exception:  # noqa: BLE001
        pass
    return {"access_token": access, "user_id": uid}


def mastodon_register_app(instance: str, redirect_uri: str) -> dict:
    """Mastodon 程序化注册应用,返回 {client_id, client_secret}。"""
    r = httpx.post(f"{instance.rstrip('/')}/api/v1/apps", data={
        "client_name": "YominPost",
        "redirect_uris": redirect_uri,
        "scopes": OAUTH_SPECS["mastodon"]["scope"],
        "website": "https://yominpost.local",
    }, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return {"client_id": d["client_id"], "client_secret": d["client_secret"]}


# --------------------------------------------------------------------------- #
# 身份(连接后回填 handle 等)
# --------------------------------------------------------------------------- #
def fetch_identity(platform: str, token: str, cfg: dict, token_response: dict | None = None) -> dict:
    """连接后回填 handle,并为需要的平台返回额外配置(page_token / ig_user_id /
    webhook_url / channel_id 等)。敏感字段由 services 加密入库。"""
    token_response = token_response or {}
    try:
        if platform == "youtube":
            r = httpx.get("https://www.googleapis.com/youtube/v3/channels",
                          params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            items = r.json().get("items", [])
            if items:
                return {"handle": items[0]["snippet"]["title"], "channel_id": items[0]["id"]}
            return {"handle": "youtube"}
        if platform == "facebook":
            # 主页资产授权在回调后可能延迟几秒才生效,重试几次直到拿到主页
            import time as _t
            for _ in range(4):
                r = httpx.get("https://graph.facebook.com/v25.0/me/accounts",
                              params={"fields": "id,name,access_token", "access_token": token},
                              timeout=TIMEOUT)
                pages = r.json().get("data", [])
                if pages:
                    p = pages[0]
                    return {"handle": p.get("name", "page"), "page_id": p["id"],
                            "page_token": p.get("access_token", "")}
                _t.sleep(2)
            return {"handle": "facebook", "_warn": "该账号下未找到可管理的 Facebook 主页"}
        if platform == "instagram":
            # Instagram Login:账号本身即用户对象(graph.instagram.com/me)
            r = httpx.get(f"{IG_GRAPH}/me",
                          params={"fields": "id,username,account_type", "access_token": token},
                          timeout=TIMEOUT)
            d = r.json()
            if d.get("id"):
                return {"handle": d.get("username", "instagram"),
                        "ig_user_id": str(d["id"]), "account_type": d.get("account_type", "")}
            # 授权成功但读不到账号:透传 Meta 真实报错(否则排障只有一句"无法读取")
            api_msg = str((d.get("error") or {}).get("message", "")) or f"HTTP {r.status_code}"
            if "api access blocked" in api_msg.lower():
                # Meta 账号/应用级封锁:重新授权无效,只能在 Meta 侧解除
                return {"handle": "instagram", "_warn": (
                    "Meta 已封锁该账号的 API 访问(API access blocked),重新连接无效。"
                    "请:① 打开 Instagram App 完成安全验证(设置→账户状态);"
                    "② 在 Meta 开发者后台确认应用未被限制;③ 解除后一般 24-48 小时恢复")}
            return {"handle": "instagram", "_warn": f"无法读取 Instagram 账号信息({api_msg})"}
        if platform == "discord":
            wh = (token_response or {}).get("webhook", {})
            return {"handle": wh.get("name", "discord"),
                    "webhook_url": wh.get("url", ""),
                    "channel_id": wh.get("channel_id", "")}
        if platform == "x":
            r = httpx.get("https://api.x.com/2/users/me",
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            u = r.json().get("data", {})
            return {"handle": u.get("username", ""), "name": u.get("name", "")}
        if platform == "linkedin":
            r = httpx.get("https://api.linkedin.com/v2/userinfo",
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            u = r.json()
            return {"handle": u.get("name", ""), "sub": u.get("sub", "")}
        if platform == "reddit":
            r = httpx.get("https://oauth.reddit.com/api/v1/me",
                          headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                          timeout=TIMEOUT)
            return {"handle": r.json().get("name", "")}
        if platform == "mastodon":
            inst = cfg.get("instance", "https://mastodon.social").rstrip("/")
            r = httpx.get(f"{inst}/api/v1/accounts/verify_credentials",
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            return {"handle": r.json().get("username", "")}
        if platform in ("oauthdemo", "demopro"):
            base = cfg.get("base", "")
            r = httpx.get(f"{base}/oauthtest/me",
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            return {"handle": r.json().get("handle", "demo_user")}
    except Exception:  # noqa: BLE001
        pass
    return {"handle": ""}


# --------------------------------------------------------------------------- #
# 发帖(真实 API 调用)
# --------------------------------------------------------------------------- #
def _raster(media_paths):
    """挑出可作为图片上传的本地文件(png/jpg,排除 svg)。"""
    return [m for m in (media_paths or [])
            if str(m).lower().endswith((".png", ".jpg", ".jpeg")) and os.path.exists(m)]


def publish(platform: str, token: str, text: str, cfg: dict,
            media_paths: list[str] | None = None) -> dict:
    """返回 {ok, external_id, permalink, error}。media_paths=本地图片文件(多图)。"""
    try:
        if platform == "x":
            r = httpx.post("https://api.x.com/2/tweets",
                           headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"},
                           json={"text": text[:280]}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            tid = r.json()["data"]["id"]
            h = cfg.get("handle", "i")
            return _ok(tid, f"https://x.com/{h}/status/{tid}")

        if platform == "linkedin":
            sub = cfg.get("sub", "")
            r = httpx.post("https://api.linkedin.com/rest/posts",
                           headers={"Authorization": f"Bearer {token}",
                                    "LinkedIn-Version": "202601",
                                    "X-Restli-Protocol-Version": "2.0.0",
                                    "Content-Type": "application/json"},
                           json={"author": f"urn:li:person:{sub}", "commentary": text,
                                 "visibility": "PUBLIC",
                                 "distribution": {"feedDistribution": "MAIN_FEED",
                                                  "targetEntities": [], "thirdPartyDistributionChannels": []},
                                 "lifecycleState": "PUBLISHED",
                                 "isReshareDisabledByAuthor": False}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            pid = r.headers.get("x-restli-id", "")
            return _ok(pid, f"https://www.linkedin.com/feed/update/{pid}" if pid else "")

        if platform == "reddit":
            sr = cfg.get("subreddit", "").lstrip("r/").strip("/")
            if not sr:
                return {"ok": False, "error": "缺少目标 subreddit(在渠道配置里填 subreddit)"}
            r = httpx.post("https://oauth.reddit.com/api/submit",
                           headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                           data={"api_type": "json", "kind": "self", "sr": sr,
                                 "title": (cfg.get("title") or text[:280]), "text": text},
                           timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            j = r.json().get("json", {})
            if j.get("errors"):
                return {"ok": False, "error": str(j["errors"])}
            data = j.get("data", {})
            return _ok(data.get("id", ""), data.get("url", ""))

        if platform == "mastodon":
            inst = cfg.get("instance", "https://mastodon.social").rstrip("/")
            r = httpx.post(f"{inst}/api/v1/statuses",
                           headers={"Authorization": f"Bearer {token}"},
                           data={"status": text, "visibility": "public"}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            d = r.json()
            return _ok(str(d.get("id", "")), d.get("url", ""))

        if platform == "facebook":
            page_id = cfg.get("page_id", "")
            page_token = cfg.get("page_token", "")
            if not page_id or not page_token:
                return {"ok": False, "error": "缺少 Facebook 主页授权(page token)"}
            base = f"https://graph.facebook.com/v25.0/{page_id}"
            imgs = _raster(media_paths)
            if imgs:
                # 多图帖:各图先 unpublished 上传,再 /feed 带 attached_media(保序)
                media_ids = []
                for p in imgs:
                    with open(p, "rb") as f:
                        rr = httpx.post(f"{base}/photos",
                                        data={"published": "false", "access_token": page_token},
                                        files={"source": f}, timeout=90)
                    if rr.status_code >= 300:
                        return _err(rr)
                    media_ids.append(rr.json()["id"])
                data = {"message": text, "access_token": page_token}
                for i, mid in enumerate(media_ids):
                    data[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
                r = httpx.post(f"{base}/feed", data=data, timeout=90)
            else:
                r = httpx.post(f"{base}/feed",
                               data={"message": text, "access_token": page_token}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            pid = r.json().get("id", "")
            return _ok(pid, f"https://www.facebook.com/{pid}")

        if platform == "discord":
            url = cfg.get("webhook_url", "")
            if not url:
                return {"ok": False, "error": "缺少 Discord webhook(重新授权)"}
            r = httpx.post(url + "?wait=true", json={"content": text[:1990]}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            mid = ""
            try:
                mid = str(r.json().get("id", ""))
            except Exception:  # noqa: BLE001
                pass
            return _ok(mid, "")

        if platform == "instagram":
            import time as _t
            ig_id = cfg.get("ig_user_id", "")
            image_url = cfg.get("image_url", "")   # 公开 JPG 海报,由连接器渲染并填入
            if not ig_id:
                return {"ok": False, "error": "缺少 Instagram 账号 id(请重新连接)"}
            if not image_url.startswith("http"):
                return {"ok": False, "error": "Instagram 需要一张公开图片,当前无可用海报 URL"}
            # 1) 建容器
            c = httpx.post(f"{IG_GRAPH}/{ig_id}/media",
                           data={"image_url": image_url, "caption": text, "access_token": token},
                           timeout=TIMEOUT)
            if c.status_code >= 300:
                return _err(c)
            cid = c.json().get("id", "")
            # 2) 轮询处理状态
            for _ in range(20):
                s = httpx.get(f"{IG_GRAPH}/{cid}",
                              params={"fields": "status_code", "access_token": token}, timeout=TIMEOUT)
                if s.json().get("status_code") == "FINISHED":
                    break
                _t.sleep(3)
            # 3) 发布
            p = httpx.post(f"{IG_GRAPH}/{ig_id}/media_publish",
                           data={"creation_id": cid, "access_token": token}, timeout=TIMEOUT)
            if p.status_code >= 300:
                return _err(p)
            mid = p.json().get("id", "")
            perm = ""
            try:
                perm = httpx.get(f"{IG_GRAPH}/{mid}",
                                 params={"fields": "permalink", "access_token": token},
                                 timeout=TIMEOUT).json().get("permalink", "")
            except Exception:  # noqa: BLE001
                pass
            return _ok(mid, perm)

        if platform == "youtube":
            return {"ok": False, "error": "YouTube 发布=上传视频文件,当前内容为文案/海报,"
                    "暂不支持(连接与授权已就绪)"}

        if platform in ("oauthdemo", "demopro"):
            base = cfg.get("base", "")
            r = httpx.post(f"{base}/oauthtest/publish",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"text": text}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return _err(r)
            d = r.json()
            return _ok(d.get("id", ""), d.get("permalink", ""))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"不支持的 OAuth 平台: {platform}"}


def list_recent(platform: str, token: str, cfg: dict, limit: int = 25) -> list[dict]:
    """从平台拉取该账号最近已发布的帖子,归一化返回。失败返回 []。
    每条:{external_id, permalink, text, media_url, media_type, created_at}。"""
    try:
        if platform == "facebook":
            page_id = cfg.get("page_id", "")
            page_token = cfg.get("page_token", "") or token
            if not page_id or not page_token:
                return []
            r = httpx.get(f"https://graph.facebook.com/v25.0/{page_id}/published_posts",
                          params={"fields": "id,message,story,created_time,full_picture,permalink_url",
                                  "limit": limit, "access_token": page_token}, timeout=TIMEOUT)
            if r.status_code >= 300:
                return []
            out = []
            for p in r.json().get("data", []):
                out.append({"external_id": str(p.get("id", "")),
                            "permalink": p.get("permalink_url", ""),
                            "text": p.get("message") or p.get("story", ""),
                            "media_url": p.get("full_picture", ""),
                            "media_type": "image", "created_at": p.get("created_time", "")})
            return out

        if platform == "instagram":
            ig_id = cfg.get("ig_user_id", "")
            if not ig_id:
                return []
            r = httpx.get(f"{IG_GRAPH}/{ig_id}/media",
                          params={"fields": "id,caption,media_url,thumbnail_url,permalink,"
                                  "timestamp,media_type", "limit": limit, "access_token": token},
                          timeout=TIMEOUT)
            if r.status_code >= 300:
                return []
            out = []
            for p in r.json().get("data", []):
                out.append({"external_id": str(p.get("id", "")),
                            "permalink": p.get("permalink", ""),
                            "text": p.get("caption", ""),
                            "media_url": p.get("thumbnail_url") or p.get("media_url", ""),
                            "media_type": (p.get("media_type", "") or "").lower(),
                            "created_at": p.get("timestamp", "")})
            return out
    except Exception:  # noqa: BLE001
        return []
    return []


def _ok(ext_id: str, permalink: str) -> dict:
    return {"ok": True, "external_id": str(ext_id), "permalink": permalink, "error": ""}


def _err(r) -> dict:
    return {"ok": False, "external_id": "", "permalink": "",
            "error": f"HTTP {r.status_code}: {r.text[:160]}"}


# --------------------------------------------------------------------------- #
# 删除已发布内容(真实 API 调用)—— 与发布对齐
# --------------------------------------------------------------------------- #
def delete(platform: str, token: str, external_id: str, cfg: dict) -> dict:
    """删除平台上的已发布内容。返回 {ok, error}。"""
    if not external_id:
        return {"ok": False, "error": "无外部 id,无法删除"}
    try:
        if platform == "facebook":
            page_token = cfg.get("page_token") or token
            r = httpx.delete(f"https://graph.facebook.com/v25.0/{external_id}",
                             params={"access_token": page_token}, timeout=TIMEOUT)
            return {"ok": r.status_code < 300} if r.status_code < 300 else _errd(r)
        if platform == "x":
            r = httpx.delete(f"https://api.x.com/2/tweets/{external_id}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            return {"ok": True} if r.status_code < 300 else _errd(r)
        if platform == "mastodon":
            inst = cfg.get("instance", "https://mastodon.social").rstrip("/")
            r = httpx.delete(f"{inst}/api/v1/statuses/{external_id}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            return {"ok": True} if r.status_code < 300 else _errd(r)
        if platform == "reddit":
            fullname = external_id if external_id.startswith("t3_") else f"t3_{external_id}"
            r = httpx.post("https://oauth.reddit.com/api/del",
                           headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
                           data={"id": fullname}, timeout=TIMEOUT)
            return {"ok": True} if r.status_code < 300 else _errd(r)
        if platform == "linkedin":
            from urllib.parse import quote
            r = httpx.delete(f"https://api.linkedin.com/rest/posts/{quote(external_id, safe='')}",
                             headers={"Authorization": f"Bearer {token}",
                                      "LinkedIn-Version": "202601",
                                      "X-Restli-Protocol-Version": "2.0.0"}, timeout=TIMEOUT)
            return {"ok": True} if r.status_code < 300 else _errd(r)
        if platform == "discord":
            url = cfg.get("webhook_url", "")
            if not url:
                return {"ok": False, "error": "缺少 webhook,无法删除"}
            r = httpx.delete(f"{url}/messages/{external_id}", timeout=TIMEOUT)
            return {"ok": True} if r.status_code < 300 else _errd(r)
        if platform in ("oauthdemo", "demopro"):
            base = cfg.get("base", "")
            r = httpx.post(f"{base}/oauthtest/delete",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"id": external_id}, timeout=TIMEOUT)
            return {"ok": r.status_code < 300}
        if platform == "instagram":
            return {"ok": False, "unsupported": True,
                    "error": "Instagram API 不支持删除已发布内容(请到 App 手动删)"}
        if platform == "youtube":
            return {"ok": False, "unsupported": True, "error": "YouTube 删除未接入"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": False, "unsupported": True, "error": f"不支持删除: {platform}"}


def _errd(r) -> dict:
    return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:160]}"}
