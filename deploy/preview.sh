#!/usr/bin/env bash
# 预览实例:临时数据 / 关鉴权 / 模板驱动(秒出),供 UI 冒烟验证,不碰生产数据。
set -e
export YOMINPOST_DATA_DIR=/tmp/yp_preview/data
export YOMINPOST_DB=/tmp/yp_preview/data/db.sqlite
export YOMINPOST_ARTIFACT_DIR=/tmp/yp_preview/art
export YOMINPOST_PROVIDER=template
export YOMINPOST_ENABLE_SCHEDULER=0
export YOMINPOST_DISABLE_CODEX_IMAGE=1
unset YOMINPOST_ACCESS_KEY
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn yominpost.web.app:app --host 127.0.0.1 --port "${PORT:-8401}"
