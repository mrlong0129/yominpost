"""真实渠道连接器 —— 真调各平台 API 发布(仅需 token/webhook,无 OAuth)。

覆盖:Telegram(Bot API)/ Discord(Webhook)/ Mastodon(Access Token)/
     Bluesky(App Password / AT Protocol)/ 通用 Webhook。

连接时校验凭据(测试调用),发布时真实 POST 并回填永久链接。
凭据存在 channel.config(仅本地 SQLite)。
"""
from __future__ import annotations

import httpx

from ..crypto import decrypt, encrypt
from ..models import Channel, ChannelStatus, PostContent, PLATFORM_BY_ID
from .base import Connector, PublishResult

TIMEOUT = 20

# 这些 config 字段是凭据,落库前必须 Fernet 加密(与 OAuth 令牌同规格):
# 存成 <field>_enc,读取时解密。历史明文值仍可读(_secret 兜底原文)。
_SECRET_FIELDS = ("bot_token", "webhook_url", "access_token", "app_password", "url")


def _put_secret(channel: Channel, field: str, value: str) -> None:
    """把敏感字段加密写入 config[<field>_enc](并清掉可能残留的明文键)。"""
    channel.config.pop(field, None)
    channel.config[f"{field}_enc"] = encrypt(value or "")


def _get_secret(channel: Channel, field: str) -> str:
    """读敏感字段:优先解密 <field>_enc,回退历史明文 config[field]。"""
    enc = channel.config.get(f"{field}_enc")
    if enc:
        return decrypt(enc)
    return channel.config.get(field, "")


def _len_ok(platform: str, text: str) -> str | None:
    meta = PLATFORM_BY_ID.get(platform)
    if meta and len(text) > meta.max_chars:
        return f"{meta.label} 内容 {len(text)} 字超过上限 {meta.max_chars}"
    if not text.strip():
        return "内容为空"
    return None


class _RealBase(Connector):
    def disconnect(self, channel: Channel) -> Channel:
        channel.status = ChannelStatus.disconnected
        for k in list(channel.config):
            if k not in ("mock",):
                channel.config.pop(k, None)
        return channel

    def validate(self, channel: Channel, content: PostContent) -> list[str]:
        errs = []
        if channel.status != ChannelStatus.connected:
            errs.append(f"{self.platform} 未连接")
        e = _len_ok(self.platform, content.as_text())
        if e:
            errs.append(e)
        return errs


class TelegramConnector(_RealBase):
    platform = "telegram"

    def connect(self, channel: Channel, credentials: dict | None = None) -> Channel:
        cfg = credentials or {}
        token = cfg.get("bot_token", "").strip()
        chat_id = cfg.get("chat_id", "").strip()
        if not token or not chat_id:
            channel.status = ChannelStatus.error
            channel.config["error"] = "需要 bot_token 与 chat_id"
            return channel
        try:
            r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=TIMEOUT)
            data = r.json()
            if not data.get("ok"):
                raise ValueError(data.get("description", "getMe 失败"))
            channel.handle = data["result"].get("username", "bot")
            channel.display_name = channel.display_name or channel.handle
            channel.config["chat_id"] = chat_id
            _put_secret(channel, "bot_token", token)   # 加密存储
            channel.status = ChannelStatus.connected
        except Exception as e:  # noqa: BLE001
            channel.status = ChannelStatus.error
            channel.config["error"] = f"连接失败: {e}"
        return channel

    def publish(self, channel, content, media_paths=None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        token = _get_secret(channel, "bot_token")
        chat_id = channel.config.get("chat_id", "")
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": content.as_text(),
                      "disable_web_page_preview": False},
                timeout=TIMEOUT,
            )
            data = r.json()
            if not data.get("ok"):
                return PublishResult(ok=False, error=data.get("description", "发送失败"))
            mid = data["result"]["message_id"]
            uname = channel.config.get("chat_id", "").lstrip("@")
            link = f"https://t.me/{uname}/{mid}" if not uname.lstrip("-").isdigit() else ""
            return PublishResult(ok=True, external_id=str(mid), permalink=link)
        except Exception as e:  # noqa: BLE001
            return PublishResult(ok=False, error=str(e))


class DiscordConnector(_RealBase):
    platform = "discord"

    def connect(self, channel, credentials=None) -> Channel:
        cfg = credentials or {}
        url = cfg.get("webhook_url", "").strip()
        if not url.startswith("https://"):
            channel.status = ChannelStatus.error
            channel.config["error"] = "需要有效的 webhook_url"
            return channel
        try:
            r = httpx.get(url, timeout=TIMEOUT)  # GET webhook 返回其元信息
            info = r.json()
            channel.handle = info.get("name", "webhook")
            channel.display_name = channel.display_name or channel.handle
            _put_secret(channel, "webhook_url", url)   # 加密存储
            channel.status = ChannelStatus.connected
        except Exception as e:  # noqa: BLE001
            channel.status = ChannelStatus.error
            channel.config["error"] = f"连接失败: {e}"
        return channel

    def publish(self, channel, content, media_paths=None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        url = _get_secret(channel, "webhook_url")
        try:
            r = httpx.post(url + "?wait=true", json={"content": content.as_text()[:1990]},
                           timeout=TIMEOUT)
            if r.status_code >= 300:
                return PublishResult(ok=False, error=f"HTTP {r.status_code}: {r.text[:120]}")
            mid = ""
            try:
                mid = str(r.json().get("id", ""))
            except Exception:  # noqa: BLE001
                pass
            return PublishResult(ok=True, external_id=mid, permalink="")
        except Exception as e:  # noqa: BLE001
            return PublishResult(ok=False, error=str(e))


class MastodonConnector(_RealBase):
    platform = "mastodon"

    def connect(self, channel, credentials=None) -> Channel:
        cfg = credentials or {}
        instance = cfg.get("instance", "").strip().rstrip("/")
        token = cfg.get("access_token", "").strip()
        if not instance.startswith("http") or not token:
            channel.status = ChannelStatus.error
            channel.config["error"] = "需要 instance 与 access_token"
            return channel
        try:
            r = httpx.get(f"{instance}/api/v1/accounts/verify_credentials",
                          headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
            r.raise_for_status()
            acct = r.json()
            channel.handle = acct.get("username", "")
            channel.display_name = channel.display_name or acct.get("display_name", channel.handle)
            channel.config["instance"] = instance
            _put_secret(channel, "access_token", token)   # 加密存储
            channel.status = ChannelStatus.connected
        except Exception as e:  # noqa: BLE001
            channel.status = ChannelStatus.error
            channel.config["error"] = f"连接失败: {e}"
        return channel

    def publish(self, channel, content, media_paths=None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        instance = channel.config.get("instance", "")
        token = _get_secret(channel, "access_token")
        try:
            r = httpx.post(f"{instance}/api/v1/statuses",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"status": content.as_text()}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return PublishResult(ok=True, external_id=str(data.get("id", "")),
                                 permalink=data.get("url", ""))
        except Exception as e:  # noqa: BLE001
            return PublishResult(ok=False, error=str(e))


class BlueskyConnector(_RealBase):
    platform = "bluesky"
    BASE = "https://bsky.social/xrpc"

    def connect(self, channel, credentials=None) -> Channel:
        cfg = credentials or {}
        handle = cfg.get("handle", "").strip()
        pw = cfg.get("app_password", "").strip()
        if not handle or not pw:
            channel.status = ChannelStatus.error
            channel.config["error"] = "需要 handle 与 app_password"
            return channel
        try:
            r = httpx.post(f"{self.BASE}/com.atproto.server.createSession",
                           json={"identifier": handle, "password": pw}, timeout=TIMEOUT)
            r.raise_for_status()
            sess = r.json()
            channel.handle = sess.get("handle", handle)
            channel.display_name = channel.display_name or channel.handle
            channel.config.update({"handle": handle, "did": sess.get("did", "")})
            _put_secret(channel, "app_password", pw)   # 加密存储
            channel.status = ChannelStatus.connected
        except Exception as e:  # noqa: BLE001
            channel.status = ChannelStatus.error
            channel.config["error"] = f"连接失败: {e}"
        return channel

    def publish(self, channel, content, media_paths=None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        cfg = channel.config
        handle = cfg.get("handle", "")
        pw = _get_secret(channel, "app_password")
        try:
            # 每次发布重新建会话拿新 token(简化 token 生命周期管理)
            s = httpx.post(f"{self.BASE}/com.atproto.server.createSession",
                           json={"identifier": handle, "password": pw},
                           timeout=TIMEOUT).json()
            jwt, did = s["accessJwt"], s["did"]
            from datetime import datetime, timezone
            rec = {"$type": "app.bsky.feed.post", "text": content.as_text()[:300],
                   "createdAt": datetime.now(timezone.utc).isoformat()}
            r = httpx.post(f"{self.BASE}/com.atproto.repo.createRecord",
                           headers={"Authorization": f"Bearer {jwt}"},
                           json={"repo": did, "collection": "app.bsky.feed.post", "record": rec},
                           timeout=TIMEOUT)
            r.raise_for_status()
            uri = r.json().get("uri", "")
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            link = f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else ""
            return PublishResult(ok=True, external_id=rkey, permalink=link)
        except Exception as e:  # noqa: BLE001
            return PublishResult(ok=False, error=str(e))


class WebhookConnector(_RealBase):
    platform = "webhook"

    def connect(self, channel, credentials=None) -> Channel:
        url = (credentials or {}).get("url", "").strip()
        if not url.startswith("http"):
            channel.status = ChannelStatus.error
            channel.config["error"] = "需要有效的 url"
            return channel
        _put_secret(channel, "url", url)   # 加密存储(URL 可能含 token)
        channel.handle = url.split("//", 1)[-1][:30]
        channel.display_name = channel.display_name or "Webhook"
        channel.status = ChannelStatus.connected
        return channel

    def publish(self, channel, content, media_paths=None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        url = _get_secret(channel, "url")
        try:
            r = httpx.post(url, json={
                "text": content.as_text(), "hook": content.hook, "body": content.body,
                "cta": content.cta, "hashtags": content.hashtags,
            }, timeout=TIMEOUT)
            ok = r.status_code < 300
            return PublishResult(ok=ok, external_id=str(r.status_code),
                                 permalink=url if ok else "",
                                 error="" if ok else f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            return PublishResult(ok=False, error=str(e))
