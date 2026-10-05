#!/usr/bin/env bash
# 安装(避开 macOS Sequoia 对 ~/Desktop 的 TCC 限制,守护必须装在 ~/.yominpost):
#   cp deploy/tunnel-guard.sh ~/.yominpost/ && chmod +x ~/.yominpost/tunnel-guard.sh
#   cp deploy/love.yomin.tunnel-guard.plist ~/Library/LaunchAgents/
#   launchctl load -w ~/Library/LaunchAgents/love.yomin.tunnel-guard.plist
# YominPost 隧道自愈守护 —— 自包含版,置于 ~/.yominpost(避开 macOS Sequoia 对 ~/Desktop
# 的 TCC 限制:launchd 后台项读 Desktop 会 "Operation not permitted")。
#
# 根因:本机 ClashX(fake-ip/TUN)在网络层间歇截断 cloudflared→Cloudflare 边缘的长连接
# (cloudflared 走 198.18.45.x 假IP)→ 访问者看到 Error 1033。本守护每 20s 绕过代理探测公网,
# 连续失败就用「清代理 env + http2」重启 cloudflared,自动恢复;进程消失也拉起。
set -u
CF="/opt/homebrew/bin/cloudflared"
LOG="$HOME/.yominpost/tunnel.log"
URL="${YOMINPOST_PUBLIC_URL:?set YOMINPOST_PUBLIC_URL}/api/health"
PATTERN="cloudflared tunnel .* run yominpost"
FAIL_LIMIT=3
mkdir -p "$HOME/.yominpost"
log() { echo "$(date '+%F %T') [guard] $*" >> "$LOG"; }
running() { /usr/bin/pgrep -f "$PATTERN" >/dev/null 2>&1; }

start_tunnel() {
  # 注:试过 --edge-ip-version 6 绕开 ClashX fake-ip——不通:ClashX 劫持时 AAAA 回空,
  # v6-only 反而"无地址可连"多耗 1-2 分钟才回退。维持 auto,靠本守护快速自愈;
  # 根治只能在 ClashX 侧给 +.argotunnel.com 加 fake-ip-filter。
  /usr/bin/pkill -f "$PATTERN" 2>/dev/null || true
  sleep 2
  nohup env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
    "$CF" tunnel --protocol http2 run yominpost >> "$LOG" 2>&1 &
  log "restarted cloudflared (http2, proxy stripped)"
  sleep 15
}

probe() { /usr/bin/curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 12 "$URL" 2>/dev/null || echo 000; }

log "guard up (pid $$)"
running || start_tunnel

fails=0
while true; do
  if ! running; then
    log "cloudflared missing"; start_tunnel; fails=0; sleep 20; continue
  fi
  code="$(probe)"
  if [ "$code" = "200" ]; then
    fails=0
  else
    fails=$((fails + 1))
    log "unhealthy (http=$code) $fails/$FAIL_LIMIT"
    if [ "$fails" -ge "$FAIL_LIMIT" ]; then start_tunnel; fails=0; fi
  fi
  sleep 20
done
