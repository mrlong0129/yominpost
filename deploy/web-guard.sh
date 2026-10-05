#!/usr/bin/env bash
# YominPost 后端(uvicorn:8300)守护。tunnel-guard 只保隧道,uvicorn 死了隧道仍在→502。
# 本守护每 15s 探测本地 8300 健康,进程消失或连续不健康就用 serve.sh 重启。
# 装在 ~/.yominpost(非 Desktop)以避开 macOS Sequoia 对 Desktop 的 TCC 限制。
set +e
# 项目目录:优先 YOMINPOST_PROJECT_DIR(守护脚本被拷到 ~/.yominpost 时必设),否则取脚本上级目录。
PROJ="${YOMINPOST_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PYBIN="$PROJ/.venv/bin/python"
ENVF="$HOME/.yominpost.env"
URL="http://127.0.0.1:8300/api/health"
PATTERN="uvicorn yominpost.web.app:app .*8300"
FAIL_LIMIT=2
fails=0

log() { echo "$(date '+%F %T') $*"; }

start_web() {
  pkill -f "$PATTERN" 2>/dev/null
  sleep 1
  set -a; [ -f "$ENVF" ] && . "$ENVF"; set +a
  # YOMINPOST_PUBLIC_URL 在 ~/.yominpost.env 里设置(例如 https://post.example.com)
  # 公网启动前强制要有访问密钥,否则拒绝拉起(避免裸奔;与 app 启动自检一致)。
  if [ -z "${YOMINPOST_ACCESS_KEY:-}" ]; then
    log "REFUSING to start: YOMINPOST_ACCESS_KEY 未设置(公网部署必须设);请在 $ENVF 配置后重试"
    return 1
  fi
  cd "$PROJ" || { log "cannot cd $PROJ (TCC?)"; return 1; }
  nohup "$PYBIN" -m uvicorn yominpost.web.app:app --host 127.0.0.1 --port 8300 \
      >> /tmp/yp_serve.log 2>&1 &
  log "started uvicorn 8300 (pid $!)"
}

probe() { /usr/bin/curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$URL" 2>/dev/null || echo 000; }

log "web-guard up"
while true; do
  if ! pgrep -f "$PATTERN" >/dev/null 2>&1; then
    log "uvicorn missing"; start_web; fails=0; sleep 15; continue
  fi
  code=$(probe)
  if [ "$code" = "200" ]; then
    fails=0
  else
    fails=$((fails+1))
    log "unhealthy (http=$code) $fails/$FAIL_LIMIT"
    if [ "$fails" -ge "$FAIL_LIMIT" ]; then start_web; fails=0; fi
  fi
  sleep 15
done
