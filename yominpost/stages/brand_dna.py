"""① 品牌知识库 / DNA。

输入:品牌名 + 素材 (官网文本 / 旧内容)。
输出:BrandDNA(调性、受众、价值主张、招牌词、违禁词、配色…)。
"""
from __future__ import annotations

from ..providers.base import Provider
from ..schemas import BrandDNA, BrandSource


def extract(brand_name: str, sources: list[BrandSource], provider: Provider) -> BrandDNA:
    ctx = {
        "brand_name": brand_name,
        "sources": [s.model_dump() for s in sources],
    }
    data = provider.run("brand_dna", ctx)
    data.setdefault("brand_name", brand_name)
    return BrandDNA.model_validate(data)
