"""⑤ 文案生成。

输入:BrandDNA + TopicIdea + FormatDecision(+ 可选 critique 用于重写)。
输出:CopyDraft(hook / body / cta / hashtags / segments)。
"""
from __future__ import annotations

from ..providers.base import Provider
from ..schemas import BrandDNA, CopyDraft, FormatDecision, TopicIdea


def write(
    brand: BrandDNA,
    topic: TopicIdea,
    decision: FormatDecision,
    provider: Provider,
    *,
    critique: str | None = None,
    material: str | None = None,
    style: str | None = None,
    language: str | None = None,
) -> CopyDraft:
    ctx = {
        "brand": brand.model_dump(),
        "topic": topic.model_dump(),
        "format": decision.format.value,
        "platform": decision.platform.value,
        "critique": critique,
        "material": material,   # 用户提供的素材(url 抓取文本 / 图片说明 / 文字)
        "style": style,         # 该渠道的风格指令
        "language": language,   # 该渠道的输出语言(en/zh;None=默认中文)
    }
    data = provider.run("copy", ctx)
    # 真模型常返回键存在但值为 null(如 "cta": null);.get 会拿到 None,
    # 传给声明 str/list[str] 的 CopyDraft 会 ValidationError,让整条多渠道 compose 崩掉。
    # 这里统一把 None 归一成 ""/[],并把列表元素强制转字符串。
    def _s(k):
        return str(data.get(k) or "")

    def _l(k):
        v = data.get(k) or []
        return [str(x) for x in v] if isinstance(v, (list, tuple)) else []

    draft = CopyDraft(
        topic_id=topic.id,
        platform=decision.platform,
        format=decision.format,
        hook=_s("hook"),
        body=_s("body"),
        cta=_s("cta"),
        hashtags=_l("hashtags"),
        segments=_l("segments"),
    )
    full = "\n".join([draft.hook, draft.body, draft.cta])
    draft.char_count = data.get("char_count") or len(full)
    return draft
