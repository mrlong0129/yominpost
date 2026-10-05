"""能力池 —— workspace 级的生成驱动配置。

「能力」= 背后可调用的生成驱动:Claude Code / Codex / agy CLI(文本),
SVG 海报 / 即梦生图(图像),分镜字幕 / 即梦视频(视频)等。
- 每个能力有 kind(text|image|video)、driver、启用开关(存 settings)、
  运行时可用性探测(CLI 是否在 PATH / API key 是否配置)。
- 规划 Loop 只会从「已启用且可用」的能力中选择。
"""
from __future__ import annotations

import json
import os
import shutil

from . import db

# 默认能力目录(id 唯一;enabled 可被 settings 覆盖)
DEFAULT_CAPABILITIES: list[dict] = [
    {"id": "template",     "kind": "text",  "label": "模板引擎(内置)",   "driver": "template"},
    {"id": "claude_code",  "kind": "text",  "label": "Claude Code",      "driver": "claude_code"},
    {"id": "codex",        "kind": "text",  "label": "Codex",            "driver": "codex"},
    {"id": "agy",          "kind": "text",  "label": "agy CLI",          "driver": "agy"},
    {"id": "openai_image", "kind": "image", "label": "GPT 生图(gpt-image)", "driver": "openai_image"},
    {"id": "codex_poster", "kind": "image", "label": "Codex 海报(Pillow)", "driver": "codex_image"},
    {"id": "svg_poster",   "kind": "image", "label": "SVG 海报(内置)",   "driver": "svg"},
    {"id": "jimeng_image", "kind": "image", "label": "即梦 · 生图",      "driver": "jimeng"},
    {"id": "storyboard",   "kind": "video", "label": "分镜+字幕(内置)",  "driver": "storyboard"},
    {"id": "jimeng_video", "kind": "video", "label": "即梦 · 视频",      "driver": "jimeng"},
]


def probe(cap: dict) -> tuple[bool, str]:
    """探测能力可用性,返回 (available, note)。"""
    d = cap["driver"]
    if d in ("template", "svg", "storyboard"):
        return True, "内置,始终可用"
    if d == "claude_code":
        return shutil.which("claude") is not None, "需本机 claude CLI"
    if d == "codex":
        return shutil.which("codex") is not None, "需本机 codex CLI"
    if d == "agy":
        return shutil.which("agy") is not None, "需本机 agy CLI"
    if d == "openai_image":
        from . import imaging_openai
        return imaging_openai.available(), "真·GPT 生图(gpt-image),需 OPENAI_API_KEY"
    if d == "codex_image":
        return shutil.which("codex") is not None, "codex 写 Pillow 代码生成定制海报(无水印)"
    if d == "jimeng":
        ok = bool(os.environ.get("JIMENG_API_KEY", "").strip() or db.get_secret_setting("jimeng_api_key"))
        return ok, "需配置 JIMENG_API_KEY(未实现,当前回退内置渲染)"
    return False, "未知驱动"


def _enabled_map() -> dict:
    raw = db.get_setting("capabilities")
    try:
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def pool() -> list[dict]:
    """返回完整能力池(含启用状态与可用性)。"""
    enabled = _enabled_map()
    out = []
    for cap in DEFAULT_CAPABILITIES:
        available, note = probe(cap)
        out.append({**cap,
                    "enabled": bool(enabled.get(cap["id"], True)),
                    "available": available, "note": note})
    return out


def set_enabled(cap_id: str, on: bool) -> None:
    m = _enabled_map()
    m[cap_id] = bool(on)
    db.set_setting("capabilities", json.dumps(m))


def usable_kinds() -> set[str]:
    """当前可真正使用的能力种类集合(启用 ∧ 可用)。"""
    return {c["kind"] for c in pool() if c["enabled"] and c["available"]}


def active_image_driver() -> str:
    """当前用于生图的驱动(启用 ∧ 可用中,按 pool 顺序优先:codex_image > jimeng > svg)。
    无可用图像能力则返回空串。YOMINPOST_DISABLE_CODEX_IMAGE=1 时跳过 codex(测试用)。"""
    skip_codex = os.environ.get("YOMINPOST_DISABLE_CODEX_IMAGE", "") == "1"
    for c in pool():
        if c["kind"] == "image" and c["enabled"] and c["available"]:
            if c["driver"] == "codex_image" and skip_codex:
                continue
            return c["driver"]
    return ""
