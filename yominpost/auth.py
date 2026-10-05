"""操作员鉴权(单操作员产品)。

机制:一个 access key(env `YOMINPOST_ACCESS_KEY` 或 settings『access_key』)。
- 未设置 → 鉴权关闭(本地开发/测试便利);
- 设置了 → 中间件校验所有请求:未登录访问 UI 跳登录页,访问 API 返回 401。
登录:POST /api/login 提交 key,校验通过写入 HttpOnly 签名 cookie(HMAC,含时间戳)。
无服务器会话状态,纯自包含。公网部署务必设置 access key。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from . import db

COOKIE_NAME = "yp_auth"
MAX_AGE = 30 * 24 * 3600  # 30 天


def access_key() -> str:
    return (os.environ.get("YOMINPOST_ACCESS_KEY", "").strip()
            or (db.get_setting("access_key") or "")).strip()


def enabled() -> bool:
    return bool(access_key())


def _sign(payload: str) -> str:
    return hmac.new(access_key().encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie() -> str:
    """cookie = <issued_ts>.<hmac(key, issued_ts)>,可校验且带签发时间。"""
    ts = str(int(time.time()))
    return f"{ts}.{_sign(ts)}"


def check_cookie(value: str) -> bool:
    if not value or "." not in value:
        return False
    ts, sig = value.rsplit(".", 1)
    if not ts.isdigit():
        return False
    if int(ts) + MAX_AGE < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(ts))


def check_key(candidate: str) -> bool:
    ak = access_key()
    return bool(ak) and hmac.compare_digest((candidate or "").strip(), ak)


def set_key(new_key: str) -> None:
    """把 access key 存入 settings(供无 env 场景;env 优先)。"""
    db.set_setting("access_key", (new_key or "").strip())
