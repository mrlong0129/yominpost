"""真·GPT 生图:调 OpenAI Images API(gpt-image-2 / gpt-image-1)。

按 codex-image-generation-runtime-notes.md 的架构落地:
- 模型只发结构化 prompt;真正的生图由 handler 调 OpenAI Images API 完成。
- 返回本地 artifact 绝对路径 + 元数据,不把 base64 塞回上下文。
- API key 只在 handler 环境读(OPENAI_API_KEY),模型/前端看不到。

需要环境变量:
  OPENAI_API_KEY        必填
  OPENAI_IMAGE_MODEL    可选,默认 gpt-image-1(账号支持则设 gpt-image-2)
"""
from __future__ import annotations

import base64
import logging
import os

from .config import ARTIFACT_DIR

log = logging.getLogger("yominpost.imaging_openai")

DEFAULT_BASE = "https://api.openai.com/v1"

# 各渠道 → OpenAI 支持的画幅(仅 1024x1024 / 1024x1536 / 1536x1024 / auto)
# 注意:IG feed 合法比例是 4:5(0.8)~1.91:1,2:3(0.667)会被拒;gpt-image 出不了 4:5,
# 故 IG 用 1:1(始终合法)。tiktok/小红书 接受更高的竖图,保留 1024x1536。
PLATFORM_IMG_SIZE = {
    "instagram": "1024x1024",   # 1:1,IG feed 合法
    "xiaohongshu": "1024x1536",
    "tiktok": "1024x1536",
    "facebook": "1536x1024",    # 横版
    "linkedin": "1536x1024",
    "x": "1536x1024",
    "youtube": "1536x1024",
}
DEFAULT_IMG_SIZE = "1024x1024"


def _setting(key: str) -> str:
    try:
        from . import db
        return (db.get_setting(key) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _secret_setting(key: str) -> str:
    try:
        from . import db
        return (db.get_secret_setting(key) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def api_key() -> str:
    # 兼容 aihubmix 等 OpenAI 兼容服务商:优先专用 IMAGE key,再回退 OPENAI_API_KEY
    return (os.environ.get("YOMINPOST_IMAGE_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or _secret_setting("openai_api_key"))   # 加密存储的设置


def base_url() -> str:
    """生图接口 base(OpenAI 兼容)。aihubmix = https://aihubmix.com/v1"""
    b = (os.environ.get("YOMINPOST_IMAGE_BASE_URL", "").strip()
         or os.environ.get("OPENAI_BASE_URL", "").strip()
         or _setting("openai_base_url") or DEFAULT_BASE)
    return b.rstrip("/")


def model() -> str:
    return (os.environ.get("OPENAI_IMAGE_MODEL", "").strip()
            or _setting("openai_image_model") or "gpt-image-1")


def available() -> bool:
    return bool(api_key())


def _timeout() -> int:
    try:
        return max(60, int(os.environ.get("YOMINPOST_IMG_TIMEOUT", "180")))
    except ValueError:
        return 180


def build_prompt(*, subject: str, headline: str, key_points: list[str],
                 palette: list[str], platform: str, style: str, brand_name: str) -> str:
    """把主题/文案/研究要点拼成 gpt-image 的图像提示词(描述画面,不是排版指令)。"""
    kp = "; ".join(str(k) for k in (key_points or [])[:3] if k)
    pal = ", ".join(c for c in (palette or []) if isinstance(c, str) and c.startswith("#"))
    parts = [
        "ABSOLUTE RULE: do NOT render any watermark, brand wordmark, logo, company name, "
        "footer bar, signature or credit text anywhere in the image (no 'YominPost', no made-up brand).",
        f"High-quality social media marketing visual for {platform}.",
        f"Theme: {subject}." if subject else "",
        f"Message to convey: {headline}." if headline else "",
        f"Supporting ideas: {kp}." if kp else "",
        f"Art direction: {style}." if style else "modern, clean, high-contrast, editorial.",
        f"Use a cohesive color palette around {pal}." if pal else "",
        "Photographic or polished 3D/graphic concept, professional advertising quality, "
        "strong focal point, good negative space. Any on-image text must be minimal and be "
        "real, legible, correctly-spelled English (no gibberish, no fake logos).",
    ]
    return " ".join(p for p in parts if p)


def render(*, subject: str, headline: str, key_points: list[str], palette: list[str],
           platform: str, style: str, brand_name: str, stem: str,
           quality: str = "high", ext: str = "png") -> str | None:
    """调 OpenAI Images API 生成图片,成功返回文件路径,失败返回 None(调用方回退)。"""
    key = api_key()
    if not key:
        return None
    size = PLATFORM_IMG_SIZE.get(platform, DEFAULT_IMG_SIZE)
    prompt = build_prompt(subject=subject, headline=headline, key_points=key_points,
                          palette=palette, platform=platform, style=style,
                          brand_name=brand_name)
    out_path = os.path.join(str(ARTIFACT_DIR), f"{stem}.{ext}")
    body = {
        "model": model(), "prompt": prompt[:4000], "size": size,
        "quality": quality if quality in ("low", "medium", "high", "auto") else "high",
        "n": 1,
    }
    try:
        import httpx
        url = base_url() + "/images/generations"
        resp = httpx.post(url, headers={"Authorization": f"Bearer {key}",
                                        "content-type": "application/json"},
                          json=body, timeout=_timeout())
        if resp.status_code != 200:
            log.warning("openai image %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        b64 = (data.get("data") or [{}])[0].get("b64_json")
        if not b64:
            # 有些账号返回 url
            url = (data.get("data") or [{}])[0].get("url")
            if url:
                img = httpx.get(url, timeout=_timeout())
                if img.status_code != 200 or not (img.headers.get("content-type", "").startswith("image")):
                    log.warning("openai image url fetch bad: %s %s", img.status_code, img.headers.get("content-type"))
                    return None
                with open(out_path, "wb") as f:
                    f.write(img.content)
                # 落到下方统一 PIL 校验
            else:
                log.warning("openai image: no payload")
                return None
        else:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
    except Exception as e:  # noqa: BLE001
        log.warning("openai image failed: %s", e)
        return None

    # 校验是合法图片
    try:
        from PIL import Image
        with Image.open(out_path) as im:
            im.verify()
    except Exception:  # noqa: BLE001
        return None
    return out_path
