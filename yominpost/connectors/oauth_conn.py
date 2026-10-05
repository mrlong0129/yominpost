"""OAuth 平台连接器:连接由 Web 的授权回调完成(见 services.complete_oauth),
本连接器负责用已加密存储的 access_token 真实发布。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..crypto import decrypt, encrypt
from ..models import Channel, ChannelStatus, PostContent, PLATFORM_BY_ID
from ..oauth_specs import delete as oauth_delete
from ..oauth_specs import list_recent as oauth_list_recent
from ..oauth_specs import publish as oauth_publish
from .base import Connector, PublishResult

log = logging.getLogger("yominpost.connectors.oauth")

# token 过期前多久就主动刷新(留足时钟偏差 + 发布耗时余量)
_REFRESH_SKEW_SEC = 120

# 发布失败时,哪些错误特征表明是"令牌失效"、值得刷新后重试一次
_AUTH_ERR_HINTS = ("401", "invalid_token", "invalid or expired", "expired",
                   "token has expired", "oauthexception", "unauthorized",
                   "access token", "code\":190")


def _looks_auth_error(err: str) -> bool:
    e = (err or "").lower()
    return any(h in e for h in _AUTH_ERR_HINTS)


# 发布失败归类为「鉴权类」的特征(用于把错误标到渠道上,提示重连)。
# 与 _AUTH_ERR_HINTS 分开:裸 "401" 会误配 id 数字,这里用 "http 401" 等更严格形式。
_PUBLISH_AUTH_HINTS = tuple(h for h in _AUTH_ERR_HINTS if h != "401") + (
    "http 401", "http 403", "令牌缺失或解密失败", "未授权")


def is_auth_error(err: str) -> bool:
    """鉴权失败(令牌失效/未授权 → 重连可解)。与「平台风控封锁」互斥:
    "API access blocked" 之类含 OAuthException 但重连无效的,归 platform_risk.
    is_platform_block,这里明确排除,避免向用户错误地提示"重新连接即可"。"""
    from ..platform_risk import is_platform_block
    if is_platform_block(err):
        return False
    e = (err or "").lower()
    return any(h in e for h in _PUBLISH_AUTH_HINTS)


def _do_refresh(channel: Channel) -> str:
    """真正执行一次 refresh_token 换新令牌,成功则加密回写并落库,返回新明文 token;
    失败(无 refresh / 平台不支持 / 网络)返回 ""。"""
    cfg = channel.config or {}
    refresh = decrypt(cfg.get("refresh_token_enc", ""))
    if not refresh:
        return ""
    try:
        from .. import db, services
        from ..oauth_flow import refresh_token as do_refresh
        from ..oauth_specs import OAUTH_SPECS, resolve_urls
        spec = OAUTH_SPECS.get(channel.platform, {})
        instance = cfg.get("instance", "") or ""
        base = cfg.get("base", "") or ""
        _, token_url = resolve_urls(channel.platform, instance=instance, base=base)
        client_id, client_secret = services.get_app_creds(channel.platform, instance)
        tok = do_refresh(token_url, client_id=client_id, client_secret=client_secret,
                         refresh=refresh, basic_auth=bool(spec.get("basic")))
        new_access = tok.get("access_token", "")
        if not new_access:
            return ""
        cfg["access_token_enc"] = encrypt(new_access)
        if tok.get("refresh_token"):        # 有些平台(如 X)会轮转 refresh token
            cfg["refresh_token_enc"] = encrypt(tok["refresh_token"])
        try:
            ttl = int(float(tok.get("expires_in", 0) or 0))
        except (TypeError, ValueError):
            ttl = 0
        if ttl > 0:
            from datetime import timedelta
            cfg["expires_at"] = (datetime.now(timezone.utc)
                                 + timedelta(seconds=ttl)).isoformat(timespec="seconds")
        channel.config = cfg
        db.save_channel(channel)
        return new_access
    except Exception as e:  # noqa: BLE001
        log.warning("token refresh failed for %s: %s", channel.platform, e)
        return ""


def _ensure_fresh_token(channel: Channel) -> str:
    """发布前"主动"刷新:仅当我们**确知**令牌已接近/超过过期时刻才刷新。

    过期时刻(expires_at)未知时**不预刷**——否则每次发布都刷会白白轮转掉像 X 这类
    一次性 refresh token,并发发布还会互相踩空。未知过期的场景由 publish() 里的
    "收到鉴权失败再反应式刷新重试"兜底,既不早刷也不漏刷。"""
    cfg = channel.config or {}
    cur = decrypt(cfg.get("access_token_enc", ""))
    if not decrypt(cfg.get("refresh_token_enc", "")):
        return cur                          # 无 refresh token → 用现有的
    exp = cfg.get("expires_at", "")
    if not exp:
        return cur                          # 过期时刻未知 → 不预刷,交给反应式重试
    try:
        remaining = (datetime.fromisoformat(exp) - datetime.now(timezone.utc)).total_seconds()
    except Exception:  # noqa: BLE001
        return cur                          # 过期时刻不可解析 → 保持原样
    if remaining > _REFRESH_SKEW_SEC:
        return cur                          # 还早,省调用
    return _do_refresh(channel) or cur


def _decrypt_cfg(channel: Channel) -> tuple[str, dict]:
    """解密 token + 加密的额外字段(page_token 等),返回 (token, cfg 明文)。"""
    token = decrypt(channel.config.get("access_token_enc", ""))
    cfg = dict(channel.config)
    for k in list(cfg):
        if k.endswith("_enc"):
            cfg[k[:-4]] = decrypt(cfg[k])
    return token, cfg


class OAuthConnector(Connector):
    def __init__(self, platform: str):
        self.platform = platform

    def connect(self, channel: Channel, credentials: dict | None = None) -> Channel:
        # OAuth 连接走浏览器授权流程(/api/.../oauth/start -> /oauth/callback)
        channel.status = channel.status or ChannelStatus.disconnected
        return channel

    def disconnect(self, channel: Channel) -> Channel:
        channel.status = ChannelStatus.disconnected
        # 清掉所有加密凭据(access/refresh/page_token/webhook…)与过期标记,
        # 不给断开的渠道在库里留任何可用令牌残留。
        for k in list(channel.config):
            if k.endswith("_enc") or k == "expires_at":
                channel.config.pop(k, None)
        return channel

    def validate(self, channel: Channel, content: PostContent) -> list[str]:
        errs = []
        if channel.status != ChannelStatus.connected or not channel.config.get("access_token_enc"):
            errs.append(f"{self.platform} 未授权")
        meta = PLATFORM_BY_ID.get(self.platform)
        # 校验真正会发出去的整文(as_caption 含 hashtags),而非 as_text —— 否则正文刚好卡上限、
        # 加上标签超限的帖会"通过校验"却被平台静默截断,违背"预览即所见"。
        if meta and meta.max_chars and len(content.as_caption()) > meta.max_chars:
            errs.append(f"{meta.label} 内容(含话题标签)超过上限 {meta.max_chars}")
        return errs

    def publish(self, channel: Channel, content: PostContent,
                media_paths=None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        token = _ensure_fresh_token(channel) or decrypt(channel.config.get("access_token_enc", ""))
        if not token:
            return PublishResult(ok=False, error="令牌缺失或解密失败")
        # 把加密的额外字段(page_token / webhook_url 等)解密成明文供发布使用
        cfg = dict(channel.config)
        for k in list(cfg):
            if k.endswith("_enc"):
                cfg[k[:-4]] = decrypt(cfg[k])

        # Instagram 直发需要一张公开 JPG:优先用已生成的图(gpt-image 海报),
        # 转成 JPG + 公网 URL;没有图才退化渲染文字海报。
        if self.platform == "instagram" and not cfg.get("image_url"):
            try:
                import os as _os
                import uuid as _uuid

                from ..imaging import public_artifact_url, render_jpg_poster, to_jpg
                gen = next((m for m in (media_paths or [])
                            if str(m).lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                            and _os.path.exists(m)), "")
                if gen:
                    cfg["image_url"] = public_artifact_url(to_jpg(gen))   # 用真图
                else:
                    headline = content.hook or (content.body or content.as_text())[:40] or channel.handle
                    stem = f"ig_{channel.id}_{_uuid.uuid4().hex[:8]}"
                    path = render_jpg_poster(headline, channel.handle or "YominPost",
                                             ["#0B3D2E", "#1DB954", "#F8FAFC", "#FACC15"], stem)
                    cfg["image_url"] = public_artifact_url(path)
            except Exception:  # noqa: BLE001
                pass

        # caption 带上话题标签(IG/FB/LinkedIn 等社媒惯例;as_text 不含 hashtags)
        caption = content.as_caption()
        res = oauth_publish(self.platform, token, caption, cfg, media_paths=media_paths)
        # 反应式刷新:发布因令牌失效(401/expired/OAuthException)失败时,用 refresh token
        # 换新令牌重试一次。覆盖那些没有 expires_at 的既有渠道(主动刷新不预刷它们)。
        if not res.get("ok") and _looks_auth_error(res.get("error", "")) \
                and decrypt(channel.config.get("refresh_token_enc", "")):
            new_token = _do_refresh(channel)
            if new_token and new_token != token:
                for k in list(channel.config):     # 重新解密(access_token 已换新)
                    if k.endswith("_enc"):
                        cfg[k[:-4]] = decrypt(channel.config[k])
                res = oauth_publish(self.platform, new_token, caption, cfg, media_paths=media_paths)
        return PublishResult(ok=res.get("ok", False), external_id=res.get("external_id", ""),
                             permalink=res.get("permalink", ""), error=res.get("error", ""))

    def list_recent(self, channel: Channel, limit: int = 25) -> list[dict]:
        """拉取该渠道账号最近已发布的帖子(用于同步)。仅 FB/IG 支持,其余返回 []。"""
        if channel.status != ChannelStatus.connected:
            return []
        token, cfg = _decrypt_cfg(channel)
        if not token:
            return []
        return oauth_list_recent(self.platform, token, cfg, limit=limit)

    def delete(self, channel: Channel, external_id: str) -> PublishResult:
        token = decrypt(channel.config.get("access_token_enc", ""))
        cfg = dict(channel.config)
        for k in list(cfg):
            if k.endswith("_enc"):
                cfg[k[:-4]] = decrypt(cfg[k])
        res = oauth_delete(self.platform, token, external_id, cfg)
        return PublishResult(ok=res.get("ok", False), error=res.get("error", ""),
                             unsupported=res.get("unsupported", False))
