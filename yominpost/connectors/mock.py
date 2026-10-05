"""可运行的模拟连接器 —— 打通渠道全链路(离线 / 可验收)。

- connect:生成账号 handle、置为 connected;
- validate:按平台字数上限校验;
- publish:生成外部 id 与永久链接,模拟成功发布。

真实连接器把这里换成各平台 OAuth + API 调用即可,接口不变。
"""
from __future__ import annotations

import uuid

from ..models import Channel, ChannelStatus, PostContent, PLATFORM_BY_ID
from .base import Connector, PublishResult

# 各平台的"永久链接"域名样板
PERMALINK_HOSTS = {
    "x": "https://x.com/{handle}/status/{id}",
    "linkedin": "https://www.linkedin.com/feed/update/{id}",
    "instagram": "https://www.instagram.com/p/{id}",
    "tiktok": "https://www.tiktok.com/@{handle}/video/{id}",
    "youtube": "https://youtube.com/shorts/{id}",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/{id}",
    "threads": "https://www.threads.net/@{handle}/post/{id}",
    "facebook": "https://www.facebook.com/{handle}/posts/{id}",
    "pinterest": "https://www.pinterest.com/pin/{id}",
    "bluesky": "https://bsky.app/profile/{handle}/post/{id}",
    "mastodon": "https://mastodon.social/@{handle}/{id}",
    "telegram": "https://t.me/{handle}/{id}",
    "discord": "https://discord.com/channels/{id}",
}


class MockConnector(Connector):
    def __init__(self, platform: str):
        self.platform = platform

    def connect(self, channel: Channel, credentials: dict | None = None) -> Channel:
        meta = PLATFORM_BY_ID.get(self.platform)
        handle = (credentials or {}).get("handle") or channel.display_name or "yomin_demo"
        handle = handle.lower().replace(" ", "_")
        channel.handle = handle
        channel.display_name = channel.display_name or (meta.label if meta else self.platform)
        channel.status = ChannelStatus.connected
        channel.config.setdefault("scopes", ["read", "write", "publish"])
        channel.config["mock"] = True
        return channel

    def disconnect(self, channel: Channel) -> Channel:
        channel.status = ChannelStatus.disconnected
        channel.config.pop("scopes", None)
        return channel

    def validate(self, channel: Channel, content: PostContent) -> list[str]:
        errors: list[str] = []
        if channel.status != ChannelStatus.connected:
            errors.append(f"{self.platform} 未连接")
        meta = PLATFORM_BY_ID.get(self.platform)
        text = content.as_text()
        if meta and len(text) > meta.max_chars:
            errors.append(f"{meta.label} 内容 {len(text)} 字超过上限 {meta.max_chars}")
        if not text.strip():
            errors.append("内容为空")
        return errors

    def publish(self, channel: Channel, content: PostContent,
                media_paths: list[str] | None = None) -> PublishResult:
        errs = self.validate(channel, content)
        if errs:
            return PublishResult(ok=False, error="; ".join(errs))
        # 模拟发布:不给"真"永久链接(否则误导为已在平台发布),标 sandbox=True。
        ext_id = "sandbox_" + uuid.uuid4().hex[:12]
        return PublishResult(ok=True, external_id=ext_id, permalink="", sandbox=True)

    def delete(self, channel: Channel, external_id: str) -> PublishResult:
        return PublishResult(ok=True)   # 模拟删除成功
