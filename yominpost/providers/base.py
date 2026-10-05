"""Provider 抽象。

Provider 只负责「生成」这件事:给定一个 task 名称 + 上下文 dict，返回一个 dict。
stage 层负责把返回 dict 校验成 Pydantic 模型 + 编排。

这样设计的好处:
  - TemplateProvider 用确定性规则实现每个 task -> 离线可跑、测试稳定；
  - AnthropicProvider 把每个 task 映射成 prompt 调真模型，失败自动回退模板；
  - Router 是纯规则、不经过 provider（对应「规则化决策」）。

支持的 task:
  brand_dna | trends | ideation | copy | visual | video | qa
"""
from __future__ import annotations

from abc import ABC, abstractmethod

TASKS = ("brand_dna", "trends", "ideation", "copy", "visual", "video", "qa")


class Provider(ABC):
    name: str = "base"
    is_live: bool = False

    @abstractmethod
    def run(self, task: str, ctx: dict) -> dict:
        """执行一个生成 task，返回 dict。实现必须对 TASKS 中的每个 task 有输出。"""
        raise NotImplementedError
