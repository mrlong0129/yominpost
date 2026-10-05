"""② 选题 / Ideation / 内容日历。

输入:BrandDNA + 趋势 + 数量/平台/起始日期/节奏。
输出:TopicIdea 列表 + 排期后的 CalendarEntry 列表。
"""
from __future__ import annotations

from datetime import date, timedelta

from ..providers.base import Provider
from ..schemas import (
    BrandDNA,
    CalendarEntry,
    Platform,
    TopicIdea,
    TrendSignal,
)


def generate(
    brand: BrandDNA,
    trends: list[TrendSignal],
    provider: Provider,
    *,
    num_topics: int = 5,
    platforms: list[Platform] | None = None,
    start_date: date | None = None,
    cadence_days: int = 2,
) -> tuple[list[TopicIdea], list[CalendarEntry]]:
    platforms = platforms or [Platform.x]
    start = start_date or date.today()

    ctx = {
        "brand": brand.model_dump(),
        "trends": [t.model_dump() for t in trends],
        "num_topics": num_topics,
        "platforms": [p.value for p in platforms],
    }
    data = provider.run("ideation", ctx)

    topics: list[TopicIdea] = []
    calendar: list[CalendarEntry] = []
    for i, raw in enumerate(data.get("topics", [])):
        raw.setdefault("id", f"t{i + 1:02d}")
        topic = TopicIdea.model_validate(raw)
        topic.scheduled_date = start + timedelta(days=i * cadence_days)
        topics.append(topic)
        calendar.append(
            CalendarEntry(
                date=topic.scheduled_date,
                topic_id=topic.id,
                title=topic.title,
                platform=topic.suggested_platform,
            )
        )
    # 按优先级排序展示,但日历保持时间序
    topics.sort(key=lambda t: (-t.priority, t.id))
    calendar.sort(key=lambda c: c.date)
    return topics, calendar
