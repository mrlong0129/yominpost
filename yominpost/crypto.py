"""令牌加密存储(Fernet)。

OAuth access/refresh token 落库前用对称密钥加密。密钥优先取环境变量
YOMINPOST_SECRET_KEY(urlsafe base64 32 字节),否则在数据目录生成并复用。
"""
from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from .config import DATA_DIR

log = logging.getLogger("yominpost.crypto")

_KEY_FILE = DATA_DIR / ".secret.key"


def _load_key() -> bytes:
    env = os.environ.get("YOMINPOST_SECRET_KEY", "").strip()
    if env:
        # 环境变量密钥必须是合法 Fernet key,否则在 import 期直接崩(且被守护无限重启)。
        # 提前校验并给出可操作报错,而不是让一个 typo 拖垮整个进程。
        try:
            Fernet(env.encode())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "YOMINPOST_SECRET_KEY 不是合法的 Fernet 密钥"
                "(需 urlsafe-base64 编码的 32 字节)。请用 "
                "`python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\"` "
                "重新生成。"
            ) from e
        return env.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


_fernet = Fernet(_load_key())
_warned_mismatch = False


def encrypt(text: str) -> str:
    if text is None:
        text = ""
    return _fernet.encrypt(text.encode()).decode()


def try_decrypt(token: str) -> tuple[bool, str]:
    """返回 (ok, plaintext)。ok=False 表示密文不是本密钥所能解(可能是历史明文值)。
    供需要"密文优先、旧明文兜底"的读取方区分两种情形。"""
    if not token:
        return True, ""
    try:
        return True, _fernet.decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001
        return False, ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        # 密钥不匹配(.secret.key 丢失/被轮换):所有历史令牌都读不出。
        # 这是配置事故,而非脏数据——响亮告警一次,便于运营者定位(否则只看到发布神秘失败)。
        global _warned_mismatch
        if not _warned_mismatch:
            _warned_mismatch = True
            log.error("加密密钥不匹配:无法解密已存令牌。请恢复原 .secret.key "
                      "或重新连接各渠道。")
        return ""
    except Exception:  # noqa: BLE001
        return ""
