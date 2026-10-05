"""连接器注册表:平台 id -> Connector 实例。

默认全部使用 MockConnector(可运行、可验收)。
接入真实平台时,在这里把对应 platform 换成真实连接器即可,例如:
    _REGISTRY["x"] = XConnector()
"""
from __future__ import annotations

from ..models import PLATFORM_BY_ID
from .base import Connector
from .mock import MockConnector
from .oauth_conn import OAuthConnector
from .real import (
    BlueskyConnector,
    TelegramConnector,
    WebhookConnector,
)

# 默认全部 mock;真实平台替换为对应连接器
_REGISTRY: dict[str, Connector] = {pid: MockConnector(pid) for pid in PLATFORM_BY_ID}
_REGISTRY.update({
    # 真实 OAuth(点 Connect 一键跳授权页)
    "x": OAuthConnector("x"),
    "linkedin": OAuthConnector("linkedin"),
    "reddit": OAuthConnector("reddit"),
    "facebook": OAuthConnector("facebook"),
    "instagram": OAuthConnector("instagram"),
    "youtube": OAuthConnector("youtube"),
    "discord": OAuthConnector("discord"),     # webhook.incoming
    "mastodon": OAuthConnector("mastodon"),   # 自助注册,零配置
    "oauthdemo": OAuthConnector("oauthdemo"),
    "demopro": OAuthConnector("demopro"),     # 需运营者先配凭据(本地沙盒)
    # 真实 token/webhook
    "telegram": TelegramConnector(),
    "bluesky": BlueskyConnector(),
    "webhook": WebhookConnector(),
})


def get_connector(platform: str) -> Connector:
    conn = _REGISTRY.get(platform)
    if conn is None:
        # 未知平台兜底为 mock,保证不崩
        conn = MockConnector(platform)
        _REGISTRY[platform] = conn
    return conn


def register(platform: str, connector: Connector) -> None:
    _REGISTRY[platform] = connector
