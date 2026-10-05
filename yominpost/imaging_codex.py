"""codex 生图:让 codex CLI 现写 Pillow 代码,基于主题/渠道/内容渲染定制营销海报。

现实边界(已实测):codex 不做照片级 AI 生图(gpt-image-1 那种,需 OpenAI Images API
+ key);它作为编码 agent 能写绘图代码。所以这里让 codex 按「设计简报」现写并运行
一段 Pillow 脚本,产出一张**无水印、随内容/渠道变化**的海报 PNG —— 比写死的模板海报
强得多,且不花任何 API 费用(复用已登录的 codex)。

流程:build_brief() 拼简报 → codex exec -s workspace-write 写脚本存 PNG →
校验是真图 → 失败回退到调用方的模板海报。
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile

from .config import ARTIFACT_DIR

log = logging.getLogger("yominpost.imaging_codex")

# 各渠道画布尺寸(宽,高)
PLATFORM_SIZE = {
    "instagram": (1080, 1350),   # 4:5 竖版信息流
    "facebook": (1200, 630),     # 横版链接卡
    "xiaohongshu": (1080, 1350),
    "tiktok": (1080, 1920),      # 9:16
    "youtube": (1280, 720),
    "linkedin": (1200, 627),
    "x": (1200, 675),
}
DEFAULT_SIZE = (1080, 1350)

# 供 codex 脚本使用的可用中文/西文字体(避免豆腐块)
_FONTS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _timeout() -> int:
    try:
        return max(60, int(os.environ.get("YOMINPOST_IMG_TIMEOUT", "180")))
    except ValueError:
        return 180


def _strip_emoji(s: str) -> str:
    """去掉 emoji / 象形符号 —— PIL 字体没有彩色 emoji 字形,会渲染成豆腐块。"""
    import re
    if not s:
        return ""
    out = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
        "\U00002190-\U000021FF\U00002B00-\U00002BFF️‍]", "", s)
    return re.sub(r"\s{2,}", " ", out).strip()


def build_brief(*, headline: str, platform: str, palette: list[str],
                key_points: list[str], brand_name: str, style: str,
                language: str, out_path: str) -> str:
    """把内容/渠道/风格拼成给 codex 的设计简报(codex 据此现写 Pillow 脚本)。"""
    w, h = PLATFORM_SIZE.get(platform, DEFAULT_SIZE)
    pal = [c for c in (palette or []) if isinstance(c, str) and c.startswith("#")][:5]
    pal = pal or ["#0F172A", "#38BDF8", "#F8FAFC", "#FACC15"]
    fonts = [p for p in _FONTS if os.path.exists(p)]
    headline = _strip_emoji(headline)
    key_points = [_strip_emoji(k) for k in (key_points or []) if _strip_emoji(k)]
    kp = "\n".join(f"    - {k}" for k in key_points[:3])
    lang_note = ("Text is English." if str(language).lower().startswith("en")
                 else "文案是中文,务必用支持中文的字体渲染(下面给了字体路径),不能出现豆腐块/方框。")
    return f"""You are a senior graphic designer. Write and RUN a single Python script (Pillow/PIL)
that renders ONE marketing poster image and saves it to EXACTLY this absolute path:
  {out_path}

Hard requirements (must all hold):
- Canvas size EXACTLY {w}x{h} px, RGB, saved as PNG (or JPEG if path ends .jpg).
- NO watermark, NO signature, NO "yominpost"/tool credit, NO placeholder lorem text.
- Do NOT fetch anything from the network. Pure local rendering only.
- Use ONLY these installed fonts (pick a good one; pass a real .ttf/.ttc path to ImageFont.truetype):
{chr(10).join('    ' + f for f in fonts) or '    (system default)'}
- {lang_note}
- ABSOLUTELY NO text overflow, clipping, or overlap. Every text block must fit inside its
  container with padding. MEASURE text with draw.textbbox / font.getbbox and, if it does not
  fit, shrink the font size or wrap to more lines until it fits. Do not let two elements overlap.
- Do NOT use emoji or pictographic characters (fonts lack the glyphs -> tofu boxes). Text only.
- Keep a safe margin (>=48px) from all edges. Prefer fewer, well-placed elements over clutter.
- Legible, high-contrast, modern editorial look. Real design, not a plain color block.

Design brief (interpret for the channel "{platform}"):
- Headline (render it large and prominent, wrap as needed):
    {headline!r}
- Do NOT render the brand name, any wordmark, logo, or credit text anywhere on the image.
- Supporting key points (optional, use if they help the composition):
{kp or '    (none)'}
- Brand color palette (use these): {pal}
- Style direction: {style or 'clean, bold, conversion-focused social poster'}

Design freedom: choose layout, gradients, geometric shapes, type hierarchy and accents
that best fit the channel and the message. Make it look like a real brand's ad creative.

After running the script, verify the file exists at the path and print its size. Output nothing else."""


def render(*, headline: str, platform: str, palette: list[str], key_points: list[str],
           brand_name: str, style: str, language: str, stem: str,
           ext: str = "png") -> str | None:
    """让 codex 生成海报,成功返回文件路径,失败返回 None(调用方回退模板)。

    codex 在**隔离临时目录**里跑(cwd + workspace-write 沙盒都锁在 temp),它写入的
    脚本、临时文件与产图都留在 temp;仅当校验通过后,才把成品图移进公网可读的 ARTIFACT_DIR。
    这样即使被注入的 headline 诱导 codex 乱写文件,也碰不到其他帖子的已发海报,
    也不会把 render_poster.py 之类脚本落到 /artifacts/ 泄露本地路径。"""
    from .providers.cli_provider import _clean_env

    with tempfile.TemporaryDirectory(prefix="ypimg_") as tmp:
        tmp_out = os.path.join(tmp, f"{stem}.{ext}")
        brief = build_brief(headline=headline, platform=platform, palette=palette,
                            key_points=key_points, brand_name=brand_name, style=style,
                            language=language, out_path=tmp_out)
        cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write",
               "-c", "model_reasoning_effort=low", brief]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=_timeout(),
                           cwd=tmp, env=_clean_env())
        except Exception as e:  # noqa: BLE001
            log.warning("codex image render failed to launch: %s", e)
            return None

        # 校验(在 temp 内):文件存在 + 是合法图片 + 尺寸接近预期
        if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < 1024:
            log.warning("codex image render produced no valid file")
            return None
        try:
            from PIL import Image
            with Image.open(tmp_out) as im:
                im.verify()
            with Image.open(tmp_out) as im2:
                w0, h0 = im2.size
            want = PLATFORM_SIZE.get(platform, DEFAULT_SIZE)
            if w0 < want[0] * 0.5 or h0 < want[1] * 0.5:
                log.warning("codex image too small: %sx%s", w0, h0)
                return None
        except Exception as e:  # noqa: BLE001
            log.warning("codex image not a valid image: %s", e)
            return None

        # 校验通过 → 把成品图(且只有它)移进公网产物目录
        final_path = os.path.join(str(ARTIFACT_DIR), f"{stem}.{ext}")
        try:
            import shutil
            shutil.move(tmp_out, final_path)
        except OSError as e:
            log.warning("failed to move rendered poster into artifacts: %s", e)
            return None
    return final_path
