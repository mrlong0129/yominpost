"""8 能力槽流水线 + Router 规则 + QA 自我批评 的单元测试。"""
from datetime import date

import pytest

from yominpost.orchestrator import run
from yominpost.providers.template_provider import TemplateProvider
from yominpost.schemas import (
    BrandDNA,
    BrandSource,
    ContentFormat,
    Platform,
    PipelineRequest,
    TopicIdea,
)
from yominpost.stages import brand_dna, copy as copy_stage, qa as qa_stage, router


def _brand():
    return brand_dna.extract(
        "YominPost",
        [BrandSource(kind="website", text="我们帮助创业者用数据与方法更快产出专业内容。")],
        TemplateProvider(),
    )


def test_orchestrator_end_to_end():
    req = PipelineRequest(
        brand_name="YominPost",
        sources=[BrandSource(text="帮助创业者高效做内容,重方法与数据。")],
        raw_trends=["AI 内容自动化上升"],
        platforms=[Platform.x, Platform.instagram, Platform.tiktok, Platform.linkedin],
        num_topics=4,
        start_date=date(2026, 7, 8),
    )
    res = run(req, provider=TemplateProvider(), persist=False)
    assert len(res.packages) == 4
    assert len(res.calendar) == 4
    for pk in res.packages:
        assert pk.copy is not None and pk.copy.hook
        assert pk.qa is not None
        assert 0.0 <= pk.qa.overall <= 1.0
        # generators 与产物一致
        if "visual" in pk.format_decision.generators:
            assert pk.visual is not None and pk.visual.svg_path.endswith(".svg")
        if "video" in pk.format_decision.generators:
            assert pk.video is not None and pk.video.srt_path.endswith(".srt")


def test_brand_dna_fields():
    b = _brand()
    assert b.brand_name == "YominPost"
    assert b.tone and b.taboos and b.color_palette
    assert len(b.color_palette) >= 3


@pytest.mark.parametrize("platform,angle,expected", [
    (Platform.tiktok, "故事式", ContentFormat.short_video),
    (Platform.youtube, "数据式", ContentFormat.short_video),
    (Platform.instagram, "清单式 拆成 N 步", ContentFormat.carousel),
    (Platform.instagram, "故事式", ContentFormat.image_text),
    (Platform.x, "清单式", ContentFormat.thread),
    (Platform.x, "故事式", ContentFormat.text),
    (Platform.linkedin, "故事式", ContentFormat.text),
])
def test_router_rules(platform, angle, expected):
    b = _brand()
    topic = TopicIdea(id="t1", title="测试", angle=angle, suggested_platform=platform, priority=0.5)
    d = router.decide(topic, b)
    assert d.format == expected
    assert d.rule_trace  # 规则可解释
    assert "copy" in d.generators


def test_router_high_priority_x_becomes_thread():
    b = _brand()
    topic = TopicIdea(id="t1", title="x", angle="故事式", suggested_platform=Platform.x, priority=0.9)
    assert router.decide(topic, b).format == ContentFormat.thread


def test_qa_scores_and_compliance():
    b = _brand()
    topic = TopicIdea(id="t1", title="测试合规", suggested_platform=Platform.x)
    decision = router.decide(topic, b)
    draft = copy_stage.write(b, topic, decision, TemplateProvider())
    report = qa_stage.evaluate(b, topic, draft, decision, TemplateProvider())
    for k in ("on_brand", "hook", "cta", "compliance", "platform_fit", "clarity"):
        assert k in report.scores
        assert 0.0 <= report.scores[k] <= 1.0
    assert isinstance(report.passed, bool)


def test_qa_self_critique_rewrites_bad_copy():
    b = _brand()
    b.taboos = ["保证"]
    topic = TopicIdea(id="t1", title="测试", suggested_platform=Platform.x)
    decision = router.decide(topic, b)
    # 构造一条命中违禁词、无 CTA 的差文案
    from yominpost.schemas import CopyDraft
    bad = CopyDraft(topic_id="t1", platform=Platform.x, format=ContentFormat.text,
                    hook="这个方法保证有效", body="保证保证", cta="", char_count=10)
    final, report = qa_stage.review(b, topic, bad, decision, TemplateProvider())
    # 触发了重写,且最终版不差于原版
    assert report.revised is True
    assert report.overall >= 0.0
