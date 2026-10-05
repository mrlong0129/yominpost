"""所有能力槽的输入 / 输出数据契约 (Pydantic v2)。

每个 stage 都是「输入 schema -> 输出 schema」的纯函数式转换，
方便单测与流水线编排。ContentPackage / PipelineResult 是聚合产物。
"""
from __future__ import annotations

import warnings

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class Platform(str, Enum):
    x = "x"
    linkedin = "linkedin"
    instagram = "instagram"
    tiktok = "tiktok"
    youtube = "youtube"
    xiaohongshu = "xiaohongshu"
    wechat = "wechat"


class ContentFormat(str, Enum):
    text = "text"            # 纯文本贴
    thread = "thread"        # 多条串 (X thread)
    image_text = "image_text"  # 单图图文
    carousel = "carousel"    # 多图轮播 (小红书/IG)
    short_video = "short_video"  # 短视频


# 每个平台的软性字数上限（用于 Router 与 QA 的规则判断）
PLATFORM_LIMITS: dict[Platform, int] = {
    Platform.x: 280,
    Platform.linkedin: 3000,
    Platform.instagram: 2200,
    Platform.tiktok: 2200,
    Platform.youtube: 5000,
    Platform.xiaohongshu: 1000,
    Platform.wechat: 20000,
}

# 天然偏向视觉 / 视频的平台
VISUAL_FIRST: set[Platform] = {Platform.instagram, Platform.xiaohongshu}
VIDEO_FIRST: set[Platform] = {Platform.tiktok, Platform.youtube}


# --------------------------------------------------------------------------- #
# ① 品牌 DNA
# --------------------------------------------------------------------------- #
class BrandSource(BaseModel):
    kind: str = "note"          # website | old_post | note
    title: str = ""
    text: str


class BrandDNA(BaseModel):
    brand_name: str
    mission: str = ""
    tone: list[str] = Field(default_factory=list)       # ["自信", "温暖"]
    voice: str = ""
    audience: list[str] = Field(default_factory=list)
    value_props: list[str] = Field(default_factory=list)
    key_messages: list[str] = Field(default_factory=list)
    vocabulary: list[str] = Field(default_factory=list)  # 招牌词
    taboos: list[str] = Field(default_factory=list)      # 禁用词/禁用主张
    visual_style: str = ""
    color_palette: list[str] = Field(default_factory=list)
    sample_hooks: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# ③ 趋势 / 洞察
# --------------------------------------------------------------------------- #
class TrendSignal(BaseModel):
    source: str = "manual"
    title: str
    summary: str = ""
    relevance: float = 0.0      # 0..1，与品牌相关度
    keywords: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# ② 选题 / 日历
# --------------------------------------------------------------------------- #
class TopicIdea(BaseModel):
    id: str
    title: str
    angle: str = ""
    rationale: str = ""
    pillar: str = ""            # 内容支柱
    suggested_platform: Platform = Platform.x
    scheduled_date: Optional[date] = None
    priority: float = 0.5       # 0..1
    trend_refs: list[str] = Field(default_factory=list)


class CalendarEntry(BaseModel):
    date: date
    topic_id: str
    title: str
    platform: Platform


# --------------------------------------------------------------------------- #
# ④ 形式 Router
# --------------------------------------------------------------------------- #
class FormatDecision(BaseModel):
    topic_id: str
    format: ContentFormat
    platform: Platform
    reasons: list[str] = Field(default_factory=list)
    generators: list[str] = Field(default_factory=list)   # ["copy","visual"]
    rule_trace: list[str] = Field(default_factory=list)    # 命中的规则


# --------------------------------------------------------------------------- #
# ⑤ 文案
# --------------------------------------------------------------------------- #
class CopyDraft(BaseModel):
    topic_id: str
    platform: Platform
    format: ContentFormat
    hook: str = ""
    body: str = ""
    cta: str = ""
    hashtags: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)   # thread/carousel 分条
    char_count: int = 0


# --------------------------------------------------------------------------- #
# ⑥ 视觉
# --------------------------------------------------------------------------- #
class VisualAsset(BaseModel):
    topic_id: str
    prompt: str = ""            # 图像生成 prompt
    alt_text: str = ""
    layout: str = "quote_card"  # quote_card | poster | carousel
    palette: list[str] = Field(default_factory=list)
    svg_path: str = ""          # 已渲染的占位海报路径


# --------------------------------------------------------------------------- #
# ⑦ 短视频
# --------------------------------------------------------------------------- #
class VideoScene(BaseModel):
    index: int
    duration_sec: float = 3.0
    visual: str = ""
    voiceover: str = ""
    on_screen_text: str = ""


class VideoStoryboard(BaseModel):
    topic_id: str
    platform: Platform
    hook_line: str = ""
    total_duration_sec: float = 0.0
    scenes: list[VideoScene] = Field(default_factory=list)
    caption: str = ""
    music_mood: str = ""
    srt_path: str = ""


# --------------------------------------------------------------------------- #
# ⑧ QA / 自我批评
# --------------------------------------------------------------------------- #
class QAIssue(BaseModel):
    severity: str = "low"       # low | medium | high
    dimension: str = ""
    message: str = ""
    fix: str = ""


class QAReport(BaseModel):
    topic_id: str
    scores: dict[str, float] = Field(default_factory=dict)  # 维度 -> 0..1
    overall: float = 0.0
    passed: bool = False
    issues: list[QAIssue] = Field(default_factory=list)
    revised: bool = False       # 是否触发过自我批评重写


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
# `copy` is part of the public data contract; silence pydantic's "shadows BaseModel.copy" warning.
warnings.filterwarnings("ignore", message='Field name "copy"', category=UserWarning)


class ContentPackage(BaseModel):
    topic: TopicIdea
    format_decision: FormatDecision
    copy: Optional[CopyDraft] = None
    visual: Optional[VisualAsset] = None
    video: Optional[VideoStoryboard] = None
    qa: Optional[QAReport] = None


class PipelineRequest(BaseModel):
    brand_name: str = "YominPost"
    sources: list[BrandSource] = Field(default_factory=list)
    raw_trends: list[str] = Field(default_factory=list)      # 手动喂入的趋势文本
    platforms: list[Platform] = Field(default_factory=lambda: [Platform.x])
    num_topics: int = 5
    start_date: Optional[date] = None
    cadence_days: int = 2       # 每几天一篇
    language: Optional[str] = None  # 文案语言 en/zh;None/auto=按素材自动判断(无素材默认中文)


class PipelineResult(BaseModel):
    run_id: str
    created_at: str = ""
    provider: str = ""
    brand: BrandDNA
    trends: list[TrendSignal] = Field(default_factory=list)
    topics: list[TopicIdea] = Field(default_factory=list)
    calendar: list[CalendarEntry] = Field(default_factory=list)
    packages: list[ContentPackage] = Field(default_factory=list)
