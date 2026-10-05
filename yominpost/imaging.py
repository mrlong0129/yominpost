"""海报 JPG 渲染(Pillow)—— Instagram 直发需要一张公开 JPG。

渲染到 artifacts 目录,并按 YOMINPOST_PUBLIC_URL 生成公网 URL(Meta 服务器会来抓)。
"""
from __future__ import annotations

import os

from .config import ARTIFACT_DIR

_CJK_FONTS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]


def _font(size: int):
    from PIL import ImageFont
    for p in _CJK_FONTS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _wrap_cjk(text: str, per_line: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in text.strip():
        if ch == "\n":
            if cur:
                lines.append(cur)
            cur = ""
            continue
        cur += ch
        if len(cur) >= per_line:
            lines.append(cur)
            cur = ""
    if cur:
        lines.append(cur)
    return lines[:8] or [text[:per_line]]


def render_jpg_poster(headline: str, brand_name: str, palette: list[str], stem: str) -> str:
    """渲染 1080x1080 JPG 海报,返回文件路径。"""
    from PIL import Image, ImageDraw

    W = H = 1080
    bg, accent, fg, accent2 = (list(palette) + ["#0F172A", "#38BDF8", "#F8FAFC", "#FACC15"])[:4]
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # 顶部装饰条 + 右上角块
    d.rectangle([90, 120, 230, 132], fill=accent)
    d.rectangle([W - 220, 0, W, 160], fill=accent2)

    headline = (headline or brand_name or "YominPost").strip()
    lines = _wrap_cjk(headline, 12)
    font = _font(76)
    line_h = 100
    total = len(lines) * line_h
    y = (H - total) // 2 - 30
    for ln in lines:
        d.text((90, y), ln, font=font, fill=fg)
        y += line_h

    # 无水印:底部只留一条品牌色装饰条,不写任何品牌名/文字
    d.rectangle([90, H - 96, 210, H - 84], fill=accent)

    path = os.path.join(str(ARTIFACT_DIR), f"{stem}.jpg")
    img.save(path, "JPEG", quality=90)
    return path


def to_jpg(src_path: str) -> str:
    """把本地图片(png/webp/…)转成 JPG(Instagram 只收 JPEG)。返回 JPG 路径;失败返回原路径。"""
    import os as _os
    if not src_path or not _os.path.exists(src_path):
        return src_path
    if src_path.lower().endswith((".jpg", ".jpeg")):
        return src_path
    try:
        from PIL import Image
        stem = _os.path.splitext(_os.path.basename(src_path))[0]
        dest = _os.path.join(str(ARTIFACT_DIR), f"{stem}_ig.jpg")
        with Image.open(src_path) as im:
            im.convert("RGB").save(dest, "JPEG", quality=92)
        return dest
    except Exception:  # noqa: BLE001
        return src_path


def public_artifact_url(path: str) -> str:
    """把产物文件路径转成公网 URL(需设 YOMINPOST_PUBLIC_URL)。"""
    base = os.environ.get("YOMINPOST_PUBLIC_URL", "").strip().rstrip("/")
    if not base or not path:
        return ""
    return f"{base}/artifacts/{os.path.basename(path)}"
