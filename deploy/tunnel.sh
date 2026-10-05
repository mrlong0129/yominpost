#!/usr/bin/env bash
# 启动 Cloudflare 隧道(你的域名 → 127.0.0.1:8300;隧道名默认 yominpost)。
#
# 若本机跑着代理(如 ClashX 的 fake-ip 模式 + HTTP(S)_PROXY)且 cloudflared
# 继承了代理环境变量,它到 Cloudflare 边缘的长连接会被 ClashX 反复截断 → 隧道抖动 → 1033。
# 解决:① 清掉代理 env,别让 cloudflared 走 ClashX;② 用 --protocol http2 回退(QUIC/UDP
# 也常被 fake-ip 挡)。彻底根治可在 ClashX 加规则 DOMAIN-SUFFIX,argotunnel.com,DIRECT。
exec env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
         -u http_proxy -u https_proxy -u all_proxy \
     cloudflared tunnel --protocol http2 run "${YOMINPOST_TUNNEL:-yominpost}"
