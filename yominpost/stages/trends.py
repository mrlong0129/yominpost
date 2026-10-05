"""③ 趋势 / 洞察输入(该写什么)。

输入:BrandDNA + 原始趋势文本(可为空,空则由 provider 合成)。
输出:打分并排序后的 TrendSignal 列表。
"""
from __future__ import annotations

from ..providers.base import Provider
from ..schemas import BrandDNA, TrendSignal


def gather(brand: BrandDNA, raw_trends: list[str], provider: Provider) -> list[TrendSignal]:
    ctx = {"brand": brand.model_dump(), "raw_trends": raw_trends}
    data = provider.run("trends", ctx)
    signals = [TrendSignal.model_validate(s) for s in data.get("signals", [])]
    signals.sort(key=lambda s: -s.relevance)
    return signals
