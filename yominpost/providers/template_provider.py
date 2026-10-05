"""确定性 / 离线 Provider。

用规则 + 模板实现每个 task，不依赖任何外部 API。
所有"随机"都用 hashlib 做成可复现的伪随机，保证测试稳定。
它既是默认 provider，也是 AnthropicProvider 的回退实现。
"""
from __future__ import annotations

import hashlib
import re

from .base import Provider

# 一些确定性的调色板(hex),按品牌名哈希选择
PALETTES = [
    ["#0F172A", "#38BDF8", "#F8FAFC", "#FACC15"],
    ["#1A1423", "#F72585", "#4CC9F0", "#F1FAEE"],
    ["#052E16", "#22C55E", "#ECFDF5", "#FDE047"],
    ["#111827", "#F97316", "#FFF7ED", "#60A5FA"],
    ["#1E1B4B", "#818CF8", "#EEF2FF", "#FBBF24"],
]

TONE_SIGNALS = {
    "自信": ["领先", "第一", "专业", "强大", "领导", "leading", "best", "expert"],
    "温暖": ["陪伴", "关心", "温暖", "一起", "friend", "care", "warm", "community"],
    "专业": ["方法", "研究", "数据", "系统", "framework", "data", "method", "research"],
    "俏皮": ["哈哈", "有趣", "玩", "fun", "playful", "joke", "meme"],
    "简洁": ["简单", "极简", "clean", "simple", "minimal"],
}

# 默认违禁 / 慎用词(广告法 + 夸大主张)
DEFAULT_TABOOS = ["最", "第一", "100%", "绝对", "保证", "国家级", "永久", "根治"]

STOPWORDS = set(
    "的 了 和 与 是 在 我 你 他 她 它 我们 你们 他们 这 那 就 都 也 而 及 或 "
    "the a an of to and for in on is are be with your our you we it this that".split()
)


def _lang_of(text: str) -> str:
    """粗判文本语言:CJK 字符多→zh,否则→en。"""
    cjk = len(re.findall(r"[一-鿿]", text or ""))
    latin = len(re.findall(r"[A-Za-z]", text or ""))
    return "zh" if cjk * 2 >= latin else "en"


# 各平台增长打法手册(research 任务的离线回退;真驱动会检索验证并写得更具体)
# 每项:formats(优势形式)、angles[(角度,原因,潜力)]、hooks、hashtags、pitfalls、algo
PLATFORM_PLAYBOOK: dict[str, dict] = {
    "facebook": {
        "formats": ["image_text", "text"],
        "angles": [("真实场景故事 — 痛点代入", "FB 分发重互动评论,故事最易引发共鸣转发", "high"),
                   ("前后对比 — 用了工具前 vs 后", "对比图文停留时长高,利于算法加权", "mid")],
        "hooks": ["以第一人称场景开头(I spent 3 hours...)", "抛出受众都经历过的挫败瞬间"],
        "hashtags": [], "pitfalls": ["外链直贴会被降权,链接放评论区", "标题党会被负反馈"],
        "algo": "Meta 优先有意义互动(评论>点赞);原生图文优于外链",
    },
    "instagram": {
        "formats": ["image_text", "carousel"],
        "angles": [("视觉化步骤拆解 — carousel 干货", "IG 收藏/转发权重高,干货图组易被收藏", "high"),
                   ("before/after 视觉对比", "首图决定停留,对比图 CTR 高", "high")],
        "hooks": ["首图大字点出结果/数字", "第一句制造好奇缺口"],
        "hashtags": ["#smallbusiness", "#ecommerce"],
        "pitfalls": ["纯文字截图表现差", "hashtag 堆砌超过 10 个反而降权"],
        "algo": "Reels/收藏/私信分享是分发核心信号;首 3 秒/首图定生死",
    },
    "x": {
        "formats": ["text", "thread"],
        "angles": [("反常识断言开头", "X 靠强观点获得转发与引用", "high"),
                   ("数字清单 thread", "thread 完读率高带来关注转化", "mid")],
        "hooks": ["一句话反常识判断", "数字+结果(How I saved 10h/week...)"],
        "hashtags": [], "pitfalls": ["外链首推会降权", "超过 2 个话题标签显得垃圾"],
        "algo": "回复与引用权重高;首条不放链接,链接放回复",
    },
    "linkedin": {
        "formats": ["text", "image_text"],
        "angles": [("专业复盘 — 数据+教训", "LinkedIn 重专业洞察,复盘文易获行业转发", "high"),
                   ("行业痛点洞察 + 解法框架", "决策者受众关心 ROI 与方法论", "high")],
        "hooks": ["首行抛出具体数字结论", "讲一个客户/自己踩坑的真实开场"],
        "hashtags": ["#ecommerce", "#productivity"],
        "pitfalls": ["销售腔会被划走", "首行平淡则 see more 无人点开"],
        "algo": "停留时长+评论质量定分发;首两行决定展开率",
    },
    "reddit": {
        "formats": ["text"],
        "angles": [("真实经验分享(不推销)", "Reddit 反感营销,真诚经验帖才能存活获 upvote", "high")],
        "hooks": ["像用户不像品牌:I built/tried X and here's what happened"],
        "hashtags": [], "pitfalls": ["任何广告腔会被踩/删", "必须遵守 subreddit 规则"],
        "algo": "前 1 小时 upvote 决定生死;评论区认真回复是关键",
    },
    "tiktok": {
        "formats": ["short_video"],
        "angles": [("3 秒痛点钩子 + 快节奏演示", "完播率是第一权重", "high")],
        "hooks": ["前 3 秒说出受众最痛的一句话"], "hashtags": [],
        "pitfalls": ["开头铺垫超过 3 秒即划走"], "algo": "完播率>互动;字幕必须加",
    },
    "youtube": {
        "formats": ["short_video"],
        "angles": [("可搜索的 How-to 标题", "搜索流量长尾稳定", "high")],
        "hooks": ["标题含关键词+结果承诺"], "hashtags": [],
        "pitfalls": ["标题与内容不符伤害留存"], "algo": "点击率×观看时长;Shorts 重完播",
    },
    "xiaohongshu": {
        "formats": ["image_text"],
        "angles": [("干货清单 + emoji 分点", "收藏率驱动长尾流量", "high")],
        "hooks": ["标题给出明确利益点+数字"], "hashtags": [],
        "pitfalls": ["硬广被限流"], "algo": "收藏+搜索是核心;封面图定点击",
    },
    "_default": {
        "formats": ["text"],
        "angles": [("故事式 — 从真实场景讲起", "通用叙事结构,适配多数渠道", "mid")],
        "hooks": ["直接点出痛点"], "hashtags": [],
        "pitfalls": ["泛泛而谈没有细节"], "algo": "",
    },
}


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def _pick(options: list, key: str):
    return options[_seed(key) % len(options)]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？.!?\n]+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 4]


def _shorten_en(s: str, limit: int) -> str:
    """英文按词边界截断,绝不切在词中间(否则会产出 'answer the buyer's re' 这种残句)。
    整句能放下就整句返回;放不下则在 limit 前最后一个空格处断开并加省略号。"""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    sp = cut.rfind(" ")
    if sp > 0:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.-") + "…"


def _shorten_cjk(s: str, limit: int) -> str:
    """中文无空格,按字数截断即可,但补省略号表明有省略。"""
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip("，、;:。") + "…"


def _keywords(text: str, limit: int = 12) -> list[str]:
    # 中文按 2-4 字词粗切 + 英文单词
    tokens = re.findall(r"[A-Za-z]{3,}", text) + re.findall(r"[一-鿿]{2,4}", text)
    freq: dict[str, int] = {}
    for t in tokens:
        tl = t.lower()
        if tl in STOPWORDS:
            continue
        freq[t] = freq.get(t, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def rule_plan(platform: str, material: str, target_format: str = "auto") -> dict:
    """确定性规划回退:按渠道特性 + 素材特征选形式/角度/要点。"""
    has_num = bool(re.search(r"\d", material or ""))
    if target_format and target_format != "auto":
        fmt = {"text": "text", "image_text": "image_text", "video": "short_video"}.get(
            target_format, "text")
    else:
        fmt = ("short_video" if platform in ("tiktok", "youtube")
               else "image_text" if platform in ("instagram", "xiaohongshu")
               else "text")
    kws = _keywords(material or "", 4)
    sents = _sentences(material or "")[:3]
    return {
        "platform": platform, "format": fmt,
        "angle": "数据式 — 用数字击穿注意力" if has_num else "故事式 — 从真实场景讲起",
        "key_points": sents, "tone": "真诚、具体",
        "hashtags": [f"#{k}" for k in kws[:3]],
        "needs_image": fmt in ("image_text", "carousel", "short_video"),
        "needs_video": fmt == "short_video",
        "reason": f"{platform} 适合 {fmt};素材含{'数据' if has_num else '故事'}要素,取对应角度",
    }


class TemplateProvider(Provider):
    name = "template"
    is_live = False

    def run(self, task: str, ctx: dict) -> dict:
        fn = getattr(self, f"_t_{task}", None)
        if fn is None:
            raise ValueError(f"unknown task: {task}")
        return fn(ctx)

    # ----------------------------- ① brand_dna ---------------------------- #
    def _t_brand_dna(self, ctx: dict) -> dict:
        brand_name = ctx.get("brand_name", "YominPost")
        sources = ctx.get("sources", [])
        blob = "\n".join(s.get("text", "") for s in sources) or brand_name
        sents = _sentences(blob)
        kws = _keywords(blob)

        # 调性:命中信号词的 tone
        tone = [t for t, sig in TONE_SIGNALS.items() if any(w in blob for w in sig)]
        if not tone:
            tone = _pick([["专业", "自信"], ["温暖", "简洁"], ["自信", "俏皮"]], brand_name)

        # 受众:找 "为/帮助/面向 X" 模式，否则默认
        audience = re.findall(r"(?:为|帮助|面向|服务)([一-鿿A-Za-z]{2,8})", blob)
        audience = list(dict.fromkeys(audience))[:3] or ["成长期的创业者", "内容团队"]

        value_props = [s for s in sents if re.search(r"帮助|让|提升|节省|更快|更好|减少", s)][:4]
        if not value_props:
            value_props = sents[:3] or [f"{brand_name} 让内容生产更高效"]

        key_messages = (sents[:3] if sents else [f"{brand_name}:把想法变成一整周的内容"])
        palette = _pick(PALETTES, brand_name)

        sample_hooks = [
            f"如果你还在为{audience[0]}发愁,先看这条。",
            f"关于{kws[0] if kws else '增长'},99% 的人搞错了一件事。",
            f"我们用 {brand_name} 做了个实验,结果出乎意料。",
        ]

        return {
            "brand_name": brand_name,
            "mission": (sents[0] if sents else f"{brand_name} 帮助你持续产出高质量内容"),
            "tone": tone,
            "voice": f"{'、'.join(tone)}的第一人称,短句为主,重价值不吹嘘",
            "audience": audience,
            "value_props": value_props,
            "key_messages": key_messages,
            "vocabulary": kws[:8],
            "taboos": DEFAULT_TABOOS,
            "visual_style": "干净、对比强、留白多,标题大字号",
            "color_palette": palette,
            "sample_hooks": sample_hooks,
        }

    # ------------------------------ ③ trends ------------------------------ #
    def _t_trends(self, ctx: dict) -> dict:
        brand = ctx.get("brand", {})
        vocab = set(brand.get("vocabulary", [])) | set(brand.get("audience", []))
        raw = ctx.get("raw_trends", []) or []

        signals = []
        if not raw:
            # 无输入时:从品牌 vocabulary 合成通用趋势,保证该槽有产物
            base = brand.get("vocabulary", ["AI", "效率", "增长"])[:3] or ["AI"]
            raw = [f"{b}相关话题正在上升,受众讨论度增加" for b in base]

        for i, r in enumerate(raw):
            kws = _keywords(r, 5)
            overlap = len(vocab & set(kws))
            relevance = min(1.0, 0.4 + 0.15 * overlap + (0.1 if i == 0 else 0))
            signals.append(
                {
                    "source": "manual" if ctx.get("raw_trends") else "synthesized",
                    "title": (kws[0] if kws else r[:16]) + " 趋势",
                    "summary": r.strip()[:120],
                    "relevance": round(relevance, 2),
                    "keywords": kws,
                }
            )
        signals.sort(key=lambda s: -s["relevance"])
        return {"signals": signals}

    # ----------------------------- ② ideation ----------------------------- #
    def _t_ideation(self, ctx: dict) -> dict:
        brand = ctx.get("brand", {})
        trends = ctx.get("trends", [])
        n = int(ctx.get("num_topics", 5))
        platforms = ctx.get("platforms", ["x"]) or ["x"]

        pillars = ["方法论", "案例复盘", "行业洞察", "产品幕后", "用户故事", "误区澄清"]
        angles = [
            ("清单式", "把复杂问题拆成 N 步"),
            ("反常识", "打破一个流行的错误认知"),
            ("对比式", "A 做法 vs B 做法"),
            ("故事式", "从一个真实场景讲起"),
            ("数据式", "用一个数字击穿注意力"),
        ]
        vocab = brand.get("vocabulary", ["增长"]) or ["增长"]
        audience = (brand.get("audience", ["创业者"]) or ["创业者"])[0]

        topics = []
        for i in range(n):
            pillar = pillars[i % len(pillars)]
            angle_name, angle_desc = angles[i % len(angles)]
            kw = vocab[i % len(vocab)]
            trend = trends[i % len(trends)] if trends else None
            title = f"[{angle_name}] 关于{kw},{audience}最该知道的一件事"
            topics.append(
                {
                    "id": f"t{i + 1:02d}",
                    "title": title,
                    "angle": f"{angle_name} — {angle_desc}",
                    "rationale": (
                        f"结合趋势「{trend['title']}」" if trend else "基于品牌核心支柱"
                    )
                    + f",面向{audience},属于{pillar}支柱。",
                    "pillar": pillar,
                    "suggested_platform": platforms[i % len(platforms)],
                    "priority": round(0.5 + (trend["relevance"] * 0.4 if trend else 0.1), 2),
                    "trend_refs": [trend["title"]] if trend else [],
                }
            )
        return {"topics": topics}

    # ------------------------------- ⑤ copy ------------------------------- #
    def _t_copy(self, ctx: dict) -> dict:
        if str(ctx.get("language", "")).lower().startswith("en"):
            return self._t_copy_en(ctx)
        brand = ctx.get("brand", {})
        topic = ctx.get("topic", {})
        fmt = ctx.get("format", "text")
        platform = ctx.get("platform", "x")
        critique = ctx.get("critique")  # QA 重写时带入
        material = (ctx.get("material") or "").strip()  # 用户素材

        voice_words = brand.get("vocabulary", []) or ["增长"]
        hooks = brand.get("sample_hooks", []) or ["先别划走,这条值得看。"]
        audience = (brand.get("audience", ["你"]) or ["你"])[0]
        title = topic.get("title", "一个想法")
        angle = topic.get("angle", "")

        hook = _pick(hooks, title + str(critique))
        # QA 重写:换更强的 hook（带数字/问句）
        if critique:
            hook = f"3 个要点,读完你对「{voice_words[0]}」的看法会变。"

        if material:
            # 有素材:提炼素材要点(离线确定性版;真驱动会写得更好)
            msents = _sentences(material)[:3] or [material[:60]]
            mkws = _keywords(material, 4)
            if not critique:
                hook = f"关于{mkws[0] if mkws else title}:{_shorten_cjk(msents[0], 34)}"
            points = [f"· {_shorten_cjk(s, 60)}" for s in msents]
            body_core = "\n".join(points)
            voice_words = (mkws or voice_words)
        else:
            points = [
                f"第一,{audience}最常忽略的是{voice_words[0]}本身,而不是工具。",
                f"第二,与其追热点,不如把一个方法讲透 —— 这正是{angle or '本条'}的角度。",
                f"第三,把它做成可复用的动作,而不是一次性灵感。",
            ]
            body_core = "\n".join(points)
        cta = f"如果对你有用,点个赞让更多{audience}看到 👇"

        # 违禁词软过滤
        taboos = brand.get("taboos", [])
        for t in taboos:
            body_core = body_core.replace(t, "更")
            hook = hook.replace(t, "更")

        segments: list[str] = []
        if fmt in ("thread", "carousel"):
            segments = [hook] + points + [cta]
            body = body_core
        else:
            body = f"{body_core}"

        full = "\n".join([hook, body, cta])
        return {
            "hook": hook,
            "body": body,
            "cta": cta,
            "hashtags": [f"#{w}" for w in voice_words[:3]],
            "segments": segments,
            "char_count": len(full),
        }

    def _t_copy_en(self, ctx: dict) -> dict:
        """English fallback copy — 结构化英文脚手架(真驱动会写得地道;这是兜底,
        但至少语言正确,不会给英文渠道产出中文)。"""
        topic = ctx.get("topic", {})
        fmt = ctx.get("format", "text")
        material = (ctx.get("material") or "").strip()
        msents = [s for s in _sentences(material) if _lang_of(s) == "en"][:3]
        kws = [k for k in _keywords(material, 6) if re.match(r"^[A-Za-z]", k)]
        title = topic.get("title", "this")

        hook = (_shorten_en(msents[0], 70) if msents
                else f"Here's what most people get wrong about {kws[0] if kws else title}.")
        if ctx.get("critique"):  # QA 重写:换更强的数字/问句钩子,避免原地打转
            hook = f"3 things about {kws[0] if kws else title} most people miss:"
        points = [f"- {_shorten_en(s, 80)}" for s in msents[1:]] or [
            "- It saves hours of manual work every week.",
            "- Built for people who'd rather grow the business than fight the tools.",
        ]
        body = "\n".join(points)
        cta = "Try it and see the difference — link in bio 👇"
        segments = [hook] + points + [cta] if fmt in ("thread", "carousel") else []
        full = "\n".join([hook, body, cta])
        return {"hook": hook, "body": body, "cta": cta,
                "hashtags": [f"#{k.lower()}" for k in kws[:3]],
                "segments": segments, "char_count": len(full)}

    # ------------------------------ ⑥ visual ------------------------------ #
    def _t_visual(self, ctx: dict) -> dict:
        brand = ctx.get("brand", {})
        topic = ctx.get("topic", {})
        copy = ctx.get("copy", {})
        style = brand.get("visual_style", "干净、对比强")
        palette = brand.get("color_palette", PALETTES[0])
        hook = copy.get("hook", topic.get("title", ""))
        prompt = (
            f"A {style} social poster, bold big headline '{hook[:40]}', "
            f"brand palette {palette}, minimal geometric shapes, high contrast, "
            f"no watermark, editorial layout, 4:5"
        )
        return {
            "prompt": prompt,
            "alt_text": f"海报:{hook[:60]}",
            "layout": "quote_card",
            "palette": palette,
        }

    # ------------------------------- ⑦ video ------------------------------ #
    def _t_video(self, ctx: dict) -> dict:
        brand = ctx.get("brand", {})
        topic = ctx.get("topic", {})
        copy = ctx.get("copy", {})
        platform = ctx.get("platform", "tiktok")
        hook = copy.get("hook", topic.get("title", ""))

        raw_points = copy.get("segments") or [copy.get("body", "")]
        points = [p for p in raw_points if p][:4] or [topic.get("title", "")]

        scenes = [
            {
                "index": 0,
                "duration_sec": 2.5,
                "visual": "大字标题 + 快速放大,抓住注意力",
                "voiceover": hook,
                "on_screen_text": hook[:24],
            }
        ]
        for i, p in enumerate(points, start=1):
            scenes.append(
                {
                    "index": i,
                    "duration_sec": 3.5,
                    "visual": f"要点 {i}:图标 + 关键词动效",
                    "voiceover": p,
                    "on_screen_text": p[:24],
                }
            )
        scenes.append(
            {
                "index": len(scenes),
                "duration_sec": 2.0,
                "visual": "品牌收尾卡 + CTA",
                "voiceover": copy.get("cta", "关注我们了解更多"),
                "on_screen_text": brand.get("brand_name", "YominPost"),
            }
        )
        total = round(sum(s["duration_sec"] for s in scenes), 1)
        return {
            "hook_line": hook,
            "total_duration_sec": total,
            "scenes": scenes,
            "caption": f"{hook}  " + " ".join(copy.get("hashtags", [])),
            "music_mood": _pick(["upbeat", "chill", "cinematic", "energetic"], hook),
        }

    # ------------------------------- plan --------------------------------- #
    def _t_plan(self, ctx: dict) -> dict:
        material = ctx.get("material", "")
        tf = ctx.get("target_format", "auto")
        plans = [rule_plan(ch.get("platform", "x"), material, tf)
                 for ch in ctx.get("channels", [])]
        return {"plans": plans}

    # ================== Post Creating Loop 确定性回退 ================== #
    def _t_analyze(self, ctx: dict) -> dict:
        """素材剖析回退:规则化提取主题/痛点/价值点/语言。notes 只作指令,不进内容。"""
        material = (ctx.get("material") or "").strip()
        sents = _sentences(material)
        subject = (sents[0][:60] if sents else material[:60]) or "本次素材"
        pains = [s for s in sents if re.search(
            r"痛点|难|问题|烦|费时|浪费|错|失败|pain|hard|struggle|problem|waste|slow", s)][:3]
        values = [s for s in sents if re.search(
            r"帮助|让|提升|节省|更快|更好|减少|解决|help|save|boost|improve|solve|faster", s)][:4]
        proofs = [s for s in sents if re.search(r"\d", s)][:3]
        return {
            "subject": subject,
            "summary": " ".join(sents[:3])[:300] or subject,
            "audience": "成长期的创业者/运营者",
            "pain_points": pains,
            "value_props": values or sents[1:4],
            "proof_points": proofs,
            "keywords": _keywords(material, 8),
            "tone": "真诚、具体",
            "language_hint": _lang_of(material),
            "topic_title": subject,
        }

    def _t_research(self, ctx: dict) -> dict:
        """平台 research 回退:内置各平台增长打法手册(静态知识,无联网)。"""
        platform = ctx.get("platform", "x")
        pb = PLATFORM_PLAYBOOK.get(platform, PLATFORM_PLAYBOOK["_default"])
        kws = (ctx.get("analysis") or {}).get("keywords", [])[:4]
        return {
            "platform": platform,
            "winning_formats": pb["formats"],
            "angles": [{"angle": a, "why": w, "potential": p} for a, w, p in pb["angles"]],
            "hook_patterns": pb["hooks"],
            "hashtag_strategy": [f"#{k}" for k in kws] + pb["hashtags"],
            "pitfalls": pb["pitfalls"],
            "algorithm_notes": pb["algo"],
        }

    def _t_strategy(self, ctx: dict) -> dict:
        """收敛回退:target_format 优先,其次 research 的优势形式,受能力池约束。"""
        platform = ctx.get("platform", "x")
        analysis = ctx.get("analysis") or {}
        research = ctx.get("research") or {}
        kinds = ctx.get("available_kinds") or []
        tf = ctx.get("target_format", "auto")

        if tf and tf != "auto":
            fmt = {"text": "text", "image_text": "image_text", "video": "short_video"}.get(tf, "text")
        else:
            wins = research.get("winning_formats") or []
            fmt = wins[0] if wins else rule_plan(platform, analysis.get("summary", ""), tf)["format"]
        if fmt == "short_video" and "video" not in kinds:
            fmt = "image_text" if "image" in kinds else "text"
        if fmt in ("image_text", "carousel") and "image" not in kinds:
            fmt = "text"

        angles = research.get("angles") or []
        top = angles[0] if angles else {"angle": "故事式 — 从真实场景讲起", "why": ""}
        points = (analysis.get("value_props") or [])[:2] + (analysis.get("pain_points") or [])[:1]
        return {
            "format": fmt,
            "angle": top.get("angle", ""),
            "key_points": points or [analysis.get("summary", "")[:80]],
            "tone": analysis.get("tone", "真诚、具体"),
            "hashtags": [f"#{k}" for k in (analysis.get("keywords") or [])[:3]],
            "hook_direction": (research.get("hook_patterns") or ["直接点出痛点"])[0],
            "cta_goal": "引导查看/试用产品",
            "needs_image": fmt in ("image_text", "carousel", "short_video") and "image" in kinds,
            "needs_video": fmt == "short_video" and "video" in kinds,
            "reason": top.get("why") or f"{platform} 适合 {fmt}",
        }

    # -------------------------------- ⑧ qa -------------------------------- #
    def _t_qa(self, ctx: dict) -> dict:
        brand = ctx.get("brand", {})
        topic = ctx.get("topic", {})
        copy = ctx.get("copy", {})
        platform = ctx.get("platform", "x")

        text = "\n".join(
            [copy.get("hook", ""), copy.get("body", ""), copy.get("cta", "")]
        )
        vocab = set(brand.get("vocabulary", []))
        taboos = brand.get("taboos", [])
        issues = []

        # 1) on-brand:词汇重合度
        overlap = len(vocab & set(_keywords(text, 20)))
        on_brand = min(1.0, 0.5 + 0.1 * overlap)

        # 2) hook 强度:是否含数字 / 问句 / 长度合适
        hook = copy.get("hook", "")
        hook_strength = 0.4
        if re.search(r"\d", hook):
            hook_strength += 0.3
        if re.search(r"[?？]", hook) or "别" in hook or "错" in hook:
            hook_strength += 0.2
        if 8 <= len(hook) <= 40:
            hook_strength += 0.1
        hook_strength = min(1.0, hook_strength)
        if hook_strength < 0.6:
            issues.append(
                {"severity": "medium", "dimension": "hook", "message": "开头钩子偏弱",
                 "fix": "用数字或反常识判断开场"}
            )

        # 3) CTA
        cta_presence = 1.0 if copy.get("cta") else 0.0
        if not copy.get("cta"):
            issues.append(
                {"severity": "low", "dimension": "cta", "message": "缺少行动号召",
                 "fix": "结尾加一句明确 CTA"}
            )

        # 4) 合规:违禁词
        hit = [t for t in taboos if t in text]
        compliance = 1.0 if not hit else max(0.0, 1.0 - 0.25 * len(hit))
        if hit:
            issues.append(
                {"severity": "high", "dimension": "compliance",
                 "message": f"命中慎用词:{hit}", "fix": "替换为不含绝对化表述的措辞"}
            )

        # 5) 平台字数适配
        limits = {
            "x": 280, "linkedin": 3000, "instagram": 2200, "tiktok": 2200,
            "youtube": 5000, "xiaohongshu": 1000, "wechat": 20000,
        }
        limit = limits.get(platform, 2000)
        length = copy.get("char_count", len(text))
        platform_fit = 1.0 if length <= limit else max(0.0, 1.0 - (length - limit) / limit)
        if length > limit:
            issues.append(
                {"severity": "medium", "dimension": "platform_fit",
                 "message": f"超出 {platform} 建议字数 ({length}/{limit})",
                 "fix": "压缩正文或拆成多条"}
            )

        # 6) 清晰度:平均句长
        sents = _sentences(text)
        avg = sum(len(s) for s in sents) / max(1, len(sents))
        clarity = 1.0 if avg <= 40 else max(0.3, 1.0 - (avg - 40) / 80)

        scores = {
            "on_brand": round(on_brand, 2),
            "hook": round(hook_strength, 2),
            "cta": round(cta_presence, 2),
            "compliance": round(compliance, 2),
            "platform_fit": round(platform_fit, 2),
            "clarity": round(clarity, 2),
        }
        # 合规权重更高
        weights = {"on_brand": 1, "hook": 1.2, "cta": 0.6,
                   "compliance": 1.5, "platform_fit": 1, "clarity": 0.8}
        overall = round(
            sum(scores[k] * weights[k] for k in scores) / sum(weights.values()), 3
        )
        passed = overall >= 0.7 and compliance >= 0.75
        return {
            "scores": scores,
            "overall": overall,
            "passed": passed,
            "issues": issues,
        }
