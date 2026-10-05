"""⑦ 短视频。

输入:BrandDNA + TopicIdea + CopyDraft + 平台。
输出:VideoStoryboard(分镜列表 / 时长 / 配乐情绪),并写出 .srt 字幕文件。
"""
from __future__ import annotations

from pathlib import Path

from ..config import ARTIFACT_DIR
from ..providers.base import Provider
from ..schemas import (
    BrandDNA,
    CopyDraft,
    Platform,
    TopicIdea,
    VideoScene,
    VideoStoryboard,
)


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(scenes: list[VideoScene], stem: str) -> str:
    lines, t = [], 0.0
    for i, sc in enumerate(scenes, start=1):
        start, end = t, t + sc.duration_sec
        text = sc.on_screen_text or sc.voiceover
        lines += [str(i), f"{_ts(start)} --> {_ts(end)}", text, ""]
        t = end
    path = Path(ARTIFACT_DIR) / f"{stem}.srt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def storyboard(
    brand: BrandDNA,
    topic: TopicIdea,
    copy: CopyDraft,
    provider: Provider,
    *,
    platform: Platform | None = None,
    stem: str | None = None,
) -> VideoStoryboard:
    ctx = {
        "brand": brand.model_dump(),
        "topic": topic.model_dump(),
        "copy": copy.model_dump(),
        "platform": (platform or copy.platform).value,
    }
    data = provider.run("video", ctx)
    scenes = [VideoScene.model_validate(s) for s in data.get("scenes", [])]
    if not scenes:  # 保险:至少一幕
        scenes = [VideoScene(index=0, voiceover=copy.hook, on_screen_text=copy.hook[:24])]
    stem = stem or f"{topic.id}"
    srt_path = _write_srt(scenes, stem)
    total = data.get("total_duration_sec") or round(sum(s.duration_sec for s in scenes), 1)
    return VideoStoryboard(
        topic_id=topic.id,
        platform=platform or copy.platform,
        hook_line=data.get("hook_line", copy.hook),
        total_duration_sec=total,
        scenes=scenes,
        caption=data.get("caption", copy.hook),
        music_mood=data.get("music_mood", "upbeat"),
        srt_path=srt_path,
    )
