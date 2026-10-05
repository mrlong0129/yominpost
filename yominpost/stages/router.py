"""④ 形式选择 Router —— 纯规则化决策(不经过 LLM)。

依据平台特性 + 选题角度 + 优先级,决定输出形式与需要触发的生成器。
所有命中的规则都会记录在 rule_trace 里,决策可解释、可单测。
"""
from __future__ import annotations

from ..schemas import (
    VIDEO_FIRST,
    VISUAL_FIRST,
    BrandDNA,
    ContentFormat,
    FormatDecision,
    Platform,
    TopicIdea,
)

# 角度中带这些词 → 适合分条 / 轮播
LIST_ANGLE_HINTS = ("清单", "对比", "数据", "N 步", "步骤", "要点")

# 形式 → 需要触发的生成器
GENERATORS = {
    ContentFormat.text: ["copy"],
    ContentFormat.thread: ["copy"],
    ContentFormat.image_text: ["copy", "visual"],
    ContentFormat.carousel: ["copy", "visual"],
    ContentFormat.short_video: ["copy", "video", "visual"],  # visual 作封面
}


def decide(topic: TopicIdea, brand: BrandDNA) -> FormatDecision:
    platform = topic.suggested_platform
    angle = topic.angle or ""
    is_listy = any(h in angle for h in LIST_ANGLE_HINTS)
    reasons: list[str] = []
    trace: list[str] = []

    # ---- 规则优先级:平台特性 > 角度 > 兜底 ---- #
    if platform in VIDEO_FIRST:
        fmt = ContentFormat.short_video
        trace.append("R1: 平台偏视频(tiktok/youtube) → short_video")
        reasons.append(f"{platform.value} 以短视频为主要消费形式")
    elif platform in VISUAL_FIRST:
        if is_listy:
            fmt = ContentFormat.carousel
            trace.append("R2a: 视觉平台 + 清单/对比角度 → carousel")
            reasons.append("多要点更适合多图轮播")
        else:
            fmt = ContentFormat.image_text
            trace.append("R2b: 视觉平台 + 叙事角度 → image_text")
            reasons.append("单图图文承载观点更聚焦")
    elif platform == Platform.x:
        if is_listy or topic.priority >= 0.8:
            fmt = ContentFormat.thread
            trace.append("R3a: X + 清单角度或高优先级 → thread")
            reasons.append("值得展开为多条串提升停留")
        else:
            fmt = ContentFormat.text
            trace.append("R3b: X + 普通角度 → text")
            reasons.append("短平快单条更契合 X")
    elif platform in (Platform.linkedin, Platform.wechat):
        fmt = ContentFormat.text
        trace.append("R4: 长文平台 → text(长文)")
        reasons.append("长文平台以纯文本深度内容为主")
    else:
        fmt = ContentFormat.text
        trace.append("R0: 兜底 → text")

    return FormatDecision(
        topic_id=topic.id,
        format=fmt,
        platform=platform,
        reasons=reasons,
        generators=GENERATORS[fmt],
        rule_trace=trace,
    )
