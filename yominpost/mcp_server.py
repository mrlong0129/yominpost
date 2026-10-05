"""MCP server (stdio) exposing YominPost to Claude Code, Cursor and other agents.

Run:  yominpost-mcp      (or: yominpost mcp, or: python -m yominpost.mcp_server)

Tools: generate_posts, check_post, list_platforms, save_draft, list_posts.
Deliberately NO publish tool: publishing is irreversible and goes out under a real account,
so drafts saved here are reviewed and published by a human in the web UI (`yominpost serve`).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

try:  # official MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - SDK v1.x
    from mcp.server.fastmcp import FastMCP as _Server


mcp = _Server(
    "yominpost",
    instructions=(
        "YominPost is a self-hosted social media studio. Use generate_posts to turn brand material into "
        "platform-specific post packages (hook, body, CTA, hashtags, format decision, QA score, optional "
        "SVG poster and SRT storyboard). Use check_post before suggesting any post for publishing: it flags "
        "prompt leaks, engagement bait, hashtag stuffing and length problems that get accounts restricted. "
        "save_draft stores a draft in the local YominPost workspace; tell the user to review and publish it "
        "in the web UI (http://127.0.0.1:8300). Never claim a post was published: this server cannot publish."
    ),
)

_PLATFORMS = ["x", "linkedin", "instagram", "tiktok", "youtube", "xiaohongshu", "wechat"]


def _workspace():
    from . import db, services

    db.init_db()
    wss = db.list_workspaces()
    return wss[0] if wss else services.seed_defaults()


def generate_posts_impl(brand: str, material: str = "", platforms: Optional[list[str]] = None,
                        num_topics: int = 3, trends: Optional[list[str]] = None,
                        language: str = "auto") -> dict[str, Any]:
    from .orchestrator import run as run_pipeline
    from .schemas import BrandSource, PipelineRequest, Platform

    plats = [p.strip().lower() for p in (platforms or ["x"]) if p and p.strip()]
    bad = [p for p in plats if p not in _PLATFORMS]
    if bad:
        return {"ok": False, "error": f"unknown platform(s): {', '.join(bad)}", "platforms": _PLATFORMS}
    req = PipelineRequest(
        brand_name=brand or "YominPost",
        sources=[BrandSource(kind="note", text=material)] if material else [],
        raw_trends=list(trends or []),
        platforms=[Platform(p) for p in plats],
        num_topics=max(1, min(int(num_topics or 1), 10)),
        language=language,
    )
    res = run_pipeline(req, persist=False)
    packages = []
    for pk in res.packages:
        c, q, d = pk.copy, pk.qa, pk.format_decision
        packages.append({
            "topic": pk.topic.title,
            "platform": d.platform.value,
            "format": d.format.value,
            "format_reason": list(getattr(d, "rule_trace", []) or []),
            "hook": c.hook if c else "",
            "body": c.body if c else "",
            "cta": c.cta if c else "",
            "hashtags": list(c.hashtags) if c else [],
            "segments": list(c.segments) if c else [],
            "qa": {"overall": q.overall, "passed": q.passed, "revised": q.revised,
                   "issues": [i.model_dump() for i in q.issues]} if q else None,
            "poster_svg": pk.visual.svg_path if pk.visual else "",
            "storyboard_srt": pk.video.srt_path if pk.video else "",
        })
    return {"ok": True, "run_id": res.run_id, "driver": res.provider,
            "brand_voice": res.brand.model_dump() if res.brand else None, "packages": packages}


def check_post_impl(text: str, platform: str = "x", hashtags: Optional[list[str]] = None) -> dict[str, Any]:
    from .models import Post, PostContent
    from .platform_risk import lint_content
    from .schemas import PLATFORM_LIMITS, Platform

    plat = (platform or "x").strip().lower()
    post = Post(id="mcp_check", workspace_id="mcp", content=PostContent(body=text or "", hashtags=list(hashtags or [])))
    issues = lint_content(plat, post)
    try:
        limit = PLATFORM_LIMITS[Platform(plat)]
    except (ValueError, KeyError):
        limit = None
    n = len(text or "")
    if limit and n > limit:
        issues.append({"level": "block", "code": "too_long", "msg": f"{n} chars > {plat} limit {limit}"})
    blocking = [i for i in issues if i.get("level") == "block"]
    return {"ok": not blocking, "platform": plat, "chars": n, "limit": limit, "issues": issues}


def save_draft_impl(text: str, title: str = "", hook: str = "", cta: str = "",
                    hashtags: Optional[list[str]] = None, platforms: Optional[list[str]] = None) -> dict[str, Any]:
    from . import db, services
    from .models import PostContent, PostCreate

    ws = _workspace()
    want = {p.strip().lower() for p in (platforms or []) if p and p.strip()}
    channels = [c for c in db.list_channels(ws.id) if getattr(c.status, "value", c.status) == "connected"]
    ids = [c.id for c in channels if not want or c.platform in want]
    post = services.create_post(ws, PostCreate(
        title=title, content=PostContent(hook=hook, body=text or "", cta=cta, hashtags=list(hashtags or [])),
        channel_ids=ids,
    ))
    missing = sorted(want - {c.platform for c in channels})
    return {"ok": True, "post_id": post.id, "status": post.status.value, "workspace": ws.name,
            "targets": [t.platform for t in post.targets], "unconnected_platforms": missing,
            "review_url": "http://127.0.0.1:8300", "note": "Draft only. Review and publish it in the YominPost web UI."}


def list_posts_impl(status: str = "", limit: int = 20) -> dict[str, Any]:
    from . import db

    ws = _workspace()
    posts = db.list_posts(ws.id, status or None)[: max(1, min(int(limit or 20), 100))]
    return {"workspace": ws.name, "posts": [{
        "id": p.id, "title": p.title, "status": p.status.value, "scheduled_at": p.scheduled_at,
        "posted_at": p.posted_at, "platforms": [t.platform for t in p.targets],
        "text": p.content.as_text()[:500]} for p in posts]}


def list_platforms_impl() -> dict[str, Any]:
    from .models import PLATFORM_CATALOG

    return {"generation_targets": _PLATFORMS, "channels": [{
        "id": p.id, "label": p.label, "real_publishing": p.real, "auth": p.auth,
        "max_chars": p.max_chars} for p in PLATFORM_CATALOG if p.id not in ("oauthdemo", "demopro")]}


@mcp.tool()
def generate_posts(brand: str, material: str = "", platforms: Optional[list[str]] = None,
                   num_topics: int = 3, trends: Optional[list[str]] = None,
                   language: str = "auto") -> dict[str, Any]:
    """Generate platform-specific social posts from brand material.

    Runs YominPost's 8-slot pipeline (brand DNA → trends → ideation → format router → copy →
    visual → short-video storyboard → QA self-critique). Works offline with no API key; uses a
    real model if the server was configured with one (YOMINPOST_PROVIDER / ANTHROPIC_API_KEY).

    Args:
        brand: brand or product name.
        material: what the brand does / the announcement / notes to write from.
        platforms: any of x, linkedin, instagram, tiktok, youtube, xiaohongshu, wechat. Default ["x"].
        num_topics: number of post ideas (1-10).
        trends: optional trend signals to weave in.
        language: auto (follow the material), en or zh.
    """
    return generate_posts_impl(brand, material, platforms, num_topics, trends, language)


@mcp.tool()
def check_post(text: str, platform: str = "x", hashtags: Optional[list[str]] = None) -> dict[str, Any]:
    """Pre-publish risk check for one post: prompt leaks, engagement bait, hashtag stuffing,
    link stacking and platform length limits. ok=false means do not publish as-is."""
    return check_post_impl(text, platform, hashtags)


@mcp.tool()
def list_platforms() -> dict[str, Any]:
    """List generation targets and publishing channels (which ones publish for real and how they authenticate)."""
    return list_platforms_impl()


@mcp.tool()
def save_draft(text: str, title: str = "", hook: str = "", cta: str = "",
               hashtags: Optional[list[str]] = None, platforms: Optional[list[str]] = None) -> dict[str, Any]:
    """Save a post as a DRAFT in the local YominPost workspace, targeting the connected channels
    for the given platforms. It is not published; the user reviews and publishes it in the web UI."""
    return save_draft_impl(text, title, hook, cta, hashtags, platforms)


@mcp.tool()
def list_posts(status: str = "", limit: int = 20) -> dict[str, Any]:
    """List posts in the local workspace. status: draft | scheduled | posted | failed (empty = all)."""
    return list_posts_impl(status, limit)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
