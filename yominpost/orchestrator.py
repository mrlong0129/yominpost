"""流水线编排器 —— 把 8 个能力槽串成一条内容生产线。

流向:
  ① brand_dna ─▶ ③ trends ─▶ ② ideation ─┐
                                          ▼  (对每个选题)
             ④ router ─▶ ⑤ copy ─▶ ⑧ qa(自我批评重写)
                                          ├─▶ ⑥ visual (若命中)
                                          └─▶ ⑦ video  (若命中)
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime

from . import store
from .config import make_provider
from .providers.base import Provider
from .schemas import (
    ContentPackage,
    PipelineRequest,
    PipelineResult,
)
from .stages import brand_dna, copy, ideation, qa, router, trends, video, visual


def run(request: PipelineRequest, provider: Provider | None = None,
        *, persist: bool = True) -> PipelineResult:
    provider = provider or make_provider()
    created_at = datetime.now().isoformat(timespec="seconds")
    digest = hashlib.sha1(
        f"{request.brand_name}{created_at}{request.num_topics}".encode()
    ).hexdigest()[:8]
    run_id = f"run_{int(time.time())}_{digest}"

    # ① 品牌 DNA
    brand = brand_dna.extract(request.brand_name, request.sources, provider)
    # ③ 趋势
    trend_signals = trends.gather(brand, request.raw_trends, provider)
    # ② 选题 / 日历
    topics, calendar = ideation.generate(
        brand,
        trend_signals,
        provider,
        num_topics=request.num_topics,
        platforms=request.platforms,
        start_date=request.start_date,
        cadence_days=request.cadence_days,
    )

    # 文案语言:显式指定优先;否则按素材自动判断(英文素材 → 英文文案)
    material = "\n".join(s.text for s in request.sources if getattr(s, "text", "")) or None
    language = (request.language or "").strip().lower() or "auto"
    if language == "auto":
        from .providers.template_provider import _lang_of
        language = _lang_of(material) if material else None

    packages: list[ContentPackage] = []
    for topic in topics:
        # ④ Router(规则)
        decision = router.decide(topic, brand)
        # ⑤ 文案
        copy_material = material if language == "en" else None   # 英文兜底模板会从素材取句子
        draft = copy.write(brand, topic, decision, provider, language=language,
                           material=copy_material)
        # ⑧ QA + 自我批评重写(得到最终文案)
        final_draft, qa_report = qa.review(brand, topic, draft, decision, provider,
                                           material=copy_material, language=language)

        stem = f"{run_id}_{topic.id}"
        visual_asset = None
        video_sb = None
        # ⑥ 视觉
        if "visual" in decision.generators:
            visual_asset = visual.render(brand, topic, final_draft, provider, stem=stem)
        # ⑦ 短视频
        if "video" in decision.generators:
            video_sb = video.storyboard(
                brand, topic, final_draft, provider,
                platform=decision.platform, stem=stem,
            )

        packages.append(
            ContentPackage(
                topic=topic,
                format_decision=decision,
                copy=final_draft,
                visual=visual_asset,
                video=video_sb,
                qa=qa_report,
            )
        )

    result = PipelineResult(
        run_id=run_id,
        created_at=created_at,
        provider=provider.name,
        brand=brand,
        trends=trend_signals,
        topics=topics,
        calendar=calendar,
        packages=packages,
    )
    if persist:
        try:
            store.save_run(result)
            store.save_brand(brand)
        except Exception:
            pass
    return result
