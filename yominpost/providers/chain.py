"""驱动回退链:选中的真驱动挂了,自动试下一个可用真驱动,全挂才用模板。

病根复盘:早先只有「选中驱动 → template」一跳。任一 CLI 掉线(agy 返回空、
claude 未登录 401、codex 撞额度)就静默退化成模板套话,核心 loop 形同没跑。
ChainProvider 让引擎在多个真驱动间自动择优,单点掉线不再拖垮全链路。

- 按序尝试 providers(选中的排第一),第一个产出 live 结果的胜出。
- per-run dead-set:某驱动本次 run 内失败一次即标记,后续 task 直接跳过,
  避免每步都对已知挂掉的 CLI 重试(agy 空跑一次 ~6s)。
- 全部真驱动失败 → 用 template 兜底,并标注 _meta(可见,不静默)。
"""
from __future__ import annotations

import logging

from .anthropic_provider import _mark_fallback
from .base import Provider
from .template_provider import TemplateProvider

log = logging.getLogger("yominpost.providers")


class ChainProvider(Provider):
    name = "chain"
    is_live = True

    def __init__(self, providers: list[Provider], fallback: Provider | None = None):
        # 只保留真驱动(is_live),模板永远垫底
        self.providers = [p for p in providers if getattr(p, "is_live", False)]
        self.fallback = fallback or TemplateProvider()
        self._dead: set[str] = set()          # 本 run 内真失败的 CLI(agy 空/claude 401 等)
        self._preferred: str | None = None    # 上次出活的驱动,下个 task 优先试

    def _order(self) -> list[Provider]:
        """把上次出活的驱动排到最前,避免每步都空跑已知慢/挂的 CLI。"""
        if not self._preferred:
            return self.providers
        pref = [p for p in self.providers if p.name == self._preferred]
        rest = [p for p in self.providers if p.name != self._preferred]
        return pref + rest

    def run(self, task: str, ctx: dict) -> dict:
        last_fail = None
        for p in self._order():
            if p.name in self._dead:
                continue
            r = p.run(task, ctx)
            meta = (r or {}).get("_meta") or {}
            if meta.get("live"):
                self._preferred = p.name
                return r
            if meta.get("fallback_from"):
                # CLI 真失败(agy 空 / claude 401 / 解析失败)→ 本 run 内拉黑,试下一个
                self._dead.add(p.name)
                last_fail = r
                continue
            # live=False 且无 fallback_from = 该 task 设计内就走模板
            # (brand_dna/qa/visual 等结构/打分任务)→ 采用,绝不惩罚该驱动。
            return r
        # 所有真驱动都挂了 → 显式模板兜底
        if last_fail is not None:
            return last_fail
        return _mark_fallback(self.fallback.run(task, ctx), "chain",
                              f"all live drivers failed: {','.join(sorted(self._dead)) or 'none'}")
