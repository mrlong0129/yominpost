#!/usr/bin/env bash
# 以公网身份启动 YominPost(OAuth 回调 / 产物 URL 都用 YOMINPOST_PUBLIC_URL)
set -e
[ -f "$HOME/.yominpost.env" ] && set -a && . "$HOME/.yominpost.env" && set +a
cd "$(dirname "$0")/.."
export YOMINPOST_PUBLIC_URL="${YOMINPOST_PUBLIC_URL:?请在 ~/.yominpost.env 设置 YOMINPOST_PUBLIC_URL,例如 https://post.example.com}"
# 公网部署必须有访问密钥,否则拒绝启动(避免全站零鉴权暴露)。
if [ -z "${YOMINPOST_ACCESS_KEY:-}" ]; then
  echo "❌ 拒绝启动:YOMINPOST_ACCESS_KEY 未设置。请在 ~/.yominpost.env 配置一个长随机密钥。" >&2
  exit 1
fi
echo "启动 YominPost  PUBLIC_URL=$YOMINPOST_PUBLIC_URL  ->  127.0.0.1:8300"
exec .venv/bin/python -m uvicorn yominpost.web.app:app --host 127.0.0.1 --port 8300
