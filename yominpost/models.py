"""产品实体模型(TryPost 式结构)。

Workspace ─┬─ Channel   (渠道连接器实例:连接/发布全链路)
           ├─ Post      (草稿 / 定时 / 已发,核心对象)
           ├─ Asset     (素材库)
           └─ Topic     (选题库,可由 ② ideation 灌入)

内容生成沿用 8 能力槽流水线(schemas / orchestrator)。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .schemas import ContentFormat, Platform


# --------------------------------------------------------------------------- #
# 平台目录(对应「Social Accounts」截图)
# --------------------------------------------------------------------------- #
class ConnectField(BaseModel):
    key: str
    label: str
    type: str = "text"          # text | password
    placeholder: str = ""


class PlatformMeta(BaseModel):
    id: str
    label: str
    color: str
    kind: str = "social"       # social | video | messaging | ads
    category: str = "social_media"  # social_media | ads —— Connections 分组
    max_chars: int = 2200
    supports_media: bool = True
    real: bool = False          # True=真实可发(连接器真调 API)
    auth: str = "mock"          # mock | credentials(token/webhook) | oauth | connector
    needs_app: bool = False     # oauth 平台是否需要用户自有开发者应用凭据
    connect_fields: list[ConnectField] = Field(default_factory=list)


# 真实可发平台(仅需 token/webhook,无 OAuth)所需的连接凭据
_TG = [ConnectField(key="bot_token", label="Bot Token", type="password", placeholder="123456:ABC-..."),
       ConnectField(key="chat_id", label="Chat ID", placeholder="@channel 或 数字 id")]
_DISCORD = [ConnectField(key="webhook_url", label="Webhook URL", type="password",
                         placeholder="https://discord.com/api/webhooks/...")]
_WEBHOOK = [ConnectField(key="url", label="Webhook URL", placeholder="https://your.endpoint/hook")]
_MASTODON = [ConnectField(key="instance", label="实例地址", placeholder="https://mastodon.social")]
_BLUESKY = [ConnectField(key="handle", label="Handle", placeholder="you.bsky.social"),
            ConnectField(key="app_password", label="App Password", type="password")]

PLATFORM_CATALOG: list[PlatformMeta] = [
    # —— 真实 OAuth 授权(需用户自有开发者应用) —— #
    PlatformMeta(id="x", label="X", color="#111827", max_chars=280,
                 real=True, auth="oauth", needs_app=True),
    PlatformMeta(id="linkedin", label="LinkedIn", color="#0A66C2", max_chars=3000,
                 real=True, auth="oauth", needs_app=True),
    PlatformMeta(id="reddit", label="Reddit", color="#FF4500", max_chars=40000,
                 real=True, auth="oauth", needs_app=True),
    # —— 真实 OAuth,自助(无需手填应用) —— #
    PlatformMeta(id="oauthdemo", label="OAuth Demo", color="#6d28d9", max_chars=5000,
                 real=True, auth="oauth", needs_app=False),
    # —— 本地沙盒:需运营者先配凭据(演示两阶段流程) —— #
    PlatformMeta(id="demopro", label="Demo(需配置)", color="#9333ea", max_chars=5000,
                 real=True, auth="oauth", needs_app=True),
    # —— 真实 OAuth(需自有开发者应用) —— #
    PlatformMeta(id="facebook", label="Facebook Page", color="#1877F2", max_chars=5000,
                 real=True, auth="oauth", needs_app=True),
    PlatformMeta(id="instagram", label="Instagram", color="#E1306C", max_chars=2200,
                 real=True, auth="oauth", needs_app=True),
    PlatformMeta(id="youtube", label="YouTube Shorts", color="#FF0000", kind="video",
                 max_chars=5000, real=True, auth="oauth", needs_app=True),
    # —— 模拟(尚未接入真实应用) —— #
    PlatformMeta(id="tiktok", label="TikTok", color="#010101", kind="video", max_chars=2200),
    PlatformMeta(id="xiaohongshu", label="小红书", color="#FF2442", max_chars=1000),
    PlatformMeta(id="threads", label="Threads", color="#000000", max_chars=500),
    PlatformMeta(id="pinterest", label="Pinterest", color="#E60023", max_chars=500),
    # —— 真实可发(token/webhook 即可,无 OAuth) —— #
    PlatformMeta(id="telegram", label="Telegram", color="#26A5E4", kind="messaging",
                 max_chars=4096, real=True, auth="credentials", connect_fields=_TG),
    # Discord:真实 OAuth(webhook.incoming,授权时选服务器/频道即自动建 webhook)
    PlatformMeta(id="discord", label="Discord", color="#5865F2", kind="messaging",
                 max_chars=2000, real=True, auth="oauth", needs_app=True),
    # Mastodon:真实 OAuth,自助注册应用(只需实例地址,零配置一键)
    PlatformMeta(id="mastodon", label="Mastodon", color="#6364FF", max_chars=500,
                 real=True, auth="oauth", needs_app=False, connect_fields=_MASTODON),
    PlatformMeta(id="bluesky", label="Bluesky", color="#0085FF", max_chars=300,
                 real=True, auth="credentials", connect_fields=_BLUESKY),
    PlatformMeta(id="webhook", label="Webhook", color="#334155", kind="messaging",
                 max_chars=20000, real=True, auth="credentials", connect_fields=_WEBHOOK),
]

PLATFORM_BY_ID: dict[str, PlatformMeta] = {p.id: p for p in PLATFORM_CATALOG}


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class ChannelStatus(str, Enum):
    disconnected = "disconnected"
    connected = "connected"
    error = "error"


class PostStatus(str, Enum):
    draft = "draft"
    scheduled = "scheduled"
    publishing = "publishing"
    posted = "posted"
    failed = "failed"


class TopicStatus(str, Enum):
    idea = "idea"
    approved = "approved"
    used = "used"


# --------------------------------------------------------------------------- #
# 实体
# --------------------------------------------------------------------------- #
class Workspace(BaseModel):
    id: str
    name: str
    slug: str = ""
    brand_name: str = ""
    created_at: str = ""


class Channel(BaseModel):
    id: str
    workspace_id: str
    platform: str                 # PLATFORM_BY_ID key
    display_name: str = ""
    handle: str = ""
    status: ChannelStatus = ChannelStatus.disconnected
    connected_at: str = ""
    config: dict = Field(default_factory=dict)


class Asset(BaseModel):
    id: str
    workspace_id: str
    kind: str = "image"           # image | svg | video | doc | article | link
    # 素材库分类(文件夹式):
    #   published = 我历史发布/生成的(海报、发帖配图)
    #   uploaded  = 我本地上传的文件
    #   external  = 从别处获取的(竞品调研引用卡等)
    # 留空表示未标注 → 读取时由 asset_source() 依 path/meta 派生,故旧数据零迁移。
    source: str = ""              # "" | published | uploaded | external
    folder: str = ""              # 子目录,如 "helium10/blog"(external 用)
    title: str = ""
    path: str = ""
    url: str = ""
    created_at: str = ""
    meta: dict = Field(default_factory=dict)


class Topic(BaseModel):
    id: str
    workspace_id: str
    title: str
    angle: str = ""
    pillar: str = ""
    status: TopicStatus = TopicStatus.idea
    priority: float = 0.5
    source: str = "manual"        # manual | ideation
    created_at: str = ""


class PostContent(BaseModel):
    hook: str = ""
    body: str = ""
    cta: str = ""
    hashtags: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)

    def as_text(self) -> str:
        parts = [self.hook, self.body, self.cta]
        return "\n\n".join(p for p in parts if p).strip()

    def as_caption(self) -> str:
        """发布用整文:正文 + 精选话题标签(社媒 caption 需带 hashtags,as_text 不含)。"""
        base = self.as_text()
        tags = " ".join(
            (h if str(h).startswith("#") else f"#{h}")
            for h in (self.hashtags or []) if str(h).strip()
        )
        if not tags:
            return base
        return f"{base}\n\n{tags}" if base else tags


class PublishTarget(BaseModel):
    channel_id: str
    platform: str
    status: str = "pending"       # pending | posted | failed
    permalink: str = ""
    external_id: str = ""
    error: str = ""
    sandbox: bool = False          # True=模拟渠道(mock)发布,非平台真帖


class Post(BaseModel):
    id: str
    workspace_id: str
    title: str = ""
    content: PostContent = Field(default_factory=PostContent)
    format: ContentFormat = ContentFormat.text
    status: PostStatus = PostStatus.draft
    targets: list[PublishTarget] = Field(default_factory=list)
    scheduled_at: str = ""        # ISO,空=立即/未排期
    posted_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    topic_id: str = ""
    run_id: str = ""              # 关联的 8 槽流水线运行
    visual_svg: str = ""          # 海报路径
    video_srt: str = ""           # 字幕路径
    media: list[str] = Field(default_factory=list)  # 附加图片路径(多图)
    qa: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# API 请求体
# --------------------------------------------------------------------------- #
class WorkspaceCreate(BaseModel):
    name: str
    brand_name: str = ""


class ChannelConnect(BaseModel):
    platform: str
    display_name: str = ""
    config: dict = Field(default_factory=dict)   # 真实平台的连接凭据


class GenerateRequest(BaseModel):
    """从话题 / 平台生成一篇 Post 的内容(走 8 能力槽)。"""
    topic: str = ""
    topic_id: str = ""
    platform: str = "x"
    brand_sources: list[str] = Field(default_factory=list)
    trends: list[str] = Field(default_factory=list)
    optimize: bool = True         # 是否触发 QA 自我批评重写


class PostCreate(BaseModel):
    title: str = ""
    content: PostContent = Field(default_factory=PostContent)
    format: ContentFormat = ContentFormat.text
    channel_ids: list[str] = Field(default_factory=list)
    scheduled_at: str = ""
    topic_id: str = ""
    run_id: str = ""
    visual_svg: str = ""
    video_srt: str = ""
    media: list[str] = Field(default_factory=list)
    qa: dict = Field(default_factory=dict)
    dedupe_key: str = ""          # 稳定幂等键(画布自动落盘:cv:<channel>:<素材指纹>)
    #   非空 → create_post 命中同键的现存草稿就地更新,不新建重复草稿。


class ComposeRequest(BaseModel):
    """多模态素材 → Post Creating Loop → 每渠道一版预发稿。"""
    source_type: str = "text"        # 兼容旧字段(现已 all-in-one,自动识别)
    source: str = ""                 # 素材:文字 / URL / 图片链接 / 视频链接,可混合
    notes: str = ""                  # 额外指令(主题/角度/语气)——不是素材内容
    channel_ids: list[str] = Field(default_factory=list)
    target_format: str = "auto"      # 目标内容形式:auto | text | image_text | video
    language: str = "auto"           # 输出语言:auto | en | zh(auto=渠道配置/素材推断)

    @field_validator("language")
    @classmethod
    def _clamp_language(cls, v: str) -> str:
        v = (v or "auto").strip().lower()
        return v if v in ("auto", "en", "zh") else "auto"
    capability: str = "auto"         # 兼容旧字段(= target_format)
    loop: bool = True                # Post Creating Loop(analyze→research→strategy→迭代)
    max_iters: int = 2               # 每渠道最多迭代次数
    optimize: bool = True            # 兼容旧字段(loop 开启时以 loop 为准)


class ComposeRun(BaseModel):
    """一次 Create-post 生成的完整落库记录(可轮询进度 / 在 UI 恢复)。"""
    id: str
    workspace_id: str
    status: str = "done"                 # running | done | failed
    created_at: str = ""
    material_preview: str = ""
    driver: str = ""
    capabilities: list[str] = Field(default_factory=list)
    material_assets: dict = Field(default_factory=dict)
    drafts: list[dict] = Field(default_factory=list)
    request: dict = Field(default_factory=dict)
    analysis: dict = Field(default_factory=dict)       # ① 素材剖析结果
    trace: list[dict] = Field(default_factory=list)    # 各阶段驱动溯源
    fallback: bool = False                             # 所有渠道文案均为模板兜底
    progress: dict = Field(default_factory=dict)       # {step, detail, pct}
    error: str = ""


class PostUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[PostContent] = None
    format: Optional[ContentFormat] = None
    channel_ids: Optional[list[str]] = None
    scheduled_at: Optional[str] = None
    status: Optional[PostStatus] = None
    media: Optional[list[str]] = None
    visual_svg: Optional[str] = None
    video_srt: Optional[str] = None
    qa: Optional[dict] = None       # 重生成后同步 QA 分,避免 composer 显示旧内容的分数


# --------------------------------------------------------------------------- #
# 素材库 —— 外部(竞品)引用卡批量入库 / 素材整理
# --------------------------------------------------------------------------- #
class ExternalAssetItem(BaseModel):
    """从别处获取的一条素材『引用卡』:只存指向与我方摘要,不搬运版权正文。"""
    title: str = ""
    url: str = ""                    # 原文/视频/帖子链接(打开即跳原站)
    kind: str = "link"               # link | article | video | image | social
    site: str = ""                   # 来源站点标识:helium10 | perci | storeclaw …
    channel: str = ""                # 渠道:blog | youtube | linkedin | x | resources …
    summary: str = ""                # 我方一句话摘要(非原文照抄)
    tags: list[str] = Field(default_factory=list)
    thumb: str = ""                  # 封面图 URL(引用,不转存)
    author: str = ""
    published_at: str = ""


class ExternalAssetBatch(BaseModel):
    items: list[ExternalAssetItem] = Field(default_factory=list)


class AssetUpdate(BaseModel):
    """素材整理:改所属文件夹 / 分类 / 标题。"""
    source: Optional[str] = None     # published | uploaded | external
    folder: Optional[str] = None
    title: Optional[str] = None
