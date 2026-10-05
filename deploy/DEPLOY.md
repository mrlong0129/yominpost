# 部署 YominPost 到 https://post.example.com(Cloudflare Tunnel)

思路:本地 Mac 跑 YominPost(127.0.0.1:8300),用 Cloudflare 命名隧道把它顶到
`https://post.example.com`,自动 HTTPS、无需云服务器、无需开端口、无需备案(隧道走出站)。

前提:你的域名(下文以 `example.com` 为例)的 DNS 需托管在 Cloudflare。

---

## A. 把 example.com 迁到 Cloudflare(你做,一次性)
1. 注册/登录 https://dash.cloudflare.com(免费 Free 计划即可)。
2. **Add a site** → 输入 `example.com` → 选 **Free** → Cloudflare 会自动扫描导入现有 DNS 记录。
   - ⚠️ 进 DNS 页**核对**:原 DNS 服务商上的记录(邮箱 MX、其他子域名等)是否都导入了,缺的手动补上,免得迁移后其它服务断掉。
3. Cloudflare 给你 **2 个 nameserver**(形如 `xxx.ns.cloudflare.com`)。
4. 到**域名注册商控制台**(如阿里云、Namecheap) → example.com → **DNS 修改 / 修改 DNS 服务器** → 把 NS 改成 Cloudflare 给的那 2 个 → 保存。
5. 等 Cloudflare 面板域名状态变 **Active**(通常几分钟~几小时)。收到 Active 后做 B。

## B. 建隧道(我做,域名 Active 后)
```bash
cloudflared tunnel login                      # 浏览器授权,选 example.com 这个 zone(你点同意)
cloudflared tunnel create yominpost           # 生成隧道 + 凭据 ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns yominpost post.example.com   # 在 Cloudflare 自动建 CNAME
```
写配置 `~/.cloudflared/config.yml`:
```yaml
tunnel: yominpost
credentials-file: /Users/YOUR_USER/.cloudflared/<UUID>.json
ingress:
  - hostname: post.example.com
    service: http://127.0.0.1:8300
  - service: http_status:404
```

## C. 跑起来(我做)
```bash
# 1) YominPost 带公网地址启动
cd ~/Desktop/loop.nosync/yominpost
YOMINPOST_PUBLIC_URL=https://post.example.com \
  .venv/bin/python -m uvicorn yominpost.web.app:app --host 127.0.0.1 --port 8300 &

# 2) 隧道
cloudflared tunnel run yominpost &
```
验证:`curl -s https://post.example.com/api/health` 应返回 `{"ok":true}`。

## D. 注册各平台应用(你做)—— 回调统一用公网地址
所有开发者后台的 **Redirect/Callback URI** 一律填:
```
https://post.example.com/oauth/callback
```
Facebook:Valid OAuth Redirect URIs;X:Callback URI(Website URL 也可填 https://post.example.com);
Instagram:同 Facebook 应用。填完把 client_id/secret 填进 YominPost 的 **Settings**,我驱动 Connect。

## 备注
- 公网上线后 **Instagram 可发布**(它要求公网图片 URL,post.example.com 满足);仍需把海报转 JPG 并公开托管(见 TODO)。
- 稳定长跑可把 uvicorn + cloudflared 各自做成 launchd 服务;先手动 `&` 起也行。
- 关掉暴露:`kill` 掉 cloudflared 进程即可,YominPost 仍在本地跑。
