"""运行时配置与 Provider 选择。

默认使用离线确定性的 TemplateProvider（保证无密钥也能端到端跑通、测试稳定）。
若设置了 ANTHROPIC_API_KEY 且 YOMINPOST_PROVIDER != 'template'，则用真模型，
且真模型失败时自动回退到模板逻辑。
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
# 零配置默认:数据/产物放在 ~/.yominpost/(源码树之外,uvx 临时环境里跑也不会丢)。
# 兼容旧部署:若包目录下已有 data/ 或 artifacts/(早期默认位置),继续沿用,避免"丢库"。
HOME_DIR = Path(os.environ.get("YOMINPOST_HOME", Path.home() / ".yominpost")).expanduser()


def _default_dir(name: str) -> Path:
    legacy = PACKAGE_DIR / name
    return legacy if legacy.is_dir() else HOME_DIR / name


DATA_DIR = Path(os.environ.get("YOMINPOST_DATA_DIR") or _default_dir("data")).expanduser()
ARTIFACT_DIR = Path(os.environ.get("YOMINPOST_ARTIFACT_DIR") or _default_dir("artifacts")).expanduser()
DB_PATH = Path(os.environ.get("YOMINPOST_DB", DATA_DIR / "yominpost.db"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# 模型
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# CLI 别名(haiku/sonnet/opus)→ Anthropic API 全模型 id 的映射。
# YOMINPOST_MODEL 是给 claude CLI 用的别名;若拿它直接当 API model id 会 404,
# 故 anthropic 驱动优先读专用的 YOMINPOST_ANTHROPIC_MODEL,并对别名做映射兜底。
_ANTHROPIC_ALIAS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-4-8",
}


def _anthropic_model() -> str:
    m = os.environ.get("YOMINPOST_ANTHROPIC_MODEL", "").strip()
    if m:
        return m
    ym = os.environ.get("YOMINPOST_MODEL", "").strip()
    if ym in _ANTHROPIC_ALIAS:
        return _ANTHROPIC_ALIAS[ym]
    if ym.startswith("claude-"):      # 已是完整 API id
        return ym
    return "claude-opus-4-8"          # 默认


ANTHROPIC_MODEL = _anthropic_model()


def default_provider_name() -> str:
    """决定默认 provider 名称。"""
    forced = os.environ.get("YOMINPOST_PROVIDER", "").strip().lower()
    if forced:
        return forced
    return "anthropic" if ANTHROPIC_API_KEY else "template"


# 运营者在服务器 env 配置一次的各平台 OAuth 应用(沿用 TryPost / 通用命名)。
# 配好后,用户点 Connect 即一键直达平台授权页,无需任何填写。
ENV_APP_CREDS: dict[str, tuple[str, str]] = {
    "x": ("X_CLIENT_ID", "X_CLIENT_SECRET"),
    "linkedin": ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"),
    "reddit": ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"),
    "facebook": ("FACEBOOK_CLIENT_ID", "FACEBOOK_CLIENT_SECRET"),
    # Instagram 直连(Instagram Login)用独立的 Instagram App ID/Secret,不复用 Facebook
    "instagram": ("INSTAGRAM_CLIENT_ID", "INSTAGRAM_CLIENT_SECRET"),
    "threads": ("THREADS_CLIENT_ID", "THREADS_CLIENT_SECRET"),
    "tiktok": ("TIKTOK_CLIENT_ID", "TIKTOK_CLIENT_SECRET"),
    "youtube": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    "pinterest": ("PINTEREST_CLIENT_ID", "PINTEREST_CLIENT_SECRET"),
    "discord": ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET"),
}


def env_app_creds(platform: str) -> tuple[str, str]:
    """从服务器环境变量读取平台 OAuth 应用凭据(运营者一次性配置)。"""
    names = ENV_APP_CREDS.get(platform)
    cid = csec = ""
    if names:
        cid = os.environ.get(names[0], "").strip()
        csec = os.environ.get(names[1], "").strip()
    # 通用回退命名 YOMINPOST_{PLATFORM}_CLIENT_ID / _SECRET
    up = platform.upper()
    cid = cid or os.environ.get(f"YOMINPOST_{up}_CLIENT_ID", "").strip()
    csec = csec or os.environ.get(f"YOMINPOST_{up}_CLIENT_SECRET", "").strip()
    return cid, csec


def provider_availability() -> dict:
    """探测各真实驱动是否可用(CLI 是否在 PATH / 是否有 key)。"""
    import shutil

    return {
        "template": True,
        "claude_code": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
        "agy": shutil.which("agy") is not None,
        "anthropic": bool(ANTHROPIC_API_KEY),
    }


_REAL_DRIVERS = ("claude_code", "codex", "agy", "anthropic")


def _make_live_provider(name: str, tmpl):
    """构造单个真驱动(不含链);不可用/构造失败返回 None。"""
    try:
        if name in ("claude_code", "claude-code", "claudecode"):
            from .providers.cli_provider import ClaudeCodeProvider
            return ClaudeCodeProvider(model=os.environ.get("YOMINPOST_MODEL", "haiku"), fallback=tmpl)
        if name == "codex":
            from .providers.cli_provider import CodexProvider
            return CodexProvider(model=os.environ.get("YOMINPOST_CODEX_MODEL", ""), fallback=tmpl)
        if name == "agy":
            from .providers.cli_provider import AgyProvider
            return AgyProvider(model=os.environ.get("YOMINPOST_AGY_MODEL", ""), fallback=tmpl)
        if name == "anthropic" and ANTHROPIC_API_KEY:
            from .providers.anthropic_provider import AnthropicProvider
            return AnthropicProvider(model=ANTHROPIC_MODEL, fallback=tmpl)
    except Exception:  # noqa: BLE001
        return None
    return None


def make_provider(name: str | None = None):
    """工厂:按名称构造 provider。

    默认构造「弹性回退链」:选中的真驱动排第一,其余可用真驱动作后备,
    template 垫底。任一 CLI 掉线(agy 返回空 / claude 401 / codex 额度)时
    自动切换到下一个真驱动,不再静默退化成模板。
    YOMINPOST_DRIVER_FALLBACK=0 可关闭(只用选中驱动,失败即模板)。
    """
    name = (name or default_provider_name()).lower()
    from .providers.template_provider import TemplateProvider

    tmpl = TemplateProvider()
    if name == "template":
        return tmpl

    fallback_on = os.environ.get("YOMINPOST_DRIVER_FALLBACK", "1") != "0"
    avail = provider_availability()

    # 排序:选中的优先,其余真驱动按默认偏好补位
    order = [name] + [d for d in _REAL_DRIVERS if d != name]
    live = []
    seen = set()
    for d in order:
        if d in seen or not avail.get(d):
            continue
        seen.add(d)
        p = _make_live_provider(d, tmpl)
        if p is not None:
            live.append(p)
        if not fallback_on:
            break  # 只要选中的那个

    if not live:
        return tmpl
    if len(live) == 1:
        return live[0]

    from .providers.chain import ChainProvider
    return ChainProvider(live, tmpl)
