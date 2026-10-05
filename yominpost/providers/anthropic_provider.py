"""真模型 Provider:调用 Anthropic Messages API。

- 每个 task 映射成一个要求「只返回 JSON」的 prompt；
- 解析失败 / 网络失败 / 无 key 时，自动回退到 TemplateProvider，
  因此上层永远拿得到结构合法的结果。
"""
from __future__ import annotations

import json
import logging
import re

from .base import Provider
from .template_provider import TemplateProvider

log = logging.getLogger("yominpost.providers")

API_URL = "https://api.anthropic.com/v1/messages"


def _mark_fallback(result: dict, from_driver: str, reason: str) -> dict:
    """给兜底结果打上溯源标记——静默降级是 bug 之源,必须可见。"""
    if isinstance(result, dict):
        result["_meta"] = {"driver": "template", "live": False,
                           "fallback_from": from_driver, "reason": reason}
    return result

# 每个 task 的输出契约提示（键名需与 stage 侧解析一致）
TASK_SPECS = {
    "brand_dna": (
        "从给定品牌素材中抽取品牌 DNA。",
        '{"brand_name":str,"mission":str,"tone":[str],"voice":str,"audience":[str],'
        '"value_props":[str],"key_messages":[str],"vocabulary":[str],"taboos":[str],'
        '"visual_style":str,"color_palette":[hex str],"sample_hooks":[str]}',
    ),
    "trends": (
        "基于品牌与原始趋势文本，产出打分后的趋势信号（relevance 0..1）。",
        '{"signals":[{"source":str,"title":str,"summary":str,"relevance":float,"keywords":[str]}]}',
    ),
    "ideation": (
        "基于品牌与趋势，生成 num_topics 个选题。",
        '{"topics":[{"id":str,"title":str,"angle":str,"rationale":str,"pillar":str,'
        '"suggested_platform":str,"priority":float,"trend_refs":[str]}]}',
    ),
    "copy": (
        "为给定平台写一条高质量、可直接发布的社媒文案。"
        "若 ctx.material 存在(用户提供的素材:文章正文/图片说明/文字),必须紧扣它、提炼其要点,不要泛泛而谈。"
        "若 ctx.style 存在,严格按该渠道风格与字数写(如 X 短促带钩子、Facebook 叙事、"
        "Instagram 视觉+话题、LinkedIn 专业)。hook 要强、body 有信息量、cta 明确、hashtags 精选。"
        "若 ctx.critique 存在,据其重写改进。",
        '{"hook":str,"body":str,"cta":str,"hashtags":[str],"segments":[str],"char_count":int}',
    ),
    "visual": (
        "为文案产出视觉简报（图像 prompt / alt / 版式 / 配色）。",
        '{"prompt":str,"alt_text":str,"layout":str,"palette":[hex str]}',
    ),
    "video": (
        "为文案产出短视频分镜脚本。",
        '{"hook_line":str,"total_duration_sec":float,"scenes":[{"index":int,'
        '"duration_sec":float,"visual":str,"voiceover":str,"on_screen_text":str}],'
        '"caption":str,"music_mood":str}',
    ),
    "plan": (
        "你是内容策划。基于 ctx.material(用户素材)与 ctx.channels(目标渠道列表),"
        "为每个渠道各规划一版内容策略,核心目标是在该渠道产生流量曝光与转化。"
        "ctx.target_format 非 auto 时必须采用该形式;format 只能取 "
        "text|thread|image_text|carousel|short_video,且 needs_image/needs_video "
        "只在 ctx.available_kinds 包含 image/video 时才可为 true。"
        "angle 要具体(不是套话),key_points 从素材中提炼,reason 说明为何这样能带来曝光转化。",
        '{"plans":[{"platform":str,"format":str,"angle":str,"key_points":[str],'
        '"tone":str,"hashtags":[str],"needs_image":bool,"needs_video":bool,"reason":str}]}',
    ),
    "qa": (
        "对文案做质量评估与合规检查，给 0..1 打分。",
        '{"scores":{str:float},"overall":float,"passed":bool,'
        '"issues":[{"severity":str,"dimension":str,"message":str,"fix":str}]}',
    ),
    # —— Post Creating Loop 三阶段 —— #
    "analyze": (
        "深度剖析用户素材,穷尽其背后的上下文:这是什么产品/内容、解决什么痛点、"
        "目标人群是谁、核心价值主张、可信证据(数字/案例/功能点)。"
        "ctx.material 是素材本体(可能含抓取的网页正文,务必充分利用);"
        "ctx.notes 是运营者的指令(角度/重点),它不是素材内容,绝不能原样写进产出。"
        "language_hint 判断素材面向的受众语言(en/zh)。topic_title 是从素材提炼的主题标题(非指令复述)。",
        '{"subject":str,"summary":str,"audience":str,"pain_points":[str],'
        '"value_props":[str],"proof_points":[str],"keywords":[str],"tone":str,'
        '"language_hint":str,"topic_title":str}',
    ),
    "research": (
        "你是 ctx.platform 平台的增长专家。基于 ctx.analysis(素材剖析)为该平台做内容 research:"
        "该领域在此平台上什么形式/角度有权重、有优势、有增长空间;同类高表现内容的打法;"
        "平台算法与分发偏好;应避开的坑。若你具备联网检索能力,请检索该平台近期同类内容验证结论。"
        "结论必须具体可操作,不要泛泛而谈。angles 按曝光转化潜力排序。"
        "所有产出使用 ctx.language 指定的语言。",
        '{"platform":str,"winning_formats":[str],'
        '"angles":[{"angle":str,"why":str,"potential":str}],'
        '"hook_patterns":[str],"hashtag_strategy":[str],"pitfalls":[str],"algorithm_notes":str}',
    ),
    "strategy": (
        "收敛决策:综合 ctx.analysis(素材剖析)与 ctx.research(平台research),"
        "为 ctx.platform 渠道敲定唯一最优内容策略,目标=在该渠道产生流量曝光与转化。"
        "ctx.operator_notes 是运营者指令(如角度/重点/语气),策略必须遵循它,"
        "但它不是内容本身,不得原样写进产出。"
        "format 只能取 text|thread|image_text|carousel|short_video,"
        "且 needs_image/needs_video 只在 ctx.available_kinds 含 image/video 时可为 true;"
        "ctx.target_format 非 auto 时必须采用该形式。"
        "angle 具体、key_points 从素材提炼、hook_direction 给出钩子写法方向、"
        "cta_goal 指明转化动作。reason 说明为何该策略能带来曝光转化。",
        '{"format":str,"angle":str,"key_points":[str],"tone":str,"hashtags":[str],'
        '"hook_direction":str,"cta_goal":str,"needs_image":bool,"needs_video":bool,"reason":str}',
    ),
}


def _lang_directive(ctx: dict) -> str:
    """输出语言指令:ctx.language 指定产出语言(默认中文)。"""
    lang = (ctx or {}).get("language", "")
    if str(lang).lower().startswith("en"):
        return ("IMPORTANT: All user-facing output fields (hook/body/cta/hashtags/"
                "angles/key_points/titles) MUST be written in natural, native English. ")
    return "所有面向用户的文案字段用中文写。"


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return json.loads(text[start : end + 1])
    return json.loads(text)


class AnthropicProvider(Provider):
    name = "anthropic"
    is_live = True

    def __init__(self, model: str, fallback: Provider | None = None, api_key: str | None = None):
        import os

        self.model = model
        self.fallback = fallback or TemplateProvider()
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def run(self, task: str, ctx: dict) -> dict:
        spec = TASK_SPECS.get(task)
        if not spec or not self.api_key:
            return _mark_fallback(self.fallback.run(task, ctx), self.name,
                                  "no spec" if not spec else "no api key")
        goal, schema_hint = spec
        system = (
            "你是 YominPost 的内容引擎。只输出一个 JSON 对象，"
            "不要任何解释或 markdown 代码块。" + _lang_directive(ctx)
        )
        user = (
            f"任务：{goal}\n"
            f"必须严格符合此 JSON 结构：{schema_hint}\n\n"
            f"上下文(JSON)：\n{json.dumps(ctx, ensure_ascii=False)[:8000]}"
        )
        try:
            import httpx

            resp = httpx.post(
                API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2000,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            text = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            )
            parsed = _extract_json(text)
            if not isinstance(parsed, dict) or not parsed:
                raise ValueError("empty parse")
            parsed["_meta"] = {"driver": self.name, "live": True}
            return parsed
        except Exception as e:  # noqa: BLE001
            # 任何失败都回退到确定性模板,保证不中断 —— 但必须留下可见痕迹
            log.warning("anthropic task=%s fell back to template: %s", task, e)
            return _mark_fallback(self.fallback.run(task, ctx), self.name, str(e)[:200])
