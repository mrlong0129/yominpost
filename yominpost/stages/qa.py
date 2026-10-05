"""⑧ 质量评估 / 自我批评(QA)。

对文案打分 + 违禁词/合规/平台适配检查。若不达标,生成 critique 并
调用文案 stage **自我批评重写** 一次,取两版中更优者。
"""
from __future__ import annotations

from ..providers.base import Provider
from ..schemas import BrandDNA, CopyDraft, FormatDecision, QAIssue, QAReport, TopicIdea
from . import copy as copy_stage


def evaluate(brand: BrandDNA, topic: TopicIdea, draft: CopyDraft,
             decision: FormatDecision, provider: Provider) -> QAReport:
    """只打分、不重写。供组合器的"不优化"模式直接调用。"""
    ctx = {
        "brand": brand.model_dump(),
        "topic": topic.model_dump(),
        "copy": draft.model_dump(),
        "platform": decision.platform.value,
        "format": decision.format.value,
    }
    data = provider.run("qa", ctx)
    return QAReport(
        topic_id=topic.id,
        scores=data.get("scores", {}),
        overall=data.get("overall", 0.0),
        passed=data.get("passed", False),
        issues=[QAIssue.model_validate(x) for x in data.get("issues", [])],
    )


def _critique(report: QAReport) -> str:
    if not report.issues:
        return "整体可提升 hook 强度与信息密度。"
    return "请针对以下问题重写:" + "；".join(
        f"[{i.dimension}] {i.message} -> {i.fix}" for i in report.issues
    )


def review(
    brand: BrandDNA,
    topic: TopicIdea,
    draft: CopyDraft,
    decision: FormatDecision,
    provider: Provider,
    *,
    material: str | None = None,
    style: str | None = None,
    language: str | None = None,
) -> tuple[CopyDraft, QAReport]:
    """返回 (最终文案, QA 报告)。不达标则自我批评重写一次(保留素材/风格)。"""
    report = evaluate(brand, topic, draft, decision, provider)
    if report.passed:
        return draft, report

    # —— 自我批评重写一次 —— #
    critique = _critique(report)
    revised_draft = copy_stage.write(brand, topic, decision, provider,
                                     critique=critique, material=material, style=style,
                                     language=language)
    revised_report = evaluate(brand, topic, revised_draft, decision, provider)
    revised_report.revised = True

    # 取更优者(overall 高者胜;并列取重写版)
    if revised_report.overall >= report.overall:
        return revised_draft, revised_report
    report.revised = True
    return draft, report
