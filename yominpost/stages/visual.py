"""⑥ 图文 / 视觉。

输入:BrandDNA + TopicIdea + CopyDraft。
输出:VisualAsset(图像 prompt / alt / 版式 / 配色),并真实渲染一张
      占位海报 SVG 到 artifacts 目录(可直接在浏览器打开)。
"""
from __future__ import annotations

import html
from pathlib import Path

from ..config import ARTIFACT_DIR
from ..providers.base import Provider
from ..schemas import BrandDNA, CopyDraft, TopicIdea, VisualAsset


def _wrap(text: str, per_line: int) -> list[str]:
    text = text.strip()
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= per_line and ch in " ，。,、!！?？;；":
            lines.append(cur.strip())
            cur = ""
        elif len(cur) >= per_line + 4:
            lines.append(cur.strip())
            cur = ""
    if cur.strip():
        lines.append(cur.strip())
    return lines[:6] or [text[:per_line]]


def _render_svg(headline: str, brand_name: str, palette: list[str], stem: str) -> str:
    bg, accent, fg, accent2 = (palette + ["#0F172A", "#38BDF8", "#F8FAFC", "#FACC15"])[:4]
    lines = _wrap(headline, 11)
    line_h = 74
    start_y = 300 - (len(lines) - 1) * line_h // 2
    tspans = "".join(
        f'<tspan x="90" y="{start_y + i * line_h}">{html.escape(ln)}</tspan>'
        for i, ln in enumerate(lines)
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="864" height="1080" viewBox="0 0 864 1080">
  <rect width="864" height="1080" fill="{bg}"/>
  <circle cx="760" cy="140" r="150" fill="{accent}" opacity="0.18"/>
  <rect x="-40" y="820" width="420" height="420" fill="{accent2}" opacity="0.12" transform="rotate(18 170 1030)"/>
  <rect x="90" y="120" width="70" height="10" rx="5" fill="{accent}"/>
  <text font-family="'PingFang SC','Helvetica Neue',Arial,sans-serif" font-size="60"
        font-weight="800" fill="{fg}" letter-spacing="1">{tspans}</text>
  <rect x="90" y="1000" width="120" height="10" rx="5" fill="{accent}"/>
</svg>"""
    path = Path(ARTIFACT_DIR) / f"{stem}.svg"
    path.write_text(svg, encoding="utf-8")
    return str(path)


def render(
    brand: BrandDNA,
    topic: TopicIdea,
    copy: CopyDraft,
    provider: Provider,
    *,
    stem: str | None = None,
    platform: str = "",
    key_points: list[str] | None = None,
    language: str = "",
    style: str = "",
) -> VisualAsset:
    ctx = {
        "brand": brand.model_dump(),
        "topic": topic.model_dump(),
        "copy": copy.model_dump(),
    }
    data = provider.run("visual", ctx)
    palette = data.get("palette") or brand.color_palette
    headline = copy.hook or topic.title
    stem = stem or f"{topic.id}"

    # 优先用当前启用的生图驱动(active_image_driver 按 openai_image>codex_image>svg 择优):
    #   openai_image = 真·GPT 生图(gpt-image, 需 OPENAI_API_KEY)
    #   codex_image  = codex 现写 Pillow 代码渲染定制海报(无水印)
    from .. import capabilities as caps_mod
    driver = caps_mod.active_image_driver()
    plat = platform or topic.suggested_platform.value
    img_path = None
    if driver == "openai_image":
        from .. import imaging_openai
        img_path = imaging_openai.render(
            subject=topic.title, headline=headline, key_points=key_points or [],
            palette=palette, platform=plat, style=style or data.get("prompt", ""),
            brand_name=brand.brand_name, stem=stem)
    if img_path is None and driver in ("openai_image", "codex_image"):
        from .. import imaging_codex
        img_path = imaging_codex.render(
            headline=headline, platform=plat, palette=palette,
            key_points=key_points or [], brand_name=brand.brand_name,
            style=style or data.get("prompt", ""), language=language, stem=stem)
    # 最终回退:内置 SVG 海报(已去水印)
    path = img_path or _render_svg(headline, brand.brand_name, palette, stem)
    return VisualAsset(
        topic_id=topic.id,
        prompt=data.get("prompt", ""),
        alt_text=data.get("alt_text", headline),
        layout=data.get("layout", "quote_card"),
        palette=palette,
        svg_path=path,
    )
