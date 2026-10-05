"""渠道连接器接口 —— 覆盖「连接 → 授权 → 发布 → 删除」全链路。

真实平台连接器(X / LinkedIn / Instagram …)只要实现这个接口即可接入,
无需改动上层业务。本仓库提供一个可运行的 MockConnector 打通全链路,
方便离线开发、演示与自动化验收。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

from ..models import Channel, PostContent


class PublishResult(BaseModel):
    ok: bool
    external_id: str = ""
    permalink: str = ""
    error: str = ""
    sandbox: bool = False       # True=模拟/未真实发布(mock 连接器),不是平台上的真帖
    unsupported: bool = False   # True=该平台结构上不支持此操作(如 IG/YT 删除),非临时失败


class Connector(ABC):
    platform: str = "base"

    @abstractmethod
    def connect(self, channel: Channel, credentials: dict | None = None) -> Channel:
        """完成授权,回填 handle / display_name / status=connected。"""

    @abstractmethod
    def disconnect(self, channel: Channel) -> Channel:
        """撤销授权。"""

    @abstractmethod
    def publish(self, channel: Channel, content: PostContent,
                media_paths: list[str] | None = None) -> PublishResult:
        """把内容发布到该渠道,返回外部 id 与永久链接。"""

    def validate(self, channel: Channel, content: PostContent) -> list[str]:
        """发布前校验(字数 / 媒体要求等),返回错误列表(空=通过)。"""
        return []

    def delete(self, channel: Channel, external_id: str) -> PublishResult:
        """删除该渠道上已发布的内容(external_id)。默认不支持,由具体连接器实现。"""
        return PublishResult(ok=False, unsupported=True, error="该渠道暂不支持删除已发布内容")

    def list_recent(self, channel: Channel, limit: int = 25) -> list[dict]:
        """拉取该渠道账号最近已发布的帖子(用于同步)。默认不支持,返回 []。"""
        return []
