"""业务服务层:把「8 能力槽生成」与「渠道发布」编排成产品级用例。

- generate_content:选题+平台 → 走 Router/文案/QA/视觉/短视频 → 返回可入库的内容
- create_post / publish_post:草稿 → 定时/立即发布 → 回填永久链接
- run_due_posts:定时器 tick,把到期的定时帖发出去
- seed_defaults:首次启动播种一个 workspace + 若干渠道/话题(便于演示与验收)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from . import db

log = logging.getLogger("yominpost.services")
from .config import make_provider
from .connectors import get_connector
from .connectors.oauth_conn import is_auth_error
from .models import (
    Asset,
    Channel,
    ChannelStatus,
    GenerateRequest,
    PLATFORM_BY_ID,
    Post,
    PostContent,
    PostCreate,
    PostStatus,
    PublishTarget,
    Topic,
    TopicStatus,
    Workspace,
)
from .schemas import BrandSource, ContentFormat, Platform, TopicIdea
from .stages import brand_dna, copy as copy_stage, qa as qa_stage, router, trends as trends_stage, video, visual


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_provider():
    """按 settings 里选择的驱动构造 provider(未设置则用 env/默认)。"""
    from . import db as _db
    from .config import make_provider

    return make_provider(_db.get_setting("provider"))


# 每个渠道的写作风格指令(喂给生成器,让同一素材各渠道各写一版)
STYLE_BY_PLATFORM = {
    "x": "X/Twitter:≤270字,强钩子开头,1个核心观点,口语短句,最多1-2个话题标签。",
    "linkedin": "LinkedIn:专业有洞察,300-600字,分段清晰,少表情,结尾1个明确观点或行动点。",
    "instagram": "Instagram:视觉化、有情绪的开头,适度emoji,结尾6-10个精选话题标签,鼓励互动。",
    "facebook": "Facebook 主页:亲和叙事式,中等长度,可讲一个小故事或场景,话题标签少而精。",
    "tiktok": "TikTok:口播脚本感,3秒强钩子,短句、节奏快、年轻化。",
    "youtube": "YouTube:标题式钩子 + 简介描述,含可搜索关键词。",
    "xiaohongshu": "小红书:标题党+分点+大量emoji+一串话题标签,种草/干货感。",
    "threads": "Threads:轻松、简短、真实,像随手一条。",
    "reddit": "Reddit:真诚、不吹嘘,像真实用户在分享经验。",
    "mastodon": "Mastodon:简洁、社区友好。",
    "bluesky": "Bluesky:简短轻松。",
    "telegram": "Telegram:清晰、可含链接与要点列表。",
    "discord": "Discord:社群口吻,可用 markdown 与 emoji。",
}

_CAP_FORMAT = {
    "text": (ContentFormat.text, ["copy"]),
    "image_text": (ContentFormat.image_text, ["copy", "visual"]),
    "video": (ContentFormat.short_video, ["copy", "video", "visual"]),
}


def _is_public_url(url: str) -> bool:
    """SSRF 防护:只允许 http(s) + 公网 IP。拦截内网/回环/云元数据地址。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        # 解析所有 A/AAAA,任一落在私网即拒绝
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
        for *_, sa in infos:
            ip = ipaddress.ip_address(sa[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _fetch_page(url: str) -> str:
    """抓取网页正文要点(标题 + 去标签文本)。失败返回空。"""
    import re as _re

    import httpx as _httpx
    from urllib.parse import urljoin
    try:
        cur = url
        with _httpx.Client(timeout=20, follow_redirects=False,
                           headers={"User-Agent": "Mozilla/5.0 (YominPost)"}) as client:
            for _ in range(5):                       # 手动跟随重定向,逐跳做 SSRF 校验
                if not _is_public_url(cur):
                    return ""
                r = client.get(cur)
                if r.is_redirect and r.headers.get("location"):
                    cur = urljoin(cur, r.headers["location"])
                    continue
                break
        html = r.text
        html = _re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
        title = ""
        m = _re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        if m:
            title = _re.sub(r"\s+", " ", m.group(1)).strip()
        text = _re.sub(r"(?s)<[^>]+>", " ", html)
        text = _re.sub(r"\s+", " ", text).strip()
        return (f"[标题] {title}\n" if title else "") + text[:2500]
    except Exception:  # noqa: BLE001
        return ""


_IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_VID_EXT = (".mp4", ".mov", ".webm", ".m3u8")


def parse_material(source: str, notes: str = "") -> dict:
    """多模态 all-in-one 素材解析:文字/网页URL/图片链接/视频链接混合输入,
    自动识别分流并抓取网页要点,产出统一素材包。"""
    import re as _re

    src = (source or "").strip()
    urls = _re.findall(r'https?://[^\s"\'<>]+', src)
    images = [u for u in urls if u.split("?")[0].lower().endswith(_IMG_EXT)]
    videos = [u for u in urls if u.split("?")[0].lower().endswith(_VID_EXT)]
    pages = [u for u in urls if u not in images and u not in videos][:2]

    text = src
    for u in urls:
        text = text.replace(u, " ")
    text = _re.sub(r"\s+", " ", text).strip()

    fetched = [t for t in (_fetch_page(u) for u in pages) if t]
    # notes 是运营者指令(角度/重点),不是素材内容 —— 绝不混入 combined,
    # 否则指令会被当成素材写进产出(仅素材完全为空时才退化用 notes 兜底)。
    combined = "\n".join(x for x in ([text] + fetched) if x)[:3500]
    if not combined and images:
        combined = "一张配图素材"
    return {"text": text, "images": images, "videos": videos, "pages": pages,
            "combined": combined or notes or src}


def _materialize_source(source_type: str, source: str, notes: str = "") -> str:
    """兼容旧接口:统一走多模态解析。"""
    return parse_material(source, notes)["combined"]


# 规划的 format → (ContentFormat, 生成器)
_PLAN_FMT = {
    "text": (ContentFormat.text, ["copy"]),
    "thread": (ContentFormat.thread, ["copy"]),
    "image_text": (ContentFormat.image_text, ["copy", "visual"]),
    "carousel": (ContentFormat.carousel, ["copy", "visual"]),
    "short_video": (ContentFormat.short_video, ["copy", "video", "visual"]),
}


class _Tracer:
    """Provider 包装:记录每次 task 调用真正由哪个驱动完成(live or 兜底)。
    静默降级是这台引擎最初的病根——溯源必须贯穿全链路。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    @property
    def name(self):
        return self.inner.name

    @property
    def is_live(self):
        return getattr(self.inner, "is_live", False)

    def run(self, task: str, ctx: dict) -> dict:
        r = self.inner.run(task, ctx)
        meta = r.pop("_meta", None) if isinstance(r, dict) else None
        meta = meta or {"driver": self.inner.name, "live": False}
        self.calls.append({"task": task, "driver": meta.get("driver", ""),
                           "live": bool(meta.get("live")),
                           "fallback": bool(meta.get("fallback_from")),
                           "reason": meta.get("reason", "")})
        return r

    def since(self, mark: int) -> list[dict]:
        return self.calls[mark:]


def _resolve_language(req, ch, analysis: dict) -> str:
    """输出语言:请求覆盖 > 渠道配置 > 全局 content_language 设置 > 素材语言推断 > 英文。"""
    lang = (getattr(req, "language", "") or "").strip().lower()
    if lang and lang != "auto":
        return lang
    ch_lang = str((ch.config or {}).get("language", "")).strip().lower()
    if ch_lang and ch_lang != "auto":
        return ch_lang
    # 全局内容语言(运营者一次性设定;本产品默认英文 —— 运营者要求全英文产出)
    setting = (db.get_setting("content_language") or "").strip().lower()
    if setting and setting != "auto":
        return setting
    return (analysis or {}).get("language_hint") or "en"


def plan_compose(provider, material: str, channels: list, target_format: str,
                 available_kinds: set[str]) -> dict:
    """规划阶段:AI(或规则回退)为每个渠道决定形式/角度/要点/所需能力。"""
    from .providers.template_provider import rule_plan

    ctx = {
        "material": material[:2500],
        "channels": [{"platform": c.platform, "handle": c.handle or c.display_name}
                     for c in channels],
        "target_format": target_format,
        "available_kinds": sorted(available_kinds),
        "goal": "在各渠道产生流量曝光与转化",
    }
    try:
        plans = provider.run("plan", ctx).get("plans", []) or []
    except Exception:  # noqa: BLE001
        plans = []
    by = {p["platform"]: p for p in plans if isinstance(p, dict) and p.get("platform")}
    for c in channels:
        if c.platform not in by:
            by[c.platform] = rule_plan(c.platform, material, target_format)
        p = by[c.platform]
        # 强约束:目标形式覆盖 + 能力可用性裁剪
        if target_format not in ("", "auto"):
            p["format"] = {"text": "text", "image_text": "image_text",
                           "video": "short_video"}.get(target_format, p.get("format", "text"))
        if "image" not in available_kinds:
            p["needs_image"] = False
        if "video" not in available_kinds:
            p["needs_video"] = False
            if p.get("format") == "short_video":
                p["format"] = "image_text" if "image" in available_kinds else "text"
    return by


def _compose_core(ws: Workspace, req, progress=None) -> dict:
    """Post Creating Loop 核心:
    ① analyze  深度剖析素材(穷尽上下文,notes 只是指令不进内容)
    ② research 逐渠道平台增长 research(有权重/优势/增长空间的形式与角度)
    ③ strategy 收敛出该渠道唯一最优策略(受目标形式与能力池约束)
    ④ generate 按渠道语言生成 → QA 评估 → 带批评迭代
    每一步都记录真实驱动;兜底(template)显式标注,绝不静默。
    progress(step, detail, pct) 供后台任务上报进度。
    """
    from . import capabilities as caps_mod
    from .schemas import FormatDecision
    from .stages.qa import _critique

    def _p(step, detail, pct):
        if progress:
            progress(step, detail, pct)

    provider = _Tracer(active_provider())
    mat = parse_material(req.source, req.notes)
    material = mat["combined"]
    target_format = (getattr(req, "target_format", "auto") or "auto")
    if target_format == "auto" and getattr(req, "capability", "auto") not in ("", "auto"):
        target_format = req.capability   # 旧字段兼容

    channels = [c for c in (db.get_channel(cid) for cid in req.channel_ids) if c]
    if not channels:
        raise ValueError("所选渠道均不存在或已删除,请重新选择渠道")
    kinds = caps_mod.usable_kinds()
    # loop 开关只控制 research/strategy 双阶段;QA 迭代由 loop 或 optimize 决定
    use_loop = bool(getattr(req, "loop", True))
    use_qa_iter = bool(use_loop or req.optimize)
    max_iters = max(1, min(int(getattr(req, "max_iters", 2) or 2), 4))
    n = max(1, len(channels))

    # —— ① 深度剖析素材 —— #
    _p("analyze", "深度剖析素材上下文", 0.05)
    req_lang = (getattr(req, "language", "") or "").strip().lower()
    analysis = provider.run("analyze", {
        "material": material[:3000], "notes": req.notes,
        "assets": {"images": len(mat["images"]), "videos": len(mat["videos"]),
                   "pages": mat["pages"]},
        **({"language": req_lang} if req_lang not in ("", "auto") else {}),
        "goal": "为多渠道社媒内容生成做基础剖析",
    }) or {}
    title = (analysis.get("topic_title") or "").strip() \
        or (material[:40] if material else req.notes.strip() or "本次内容").strip()

    brand = brand_dna.extract(
        ws.brand_name or ws.name or "YominPost",
        [BrandSource(kind="note", text=(material or req.notes or ws.brand_name or ws.name))],
        provider,
    )

    # 非 loop 模式:退回轻量 plan(快速);loop 模式:research+strategy 双阶段
    plan_by_fast = {} if use_loop else plan_compose(
        provider, material, channels, target_format, kinds)

    drafts = []
    for i, ch in enumerate(channels):
        language = _resolve_language(req, ch, analysis)
        mark = len(provider.calls)
        base = 0.1 + 0.85 * (i / n)          # 该渠道的进度基线(严格单调递增)
        step = 0.85 / n

        # —— ② 平台 research(loop 模式) —— #
        research = {}
        if use_loop:
            _p("research", f"{ch.platform} 平台增长 research", base)
            research = provider.run("research", {
                "platform": ch.platform, "analysis": analysis, "language": language,
                "goal": "找出该平台该领域有权重、有优势、有增长空间的内容打法",
            }) or {}

            # —— ③ 收敛策略 —— #
            _p("strategy", f"{ch.platform} 收敛内容策略", base + 0.3 * step)
            chplan = provider.run("strategy", {
                "platform": ch.platform, "analysis": analysis, "research": research,
                "target_format": target_format, "available_kinds": sorted(kinds),
                "language": language, "operator_notes": req.notes,
                "goal": "在该渠道产生流量曝光与转化",
            }) or {}
            # 强约束:目标形式覆盖 + 能力可用性裁剪(不信模型的自觉)
            if target_format not in ("", "auto"):
                chplan["format"] = {"text": "text", "image_text": "image_text",
                                    "video": "short_video"}.get(target_format,
                                                                chplan.get("format", "text"))
            if "image" not in kinds:
                chplan["needs_image"] = False
                # 无生图能力且用户也没给图 → 图文降级纯文本
                if chplan.get("format") in ("image_text", "carousel") and not mat["images"]:
                    chplan["format"] = "text"
            if "video" not in kinds:
                chplan["needs_video"] = False
                if chplan.get("format") == "short_video":
                    chplan["format"] = "image_text" if "image" in kinds else "text"
        else:
            chplan = plan_by_fast.get(ch.platform) or {}

        fmt, gens = _PLAN_FMT.get(chplan.get("format", "text"), _PLAN_FMT["text"])
        if chplan.get("needs_image") and "visual" not in gens:
            gens = gens + ["visual"]
        try:
            platform = Platform(ch.platform)
        except ValueError:
            platform = Platform.x
        topic = TopicIdea(id="compose", title=title,
                          angle=chplan.get("angle", ""), suggested_platform=platform)
        decision = FormatDecision(topic_id="compose", format=fmt, platform=platform,
                                  generators=gens, reasons=[chplan.get("reason", "")],
                                  rule_trace=["strategy" if use_loop else "planner"])
        style = STYLE_BY_PLATFORM.get(ch.platform, "通用:清晰有力、可直接发布。")
        if chplan.get("angle"):
            style += f" 内容角度:{chplan['angle']}。"
        if chplan.get("key_points"):
            style += " 必须覆盖要点:" + ";".join(str(k) for k in chplan["key_points"][:3])
        if chplan.get("hook_direction"):
            style += f" 钩子方向:{chplan['hook_direction']}。"
        if chplan.get("cta_goal"):
            style += f" CTA 目标:{chplan['cta_goal']}。"
        if req.notes.strip():
            style += f" 运营者指令(遵循但不要原样照抄进文案):{req.notes.strip()}。"

        # —— ④ 生成 + 评估 + 迭代 —— #
        _p("generate", f"{ch.platform} 生成 + QA 迭代", base + 0.5 * step)
        draft = copy_stage.write(brand, topic, decision, provider,
                                 material=material, style=style, language=language)
        report = qa_stage.evaluate(brand, topic, draft, decision, provider)
        iterations = [report.overall]
        while use_qa_iter and not report.passed and len(iterations) <= max_iters:
            crit = _critique(report)
            cand = copy_stage.write(brand, topic, decision, provider, critique=crit,
                                    material=material, style=style, language=language)
            cand_r = qa_stage.evaluate(brand, topic, cand, decision, provider)
            if cand_r.overall >= report.overall:
                draft, report = cand, cand_r
                report.revised = True
            iterations.append(cand_r.overall)

        # —— 素材来源链接:附到 CTA 末尾(hashtags 之前),让读者能找到落地页 —— #
        # 运营者给的 URL 是最想推的落地页;社媒 caption 惯例把它放在正文尾、标签前。
        src_link = (mat.get("pages") or [""])[0]
        if src_link and src_link not in (draft.cta or "") and src_link not in (draft.body or ""):
            tail = f"🔗 {src_link}"
            draft.cta = (draft.cta.rstrip() + "\n\n" + tail) if draft.cta else tail

        # —— 资产(按策略 + 能力池) —— #
        stem = f"{ws.id}_{ch.id}_{uuid.uuid4().hex[:6]}"
        ext_image = mat["images"][0] if mat["images"] else ""
        visual_svg = ""
        video_srt = ""
        if not ext_image and "visual" in gens and "image" in kinds:
            visual_svg = visual.render(
                brand, topic, draft, provider, stem=stem,
                platform=ch.platform, language=language,
                key_points=[str(k) for k in (chplan.get("key_points") or [])[:3]],
                style=chplan.get("angle", "")).svg_path
        if "video" in gens and "video" in kinds:
            video_srt = video.storyboard(brand, topic, draft, provider,
                                         platform=platform, stem=stem).srt_path

        # —— 该渠道各阶段驱动溯源 —— #
        ch_calls = provider.since(mark)
        drivers = {}
        for c in ch_calls:
            drivers.setdefault(c["task"], c["driver"])
        copy_live = any(c["task"] == "copy" and c["live"] for c in ch_calls)

        drafts.append({
            "channel_id": ch.id, "platform": ch.platform,
            "handle": ch.handle or ch.display_name, "format": decision.format.value,
            "language": language,
            "content": {"hook": draft.hook, "body": draft.body, "cta": draft.cta,
                        "hashtags": draft.hashtags or chplan.get("hashtags", []),
                        "segments": draft.segments},
            "char_count": draft.char_count, "qa": report.model_dump(),
            "plan": {k: chplan.get(k) for k in
                     ("format", "angle", "key_points", "tone", "reason",
                      "needs_image", "needs_video", "hook_direction", "cta_goal")},
            "research": {k: research.get(k) for k in
                         ("winning_formats", "angles", "hook_patterns",
                          "pitfalls", "algorithm_notes")} if research else {},
            "iterations": [round(x, 3) for x in iterations],
            "drivers": drivers, "fallback": not copy_live,
            "visual_svg": visual_svg, "video_srt": video_srt, "ext_image": ext_image,
        })

    all_fallback = bool(drafts) and all(d["fallback"] for d in drafts)
    result = {"material_preview": material[:500],
              "material_assets": {"images": mat["images"], "videos": mat["videos"],
                                  "pages": mat["pages"]},
              "driver": provider.name, "capabilities": sorted(kinds),
              "analysis": {k: analysis.get(k) for k in
                           ("subject", "summary", "audience", "pain_points",
                            "value_props", "language_hint", "topic_title")},
              "trace": provider.calls, "fallback": all_fallback,
              "drafts": drafts}
    _p("done", "", 1.0)
    return result


def compose(ws: Workspace, req) -> dict:
    """同步版 compose(测试/兼容)。Web 层的异步入口是 start_compose。"""
    result = _compose_core(ws, req)
    run = _new_compose_run(ws, req, status="done")
    _fill_run(run, result)
    try:
        db.save_compose_run(run)
        result["run_id"] = run.id
    except Exception:  # noqa: BLE001  — 生成结果已可用,落库失败不影响返回
        log.exception("save_compose_run failed")
    return result


# --------------------------------------------------------------------------- #
# 异步 compose:立即返回 run_id,后台线程跑 Loop,前端轮询进度
# --------------------------------------------------------------------------- #
def _new_compose_run(ws: Workspace, req, status: str) -> "ComposeRun":
    from .models import ComposeRun
    return ComposeRun(
        id=db.new_id("cmp"), workspace_id=ws.id, status=status,
        request={"source": req.source, "notes": req.notes,
                 "channel_ids": req.channel_ids,
                 "target_format": getattr(req, "target_format", "auto"),
                 "language": getattr(req, "language", "auto"),
                 "loop": bool(getattr(req, "loop", True))},
    )


def _fill_run(run, result: dict) -> None:
    run.material_preview = result.get("material_preview", "")
    run.driver = result.get("driver", "")
    run.capabilities = result.get("capabilities", [])
    run.material_assets = result.get("material_assets", {})
    run.drafts = result.get("drafts", [])
    run.analysis = result.get("analysis", {})
    run.trace = result.get("trace", [])
    run.fallback = bool(result.get("fallback"))


MAX_RUNNING_COMPOSE = 3   # 每个 workspace 同时运行的 compose 上限(CLI 驱动很重)


def start_compose(ws: Workspace, req) -> str:
    """启动后台 compose,立即返回可轮询的 run_id。超过并发上限抛 ValueError。"""
    import threading
    import time as _time

    running = [r for r in db.list_compose_runs(ws.id, limit=20) if r.status == "running"]
    if len(running) >= MAX_RUNNING_COMPOSE:
        raise ValueError(f"已有 {len(running)} 个生成在运行,请等待完成或从历史查看")

    run = _new_compose_run(ws, req, status="running")
    run.progress = {"step": "queued", "detail": "排队中", "pct": 0.0}
    db.save_compose_run(run)

    def _save_final():
        # 终态写入决定 run 是否永远卡在 running —— 必须带重试
        for attempt in range(3):
            try:
                db.save_compose_run(run)
                return
            except Exception:  # noqa: BLE001
                log.exception("final save failed (attempt %d) run=%s", attempt + 1, run.id)
                _time.sleep(1 + attempt)
        log.error("run %s 终态写入彻底失败,将由启动回收标记为 failed", run.id)

    def _job():
        def _progress(step, detail, pct):
            run.progress = {"step": step, "detail": detail, "pct": round(pct, 2)}
            try:
                db.save_compose_run(run)
            except Exception:  # noqa: BLE001
                log.exception("progress save failed")

        try:
            result = _compose_core(ws, req, progress=_progress)
            _fill_run(run, result)
            run.status = "done"
            run.progress = {"step": "done", "detail": "", "pct": 1.0}
        except Exception as e:  # noqa: BLE001
            log.exception("compose run %s failed", run.id)
            run.status = "failed"
            run.error = f"{type(e).__name__}: {e}"[:300]
        _save_final()

    threading.Thread(target=_job, name=f"compose-{run.id}", daemon=True).start()
    return run.id


def recover_stuck_compose_runs(max_age_sec: int = 0) -> int:
    """回收卡在 'running' 的 compose run → failed。
    max_age_sec=0(启动时):所有 running 都是孤儿(线程不跨进程存活),全回收。
    max_age_sec>0(周期调用):只回收 created_at 超过该时长的(防活进程里罕见的写失败卡死)。"""
    from datetime import timedelta
    cutoff = ""
    if max_age_sec > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)).isoformat(timespec="seconds")
    n = 0
    for ws in db.list_workspaces():
        for r in db.list_compose_runs(ws.id, limit=200):
            if r.status == "running" and (not cutoff or (r.created_at or "") < cutoff):
                r.status = "failed"
                r.error = "生成超时/中断 —— 请重新生成"
                db.save_compose_run(r)
                n += 1
    return n


# --------------------------------------------------------------------------- #
# 内容生成(8 能力槽,单选题聚焦版)
# --------------------------------------------------------------------------- #
def generate_content(ws: Workspace, req: GenerateRequest) -> dict:
    provider = active_provider()
    sources = [BrandSource(kind="note", text=s) for s in req.brand_sources if s.strip()]
    if not sources:
        sources = [BrandSource(kind="note", text=ws.brand_name or ws.name)]

    brand = brand_dna.extract(ws.brand_name or ws.name or "YominPost", sources, provider)

    topic_title = req.topic.strip()
    if not topic_title and req.topic_id:
        t = db.get_topic(req.topic_id)
        if t:
            topic_title = t.title
    topic_title = topic_title or "本周主打选题"

    try:
        platform = Platform(req.platform)
    except ValueError:
        platform = Platform.x

    trend_signals = trends_stage.gather(brand, req.trends, provider)
    topic = TopicIdea(
        id=req.topic_id or "adhoc",
        title=topic_title,
        angle="清单式 — 把复杂问题拆成 N 步" if any(
            k in topic_title for k in ("步", "清单", "对比", "方法")
        ) else "故事式 — 从一个真实场景讲起",
        suggested_platform=platform,
        trend_refs=[t.title for t in trend_signals[:1]],
    )

    decision = router.decide(topic, brand)
    draft = copy_stage.write(brand, topic, decision, provider)
    if req.optimize:
        final, report = qa_stage.review(brand, topic, draft, decision, provider)
    else:
        final = draft
        report = qa_stage.evaluate(brand, topic, draft, decision, provider)

    stem = f"{ws.id}_{uuid.uuid4().hex[:8]}"
    visual_svg = ""
    video_srt = ""
    # 尊重能力池:Settings 里关掉图像/视频时,Studio 也不应偷偷生成
    # (镜像 _compose_core 的裁剪逻辑;否则 codex 生图会同步卡住 Web 请求)。
    from . import capabilities as _caps
    kinds = _caps.usable_kinds()
    if "visual" in decision.generators and "image" in kinds:
        visual_svg = visual.render(brand, topic, final, provider, stem=stem).svg_path
    if "video" in decision.generators and "video" in kinds:
        video_srt = video.storyboard(
            brand, topic, final, provider, platform=platform, stem=stem
        ).srt_path

    return {
        "content": {
            "hook": final.hook,
            "body": final.body,
            "cta": final.cta,
            "hashtags": final.hashtags,
            "segments": final.segments,
        },
        "format": decision.format.value,
        "router": decision.model_dump(),
        "qa": report.model_dump(),
        "visual_svg": visual_svg,
        "video_srt": video_srt,
        "brand": brand.model_dump(),
        "trends": [t.model_dump() for t in trend_signals],
        "char_count": final.char_count,
        "driver": provider.name,
    }


# --------------------------------------------------------------------------- #
# Post 生命周期
# --------------------------------------------------------------------------- #
def normalize_dt(s: str) -> str:
    """把客户端来的时间统一成 tz-aware UTC ISO(与 _now() 同格式 '+00:00'),
    否则定时比较(字符串)与本地时区显示都会错。空 → "";非法 → 抛 ValueError。

    ⚠️ 绝不能把非法时间原样返回:due_scheduled_posts 用字符串比较判定到期,
    像 "07/15/2026 10:00" 这种非 ISO 串按字典序 < 当前时间 → 会被立刻当作到期
    发布到真实平台(错误的提前投递);"tomorrow 9am" 又永远不到期而静默卡死。
    故解析失败必须显式报错,由 API 层转成 400,让运营者改正而不是错发。"""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:                 # 无时区 → 视为 UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"无法识别的时间格式: {s!r}(需 ISO 8601,如 2026-07-15T10:00)") from e


# --------------------------------------------------------------------------- #
# 素材库分类(文件夹式:published / uploaded / external)
# --------------------------------------------------------------------------- #
ASSET_SOURCES = ("published", "uploaded", "external")


def asset_source(a: Asset) -> str:
    """派生素材归属桶。显式 a.source 优先;否则按 path/meta 启发式分类 → 旧数据零迁移。"""
    if a.source in ASSET_SOURCES:
        return a.source
    meta = a.meta or {}
    if meta.get("source") in ASSET_SOURCES:
        return meta["source"]
    if "/up_" in (a.path or ""):        # 本地上传:文件名前缀 up_
        return "uploaded"
    if meta.get("post_id"):             # 由发帖 / 画布生成的海报 / 配图
        return "published"
    return "published"                  # 兜底:视为平台产出


_ASSET_STOP = {
    "the", "and", "for", "with", "your", "you", "are", "how", "why", "what", "this",
    "that", "から", "amazon", "listing", "的", "了", "在", "和", "是", "做", "个", "这",
    "一个", "如何", "怎么", "可以", "我们", "你的",
}


def _kw_tokens(text: str) -> set[str]:
    import re
    toks = set()
    for w in re.split(r"[^0-9a-z一-鿿]+", (text or "").lower()):
        if len(w) >= 3 and w not in _ASSET_STOP:
            toks.add(w)
        elif len(w) >= 2 and any("一" <= c <= "鿿" for c in w) and w not in _ASSET_STOP:
            toks.add(w)                       # 保留 2 字中文词
    return toks


def rank_assets_by_relevance(text: str, items: list[dict], limit: int = 8) -> list[dict]:
    """按输入文本与素材(标题/摘要/标签/渠道)的关键词重合度排序,返回最相关的若干条。
    标题与标签命中权重更高。无关(0 分)的不返回,避免硬凑。"""
    kws = _kw_tokens(text)
    if not kws:
        return []
    scored = []
    for d in items:
        m = d.get("meta") or {}
        title = str(d.get("title") or "").lower()
        summary = str(m.get("summary") or "").lower()
        tags = " ".join(m.get("tags") or []).lower()
        score = 0
        for k in kws:
            if k in tags:
                score += 3
            if k in title:
                score += 2
            if k in summary:
                score += 1
        if score:
            scored.append((score, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in scored[:limit]]


def asset_folder(a: Asset) -> str:
    """素材所属文件夹(external 用二级目录,如 helium10/blog)。"""
    if a.folder:
        return a.folder
    meta = a.meta or {}
    if asset_source(a) == "external":
        site = str(meta.get("site") or "").strip("/")
        channel = str(meta.get("channel") or "").strip("/")
        return "/".join(x for x in (site, channel) if x)
    return ""


def _post_asset_specs(post: Post) -> list[tuple[str, str]]:
    """post 当前引用的素材:(kind, path)。海报=svg,多图=image。"""
    specs: list[tuple[str, str]] = []
    if post.visual_svg:
        specs.append(("svg", post.visual_svg))
    for mp in (post.media or []):
        specs.append(("image", mp))
    return specs


def _sync_post_assets(ws: Workspace, post: Post) -> None:
    """把素材库中属于该 post 的行,对账到它当前引用的图/海报:
    - 按 (post_id, path) 幂等:已存在的 path 不重复登记;
    - 移除该 post 不再引用的旧行 → 避免重开画布反复自动落盘导致 asset 行无界累积。
    只动 meta.post_id 命中的行,手动上传/别的 post 的素材不受影响。"""
    specs = _post_asset_specs(post)
    desired = {path for _, path in specs}
    existing = [a for a in db.list_assets(ws.id) if (a.meta or {}).get("post_id") == post.id]
    have = {a.path for a in existing}
    for a in existing:
        if a.path not in desired:
            db.delete_asset(a.id)
    for kind, path in specs:
        if path in have:
            continue
        db.save_asset(Asset(
            id=db.new_id("asset"), workspace_id=ws.id, kind=kind,
            title=post.title, path=path, created_at=_now(), meta={"post_id": post.id},
        ))


def _find_draft_by_dedupe(ws_id: str, key: str) -> Post | None:
    """按稳定幂等键查同工作区仍为 draft 的帖(仅 draft:已排期/已发布绝不被自动改写)。"""
    for p in db.list_posts(ws_id, status=PostStatus.draft.value):
        if (p.meta or {}).get("dedupe_key") == key:
            return p
    return None


def create_post(ws: Workspace, data: PostCreate) -> Post:
    targets: list[PublishTarget] = []
    for ch_id in data.channel_ids:
        ch = db.get_channel(ch_id)
        if ch:
            targets.append(PublishTarget(channel_id=ch.id, platform=ch.platform))

    sched = normalize_dt(data.scheduled_at)
    status = PostStatus.scheduled if sched else PostStatus.draft

    # 画布自动落盘幂等:同一 (渠道+素材) 的稳定 dedupe_key 命中现存草稿 → 就地更新,
    # 不新建重复草稿(重开/刷新画布丢了 c.postId 也不会重复建帖、重复写素材库)。
    # 仅对未排期的自动草稿生效,且只认领 draft;已排期/已发布的帖不被降级或覆盖。
    if data.dedupe_key and not sched:
        existing = _find_draft_by_dedupe(ws.id, data.dedupe_key)
        if existing:
            existing.title = data.title or (
                data.content.hook[:40] if data.content.hook else existing.title)
            existing.content = data.content
            existing.format = data.format
            existing.targets = targets
            existing.topic_id = data.topic_id or existing.topic_id
            existing.run_id = data.run_id or existing.run_id
            existing.visual_svg = data.visual_svg
            existing.video_srt = data.video_srt
            existing.media = data.media
            existing.qa = data.qa
            db.save_post(existing)
            _sync_post_assets(ws, existing)
            return existing

    post = Post(
        id=db.new_id("post"),
        workspace_id=ws.id,
        title=data.title or (data.content.hook[:40] if data.content.hook else "未命名草稿"),
        content=data.content,
        format=data.format,
        status=status,
        targets=targets,
        scheduled_at=sched,
        topic_id=data.topic_id,
        run_id=data.run_id,
        visual_svg=data.visual_svg,
        video_srt=data.video_srt,
        media=data.media,
        qa=data.qa,
        created_at=_now(),
        meta={"dedupe_key": data.dedupe_key} if data.dedupe_key else {},
    )
    db.save_post(post)
    _sync_post_assets(ws, post)
    return post


def publish_post(post: Post) -> Post:
    """把一篇帖子发布到它所有已连接渠道,回填永久链接。

    幂等:原子认领(仅非 publishing 才进入),防止双击/重试并发重复发布。
    """
    if not post.targets:
        post.status = PostStatus.failed
        post.meta["error"] = "没有选择任何渠道"
        return db.save_post(post)

    if not db.claim_post_for_publish(post.id):
        # 已在发布中或已发布 → 直接返回当前状态,不再重复发送
        return db.get_post(post.id) or post
    fresh = db.get_post(post.id) or post
    fresh.status = PostStatus.publishing
    return _publish_targets(fresh)


def sync_channel_posts(ws: Workspace, channel: Channel, limit: int = 25) -> dict:
    """从平台拉取该渠道账号最近已发帖,与本地对账:
    - external_id 命中本地帖 → 回填 permalink + 平台图 URL(即使本地图丢了也能显示真图)。
    - 平台上有、本地没有的 → 导入为一条 posted(synced=platform),让 Posts 反映账号真实内容。
    返回 {fetched, updated, imported}。"""
    conn = get_connector(channel.platform)
    try:
        remote = conn.list_recent(channel, limit=limit)
    except Exception:  # noqa: BLE001
        log.exception("list_recent failed for %s", channel.platform)
        return {"fetched": 0, "updated": 0, "imported": 0, "error": "拉取失败"}
    if not remote:
        return {"fetched": 0, "updated": 0, "imported": 0}

    # 本地已发帖按 external_id 建索引
    local_by_ext = {}
    for p in db.list_posts(ws.id):
        for t in p.targets:
            if t.external_id:
                local_by_ext[str(t.external_id)] = (p, t)

    updated = imported = 0
    for r in remote:
        ext = str(r.get("external_id", ""))
        if not ext:
            continue
        murl = r.get("media_url", "")
        if ext in local_by_ext:
            p, t = local_by_ext[ext]
            changed = False
            if r.get("permalink") and t.permalink != r["permalink"]:
                t.permalink = r["permalink"]; changed = True
            if murl and (p.meta or {}).get("platform_media_url") != murl:
                p.meta = dict(p.meta or {}); p.meta["platform_media_url"] = murl; changed = True
            if changed:
                db.save_post(p); updated += 1
        else:
            # 平台上有、本地无 → 导入为只读同步帖
            post = Post(
                id=db.new_id("post"), workspace_id=ws.id, status=PostStatus.posted,
                title=(r.get("text", "") or f"{channel.platform} 帖").strip()[:40],
                content=PostContent(body=r.get("text", "")),
                targets=[PublishTarget(channel_id=channel.id, platform=channel.platform,
                                       status="posted", permalink=r.get("permalink", ""),
                                       external_id=ext)],
                posted_at=r.get("created_at", "") or _now(),
                meta={"synced_from": channel.platform, "platform_media_url": murl},
            )
            db.save_post(post); imported += 1
    return {"fetched": len(remote), "updated": updated, "imported": imported}


def sync_all_channels(ws: Workspace, limit: int = 25) -> dict:
    """同步该 workspace 下所有已连接、支持拉取的渠道(目前 FB/IG)。"""
    from . import platform_risk
    total = {"fetched": 0, "updated": 0, "imported": 0, "channels": 0}
    for ch in db.list_channels(ws.id):
        if ch.status != ChannelStatus.connected or ch.platform not in ("facebook", "instagram"):
            continue
        if platform_risk.frozen_until(ch):
            continue              # 熔断期内连读接口也不碰

        r = sync_channel_posts(ws, ch, limit=limit)
        total["fetched"] += r.get("fetched", 0)
        total["updated"] += r.get("updated", 0)
        total["imported"] += r.get("imported", 0)
        total["channels"] += 1
    return total


_LAST_SYNC_SETTING = "last_channel_data_sync"


def sync_channel_data(ws: Workspace) -> dict:
    """一次「渠道数据同步」:① 拉账号真实内容(sync_all_channels)② 刷新平台原生健康
    画像(account_intel.refresh_all,规则版不烧 LLM)。这是**统一入口** —— 手动刷新按钮
    与每日自动 tick 都走这里,保证「账号状态」与「内容事实」总是一起、同频更新。"""
    from . import account_intel
    content = sync_all_channels(ws)
    prof = account_intel.refresh_all(ws.id, use_llm=False)
    now = _now()
    db.set_setting(_LAST_SYNC_SETTING, now)
    return {"content": content, "profiles": prof, "synced_at": now}


def daily_channel_sync() -> dict:
    """每日自动版:遍历所有 workspace 做 sync_channel_data。scheduler 一天调一次。"""
    agg = {"content": {"fetched": 0, "updated": 0, "imported": 0, "channels": 0},
           "profiles": {"refreshed": 0, "at_risk_or_blocked": 0}}
    for ws in db.list_workspaces():
        r = sync_channel_data(ws)
        for k in agg["content"]:
            agg["content"][k] += r["content"].get(k, 0)
        for k in agg["profiles"]:
            agg["profiles"][k] += r["profiles"].get(k, 0)
    agg["synced_at"] = _now()
    db.set_setting(_LAST_SYNC_SETTING, agg["synced_at"])
    log.info("daily channel sync: %s", agg)
    return agg


def last_channel_sync_at() -> str:
    return db.get_setting(_LAST_SYNC_SETTING) or ""


def _rasterize_for_publish(path: str, post: Post) -> str:
    """平台只接受位图(png/jpg)。若 media 是 .svg 海报,就用 Pillow 渲染一张 JPG 顶替
    (从帖子 hook + 品牌色重绘),避免 FB/IG 静默丢图。位图原样返回。"""
    if not path or not str(path).lower().endswith(".svg"):
        return path
    try:
        from . import imaging
        headline = (post.content.hook or post.title or "").strip()
        palette = (post.meta or {}).get("palette") or []
        stem = f"raster_{uuid.uuid4().hex[:8]}"
        return imaging.render_jpg_poster(headline, "", palette, stem)
    except Exception:  # noqa: BLE001
        log.warning("svg rasterize failed for %s", path)
        return path


def _publish_targets(post: Post) -> Post:
    """执行发布(假定已置为 publishing / 已原子认领)。回填每个 target 结果。
    幂等:已成功(posted + external_id)的 target 跳过,绝不重复发送。每条完成即落库,
    缩小崩溃窗口。"""
    if not post.targets:
        post.status = PostStatus.failed
        post.meta["error"] = "没有选择任何渠道"
        return db.save_post(post)

    ok_all = True
    for tgt in post.targets:
        if tgt.status == "posted":
            continue                      # 已发成功,幂等跳过(部分失败重试不重复发)
        ch = db.get_channel(tgt.channel_id)
        if not ch or ch.status != ChannelStatus.connected:
            tgt.status = "failed"
            tgt.error = "渠道未连接"
            ok_all = False
            continue
        from . import platform_risk
        # 硬安全停:熔断态无条件拦截(即使软风控门关闭,如测试)。对被平台标记的账号
        # 继续调 API 只会加重自动化画像,唯一正确动作是停手 + 人工介入。
        if platform_risk.frozen_until(ch):
            tgt.status = "failed"
            tgt.error = ("风控熔断中(未发出):" + str((ch.config or {}).get("error", ""))[:90])
            ok_all = False
            continue
        # 软风控门(内容 lint/节奏/重复度):默认开,conftest 关。block 级直接拦下——
        # connector 都不碰,风控问题在出手前解决,不是靠平台报错事后学习。
        if platform_risk.gate_enabled():
            risk = platform_risk.pre_publish_issues(ch, post)
            blocks = [i for i in risk if i["level"] == "block"]
            if blocks:
                tgt.status = "failed"
                tgt.error = "风控拦截(未发出):" + ";".join(i["msg"] for i in blocks[:2])
                ok_all = False
                post.meta.setdefault("risk_issues", {})[ch.platform] = risk
                continue
            if risk:                                # warn 级放行但留痕(UI 可见)
                post.meta.setdefault("risk_issues", {})[ch.platform] = risk
        conn = get_connector(ch.platform)
        media = [m for m in (post.media or ([post.visual_svg] if post.visual_svg else [])) if m]
        media = [_rasterize_for_publish(m, post) for m in media]   # .svg → .jpg,否则平台丢图
        res = conn.publish(ch, post.content, media)
        if res.ok:
            tgt.status = "posted"
            tgt.permalink = res.permalink
            # 平台发成功却没回 id 时,补一个哨兵句柄:否则空 external_id 会让下次重试
            # 的"已发跳过"判定失效(现改为只看 status,双保险),导致重复投递。
            tgt.external_id = res.external_id or f"posted_{uuid.uuid4().hex[:8]}"
            tgt.sandbox = res.sandbox      # 模拟渠道标记,前端区分显示(非平台真帖)
            if (ch.config or {}).get("error"):          # 发布成功 → 渠道错误标记自愈
                ch.config = dict(ch.config)
                ch.config.pop("error", None)
                db.save_channel(ch)
        else:
            tgt.status = "failed"
            tgt.error = res.error
            ok_all = False
            if platform_risk.is_platform_block(res.error):
                # 平台风控封锁:熔断优先于鉴权分类("API access blocked" 两者都命中,
                # 但重连对风控封锁无效,提示重连只会诱导用户加重账号画像)。
                platform_risk.freeze_channel(ch, res.error)
            elif is_auth_error(res.error):
                # 鉴权类失败:标到渠道上(Connections 出红字+重连按钮)。
                # 不翻 status:connected 是重试/同步的闸,翻了会把用户锁死在「渠道未连接」。
                ch.config = dict(ch.config or {})
                ch.config["error"] = f"发布鉴权失败,请重新连接:{(res.error or '')[:120]}"
                db.save_channel(ch)
        post.status = PostStatus.publishing
        db.save_post(post)                # 每条落库:崩溃也不丢已发状态

    post.status = PostStatus.posted if ok_all else PostStatus.failed
    if ok_all:
        post.posted_at = _now()
    return db.save_post(post)


def delete_post(post: Post, force: bool = False) -> dict:
    """删除帖子:先删平台上已发布的真帖,全部成功(或平台结构不支持删除)才删本地记录。

    ⚠️ 只在**临时失败**(网络/权限,内容可能还在平台且可重试)时保留本地(标 delete_failed),
    避免丢对账句柄。**平台结构上不支持删除**(IG/YT 等 unsupported)不阻塞本地删除——否则帖会永久卡住。
    `force=True`:无视远端结果,强制只删本地记录(前端"仅删除本地"入口)。"""
    results = []
    blocked = False               # 有"可重试的真失败"才阻塞本地删除
    for tgt in post.targets:
        if tgt.status == "posted" and tgt.external_id:
            ch = db.get_channel(tgt.channel_id)
            if not ch:
                # 渠道已删,无法重试远端删除 → 不阻塞本地删除
                results.append({"platform": tgt.platform, "ok": False,
                                "unsupported": True, "error": "渠道不存在"})
                continue
            res = get_connector(ch.platform).delete(ch, tgt.external_id)
            results.append({"platform": tgt.platform, "ok": res.ok,
                            "unsupported": res.unsupported, "error": res.error})
            if res.ok:
                tgt.status = "deleted"      # 幂等:重试时跳过,多目标删除可收敛
                tgt.external_id = ""
            elif not res.unsupported:
                blocked = True              # 临时失败:内容可能还在,保留本地待重试
    if force or not blocked:
        db.delete_post(post.id)
        return {"ok": True, "deleted_local": True, "forced": force and blocked,
                "platform_deletes": results}
    post.meta = dict(post.meta or {})
    post.meta["delete_failed"] = True
    db.save_post(post)
    return {"ok": False, "deleted_local": False, "platform_deletes": results,
            "error": "平台删除失败(可能仅临时),已保留本地记录;可重试或强制仅删本地"}


def run_due_posts(workspace_id: str | None = None) -> list[Post]:
    """定时 tick:发布所有到期的定时帖。用原子认领防止并发重复发布。"""
    now = _now()
    due = db.due_scheduled_posts(workspace_id, now)
    published = []
    for post in due:
        if not db.claim_scheduled_post(post.id):
            continue  # 已被其他 tick/请求认领,跳过
        fresh = db.get_post(post.id) or post
        fresh.status = PostStatus.publishing
        published.append(_publish_targets(fresh))
    return published


def recover_stuck_publishing(older_than_sec: int = 300) -> int:
    """启动时回收崩溃/重启遗留在 'publishing' 的帖子。

    ⚠️ 关键:绝不把在途帖回退成 scheduled(会被调度器自动重发 → 重复发帖 / 一稿多发)。
    崩溃时某些 target 可能已发出但没回填 external_id,无法确定,故标为 failed + 待人工确认;
    用户手动重试时 `_publish_targets` 会跳过已 posted 的 target,不会重复发送。"""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_sec)).isoformat(timespec="seconds")
    n = 0
    for ws in db.list_workspaces():
        for p in db.list_posts(ws.id, status="publishing"):
            if (p.updated_at or "") < cutoff:
                p.status = PostStatus.failed
                p.meta = dict(p.meta or {})
                p.meta.setdefault("recovered", "发布中断,状态未知;请确认平台后再手动重试(不会重复已发渠道)")
                db.save_post(p)
                n += 1
    return n


# --------------------------------------------------------------------------- #
# 播种默认数据(贴近截图,便于演示 / 验收)
# --------------------------------------------------------------------------- #
def seed_defaults() -> Workspace:
    existing = db.list_workspaces()
    if existing:
        return existing[0]

    ws = db.create_workspace("My Workspace", brand_name="YominPost")

    # 预连接几个模拟渠道(便于演示;真实 OAuth 平台需授权后才连接)
    for platform, name in [("tiktok", "Demo Creator"), ("pinterest", "Demo Brand"),
                           ("threads", "Demo Creator")]:
        ch = Channel(id=db.new_id("ch"), workspace_id=ws.id, platform=platform,
                     display_name=name)
        ch = get_connector(platform).connect(ch, {"handle": name})
        ch.connected_at = _now()
        db.save_channel(ch)

    # 几个初始话题
    for i, (title, pillar) in enumerate([
        ("为什么大多数产品页输在细节而非设计", "误区澄清"),
        ("用一条 thread 讲清内容复用的 5 个动作", "方法论"),
        ("我们做了个内容自动化实验,结果出乎意料", "案例复盘"),
    ]):
        db.save_topic(Topic(
            id=db.new_id("topic"), workspace_id=ws.id, title=title, pillar=pillar,
            priority=0.6 + i * 0.1, source="manual", created_at=_now(),
        ))
    return ws


# --------------------------------------------------------------------------- #
# OAuth:开发者应用凭据 + 授权 / 回调全链路
# --------------------------------------------------------------------------- #
def set_app_creds(platform: str, client_id: str, client_secret: str) -> None:
    from .crypto import encrypt
    db.set_setting(f"app:{platform}:id", client_id)
    db.set_setting(f"app:{platform}:secret", encrypt(client_secret))


def get_app_creds(platform: str, instance: str = "") -> tuple[str, str]:
    """凭据优先级:oauthdemo 内置 > mastodon 按实例自助 > 服务器 env > 本地 settings。"""
    from .config import env_app_creds
    from .crypto import decrypt
    if platform == "oauthdemo":
        return ("yominpost-demo", "demo-secret")
    if platform == "mastodon":
        prefix = f"app:mastodon:{instance}:"
        return (db.get_setting(prefix + "id") or "",
                decrypt(db.get_setting(prefix + "secret") or ""))
    # 1) 服务器环境变量(运营者配一次 → 全员一键)
    cid, csec = env_app_creds(platform)
    if cid:
        return cid, csec
    # 2) 本地 settings(可选:通过 Settings 页填一次)
    cid = db.get_setting(f"app:{platform}:id") or ""
    csec = decrypt(db.get_setting(f"app:{platform}:secret") or "")
    return cid, csec


def app_configured(platform: str) -> bool:
    from .models import PLATFORM_BY_ID
    meta = PLATFORM_BY_ID.get(platform)
    if not meta or meta.auth != "oauth":
        return False
    if not meta.needs_app:      # oauthdemo / mastodon 自助
        return True
    cid, _ = get_app_creds(platform)
    return bool(cid)


def start_oauth(ws: Workspace, platform: str, base_url: str, instance: str = "") -> str:
    from .oauth_flow import build_authorize_url, gen_pkce, gen_state
    from .oauth_specs import OAUTH_SPECS, mastodon_register_app, resolve_urls

    spec = OAUTH_SPECS.get(platform)
    if not spec:
        raise ValueError(f"{platform} 不是 OAuth 平台")
    redirect_uri = f"{base_url}/oauth/callback"

    # 取/建应用凭据
    if platform == "mastodon" and not get_app_creds(platform, instance)[0]:
        creds = mastodon_register_app(instance or "https://mastodon.social", redirect_uri)
        db.set_setting(f"app:mastodon:{instance}:id", creds["client_id"])
        from .crypto import encrypt
        db.set_setting(f"app:mastodon:{instance}:secret", encrypt(creds["client_secret"]))
    client_id, _ = get_app_creds(platform, instance)
    if not client_id:
        raise ValueError(f"{platform} 需要先配置开发者应用凭据(client_id/secret)")

    state = gen_state()
    verifier, challenge = gen_pkce() if spec.get("pkce") else ("", "")
    authorize_url, _ = resolve_urls(platform, instance=instance, base=base_url)
    url = build_authorize_url(
        authorize_url, client_id=client_id, redirect_uri=redirect_uri,
        scope=spec["scope"], state=state,
        code_challenge=challenge if spec.get("pkce") else None,
        extra=spec.get("extra_auth"),
    )
    db.save_oauth_state(state, ws.id, platform, verifier, instance, redirect_uri)
    return url


def complete_oauth(state: str, code: str, base_url: str) -> Channel:
    from .crypto import encrypt
    from .oauth_flow import exchange_code
    from .oauth_specs import OAUTH_SPECS, fetch_identity, resolve_urls

    st = db.pop_oauth_state(state)
    if not st:
        raise ValueError("无效或过期的 state")
    platform, instance = st["platform"], st["instance"] or ""
    spec = OAUTH_SPECS[platform]
    client_id, client_secret = get_app_creds(platform, instance)
    _, token_url = resolve_urls(platform, instance=instance, base=base_url)

    if spec.get("ig_login"):
        # Instagram 直连:专用 multipart 换短效 + 长效令牌(见 oauth_specs.instagram_exchange)
        from .oauth_specs import instagram_exchange
        igt = instagram_exchange(code, client_id, client_secret, st["redirect_uri"])
        access = igt["access_token"]
        refresh = ""
        tok = {"access_token": access, "user_id": igt["user_id"]}
    else:
        tok = exchange_code(
            token_url, client_id=client_id, client_secret=client_secret, code=code,
            redirect_uri=st["redirect_uri"], code_verifier=st["verifier"] or None,
            basic_auth=bool(spec.get("basic")),
        )
        access = tok.get("access_token", "")
        refresh = tok.get("refresh_token", "")
        if platform == "facebook":  # 换 ~60 天长效用户令牌 → 主页令牌基本不过期
            from .oauth_specs import exchange_long_lived
            access = exchange_long_lived("facebook", access, client_id, client_secret)

    cfg = {"instance": instance, "base": base_url}
    ident = fetch_identity(platform, access, cfg, tok)
    # IG 直连:token 交换已带 user_id,身份接口取不到时兜底
    if spec.get("ig_login") and not ident.get("ig_user_id") and tok.get("user_id"):
        ident["ig_user_id"] = tok["user_id"]

    config = {
        "access_token_enc": encrypt(access),
        "refresh_token_enc": encrypt(refresh),
        "instance": instance, "base": base_url,
        "handle": ident.get("handle", ""),
    }
    # token 过期时刻:让连接器在发布前/收到 401 时按需刷新(见 oauth_conn._ensure_fresh_token)
    expires_in = 0
    try:
        expires_in = int(float(tok.get("expires_in", 0) or 0))
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        from datetime import timedelta
        config["expires_at"] = (datetime.now(timezone.utc)
                                + timedelta(seconds=expires_in)).isoformat(timespec="seconds")
    # 合并各平台额外配置;敏感字段(页令牌/webhook)加密存储
    _SENSITIVE = {"page_token", "webhook_url"}
    for k, v in ident.items():
        if k in ("handle", "_warn") or not v:
            continue
        config[f"{k}_enc" if k in _SENSITIVE else k] = encrypt(v) if k in _SENSITIVE else v

    # 连接可能"授权成功但不可发":如 Facebook 没有可管理的主页 → fetch_identity 会给 _warn。
    # 这种情况标 error + 存原因,而不是谎称 connected(否则发布时才神秘失败)。
    warn = ident.get("_warn", "")
    status = ChannelStatus.error if warn else ChannelStatus.connected
    if warn:
        config["error"] = warn

    # 重新授权(令牌过期后的常规恢复路径)必须复用同一渠道行,否则:
    #   ① Connections 页堆叠重复渠道;② 旧行仍标 connected 但令牌已死;
    #   ③ 既有定时/草稿帖的 target.channel_id 仍指向旧行 → 发布用死令牌失败。
    # 按 workspace+platform 匹配现有渠道(优先同 handle / ig_user_id / page_id),就地更新。
    handle = ident.get("handle", "")
    existing = _find_reauth_channel(st["workspace_id"], platform, ident)
    if existing:
        existing.handle = handle or existing.handle
        existing.display_name = handle or existing.display_name or platform
        existing.status = status
        existing.connected_at = _now()
        existing.config = config
        db.save_channel(existing)
        _profile_on_connect(existing)
        return existing

    ch = Channel(
        id=db.new_id("ch"), workspace_id=st["workspace_id"], platform=platform,
        display_name=handle or platform, handle=handle,
        status=status, connected_at=_now(), config=config,
    )
    db.save_channel(ch)
    _profile_on_connect(ch)
    return ch


def _profile_on_connect(channel: Channel) -> None:
    """连接成功即建立账号健康基线(best-effort,失败不挡连接)。
    这正是此次 IG 事故缺的:连上的第一刻就该知道"新号需 warm-up",而非发了才知。"""
    try:
        from . import account_intel
        account_intel.refresh_profile(channel, use_llm=False)
    except Exception:  # noqa: BLE001
        log.info("connect-time profile skipped for %s", channel.id, exc_info=True)


def _find_reauth_channel(ws_id: str, platform: str, ident: dict) -> "Channel | None":
    """为重新授权寻找可复用的现有渠道:同 workspace+platform,且账号身份一致。
    身份优先级:ig_user_id > page_id > handle;都取不到时,退化为该 platform 的唯一渠道。"""
    same = [c for c in db.list_channels(ws_id) if c.platform == platform]
    if not same:
        return None
    for key in ("ig_user_id", "page_id"):
        val = ident.get(key)
        if val:
            hit = [c for c in same if str((c.config or {}).get(key, "")) == str(val)]
            if hit:
                return hit[0]
    handle = ident.get("handle", "")
    if handle:
        hit = [c for c in same if (c.handle or "") == handle]
        if hit:
            return hit[0]
    # 无可辨身份:仅当该平台只有一个渠道时才就地更新(避免误并两个不同账号)
    return same[0] if len(same) == 1 else None


# --------------------------------------------------------------------------- #
# 渠道健康守护:主动续期 + 探针 + 失败标记(全局通知由 /api/notifications 聚合)
# --------------------------------------------------------------------------- #
HEALTH_PROBE_TIMEOUT = 20     # 每渠道探针预算(秒),防单平台拖死整轮


def check_channel_health() -> dict:
    """对所有 connected 的真实 OAuth 渠道:①到期前主动续期(已有机制);②轻量身份
    探针(fetch_identity)验证令牌真的可用;③鉴权类失败 → 标记 config.error(即
    「自动重连失败,需人工」),瞬时错误不误标;④成功 → 自愈清除 + 记录检查时间。
    绝不翻 channel.status(connected 是发布/重试的闸)。"""
    from .connectors.oauth_conn import _decrypt_cfg, _ensure_fresh_token, is_auth_error
    from .models import PLATFORM_BY_ID
    from .oauth_specs import fetch_identity

    from . import platform_risk
    checked = refreshed = marked = healed = 0
    for ws in db.list_workspaces():
        for ch in db.list_channels(ws.id):
            meta = PLATFORM_BY_ID.get(ch.platform)
            if (not meta or not meta.real or meta.auth != "oauth"
                    or ch.status != ChannelStatus.connected):
                continue
            if platform_risk.frozen_until(ch):
                continue          # 熔断期内不探针:对已标记账号任何 API 调用都在加重画像
            checked += 1
            try:
                old_tok = (ch.config or {}).get("access_token_enc", "")
                token = _ensure_fresh_token(ch)          # 到期前自动续期(=自动重连)
                if (ch.config or {}).get("access_token_enc", "") != old_tok:
                    refreshed += 1
                if not token:
                    raise RuntimeError("令牌缺失或解密失败(未授权)")
                _tok, cfg_plain = _decrypt_cfg(ch)
                fetch_identity(ch.platform, token, cfg_plain)   # 轻量身份探针
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:160]
                if platform_risk.is_platform_block(err):
                    platform_risk.freeze_channel(ch, err)      # 风控封锁:熔断,勿再探
                    marked += 1
                    log.warning("channel health BLOCKED %s/%s: %s", ch.platform, ch.id, err)
                elif is_auth_error(err):
                    ch.config = dict(ch.config or {})
                    ch.config["error"] = f"健康检查失败(自动重连无效,需人工重连):{err[:110]}"
                    db.save_channel(ch)
                    marked += 1
                    log.warning("channel health FAIL %s/%s: %s", ch.platform, ch.id, err)
                else:
                    log.info("channel health transient %s/%s: %s", ch.platform, ch.id, err)
                continue
            # 探针通过 → 自愈 + 记录
            ch.config = dict(ch.config or {})
            if ch.config.pop("error", None):
                healed += 1
            ch.config["health_checked_at"] = _now()
            db.save_channel(ch)
    out = {"checked": checked, "refreshed": refreshed, "marked": marked, "healed": healed}
    log.info("channel health: %s", out)
    return out


def channel_notifications() -> list[dict]:
    """需人工介入的渠道清单(全局通知数据源)。按 kind 分流:
    - `health` = 平台封控/限制(如 IG「API access blocked」)—— **不是连接问题**,重连无效,
      归「账号管理」处理;
    - `connection` = 鉴权失效(令牌过期,重连可解)—— 归「Connections」处理。
    这条分流对应用户的 IA 诉求:连接卡只管链接,账号健康归账号管理。"""
    from . import platform_risk
    out = []
    for ws in db.list_workspaces():
        for ch in db.list_channels(ws.id):
            err = (ch.config or {}).get("error")
            if not err:
                continue
            blocked = (platform_risk.is_platform_block(str(err))
                       or platform_risk.frozen_until(ch) is not None)
            out.append({"channel_id": ch.id, "workspace_id": ws.id,
                        "platform": ch.platform,
                        "display_name": ch.display_name or ch.platform,
                        "error": str(err)[:200],
                        "kind": "health" if blocked else "connection"})
    return out
