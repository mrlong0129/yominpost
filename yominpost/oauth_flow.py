"""通用 OAuth2 授权码流程(支持 PKCE / Basic 认证)。

被各平台 connector 复用:build_authorize_url -> (用户授权) -> exchange_code -> token。
PKCE 依据 RFC 7636(S256)。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

TIMEOUT = 20


def gen_state() -> str:
    return secrets.token_urlsafe(16)


def gen_pkce() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge[S256])。"""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def build_authorize_url(authorize_url: str, *, client_id: str, redirect_uri: str,
                        scope: str, state: str, code_challenge: str | None = None,
                        extra: dict | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if extra:
        params.update(extra)
    sep = "&" if "?" in authorize_url else "?"
    return f"{authorize_url}{sep}{urlencode(params)}"


def exchange_code(token_url: str, *, client_id: str, client_secret: str, code: str,
                  redirect_uri: str, code_verifier: str | None = None,
                  basic_auth: bool = False, scope: str | None = None,
                  extra_headers: dict | None = None) -> dict:
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        body["code_verifier"] = code_verifier
    if scope:
        body["scope"] = scope
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if basic_auth:
        token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    else:
        body["client_id"] = client_id
        if client_secret:
            body["client_secret"] = client_secret
    r = httpx.post(token_url, data=body, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return _parse_token_response(r)


def _parse_token_response(r) -> dict:
    """兼容 JSON 与 urlencoded 的 token 响应(Facebook 旧接口返回 querystring)。"""
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        from urllib.parse import parse_qs

        parsed = {k: v[0] for k, v in parse_qs(r.text).items()}
        if "expires" in parsed and "expires_in" not in parsed:
            parsed["expires_in"] = parsed["expires"]
        return parsed


def refresh_token(token_url: str, *, client_id: str, client_secret: str,
                  refresh: str, basic_auth: bool = False) -> dict:
    body = {"grant_type": "refresh_token", "refresh_token": refresh}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if basic_auth:
        token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    else:
        body["client_id"] = client_id
        if client_secret:
            body["client_secret"] = client_secret
    r = httpx.post(token_url, data=body, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()
