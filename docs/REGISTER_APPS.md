# 开发者应用注册清单(运营者一次性)

所有平台的**回调地址(Redirect URI)统一填**:
```
<你的域名>/oauth/callback
# 本地: http://localhost:8300/oauth/callback
```
> 注意:X / Google / Facebook / Discord / Reddit 允许 localhost 做开发回调;
> 正式对外用需换成你的 **https 公网域名**(多数平台要求 https)。

拿到 Client ID / Secret 后,填到 **Settings 页**对应卡片,或写进服务器 `.env`(见 `.env.example`)。

---

## 1. Reddit(最快,无审核)
- 后台:https://www.reddit.com/prefs/apps → **create another app**
- 类型选 **web app**;**redirect uri** 填回调地址
- 保存后:应用名下方那串是 **client id**;**secret** 字段是密钥
- 填入 Settings 的 Reddit 卡(或 `REDDIT_CLIENT_ID/SECRET`)
- 发帖需在渠道里指定目标 subreddit

## 2. Discord(快,无审核)
- 后台:https://discord.com/developers/applications → **New Application**
- 左侧 **OAuth2** → **Redirects** 添加回调地址
- scope 用 `webhook.incoming`(YominPost 已内置);授权时你选一个服务器+频道,Discord 自动建 webhook
- 复制 **Client ID** 与 **Client Secret** → Settings 的 Discord 卡(或 `DISCORD_CLIENT_ID/SECRET`)

## 3. X / Twitter(2026 现状:需付费额度才能真发)
- 前提:该 X 账号已验证**邮箱 + 手机**(开发者账号必需)。
- 后台:https://developer.x.com(或 https://console.x.com)→ 登录 → 申请开发者账号 → 建 Project + App。
- App → **Settings → User authentication settings → Set up**:
  - **App permissions = Read and write**(默认 Read only 不能发!必须先设这个)
  - **Type of App = Web App / Automated App or Bot**(机密客户端,会给 Client Secret)
  - **Callback URI** 精确填 `http://localhost:8300/oauth/callback`(若拒绝 localhost 就用 `http://127.0.0.1:8300/oauth/callback`)
  - **Website URL** 必须填一个**真实 https 网址**(不能是 localhost,填你的域名/GitHub 即可)
  - 保存
- 到 **Keys and tokens** 标签 → **OAuth 2.0 Client ID and Client Secret** 区:复制两者(**Secret 只显示一次**)→ Settings 的 X 卡(或 `X_CLIENT_ID/SECRET`)。
- ⚠️ **重要**:X 已对新开发者关闭免费档,改为**按量付费**(约 $0.015/条)。建 App、配 OAuth 都免费,但**真正发推需要先在 Console 加支付方式/充额度**,否则 `POST /2/tweets` 会失败。授权连接本身不花钱。

## 4. LinkedIn
- 后台:https://www.linkedin.com/developers/apps → **Create app**(需关联一个 LinkedIn 公司主页)
- **Products** 里申请:**Sign In with LinkedIn using OpenID Connect** + **Share on LinkedIn**(拿到 `w_member_social`)
- **Auth** 标签 → **Authorized redirect URLs** 填回调地址
- 复制 **Client ID / Primary Client Secret** → Settings 的 LinkedIn 卡(或 `LINKEDIN_CLIENT_ID/SECRET`)

## 5. Facebook Page(Graph API v25;开发模式下你自己的主页可直接真发)
- 前提:你的 FB 账号是目标**主页的管理员**;有一个 **Business 组合(Business Portfolio)**。
- 后台:https://developers.facebook.com/apps → **Create app** → 填名称/邮箱 → Use case 选 **Other** → Type 选 **Business** → 关联/新建 Business 组合 → 创建。
- 左栏 **Add product → Facebook Login → Set up**;进 **Facebook Login → Settings**:
  - **Valid OAuth Redirect URIs** 填 `http://localhost:8300/oauth/callback`;确认 Client/Web OAuth Login = Yes → 保存。
- **App settings → Basic**:复制 **App ID** 与(点 Show)**App Secret** → Settings 的 Facebook 卡(或 `FACEBOOK_CLIENT_ID/SECRET`)。
- 权限:`pages_show_list` `pages_manage_posts` `pages_read_engagement` `business_management`。
- ✅ **快速路径**:App 处于 **Development 模式**时,作为 App 管理员的你**无需审核**即可发到**你自己管理的主页**。
  (要给别人用 / 发别人的主页,才需切 Live + App Review + 商业验证,耗时数天~数周。)
- YominPost 已自动:换长时令牌 → 取 `/me/accounts` 第一个主页的 page token → 发到 `/{page_id}/feed`。

## 6. Instagram(复用同一个 Meta 应用)
- 前提(缺一不可):① **IG 专业账号**(商业/创作者,个人号不行,在 IG App 里切换);
  ② 一个你管理的 **Facebook 主页**;③ 该 IG 账号**已关联到那个 FB 主页**。
- 同一个 Facebook 应用里 **Add product → Instagram**(选「**API setup with Facebook login**」那种,不是 Instagram login 那种)。
- 追加权限:`instagram_basic` `instagram_content_publish`(回调、凭据都复用 Facebook,YominPost 已自动)。
- ⚠️ **硬限制**:IG 发帖**只能发图片/视频,且图片必须是 Instagram 服务器能访问的公网 URL**(localhost 不行),不支持纯文本。
  → 所以在**本地**:Instagram **只能"授权连接"成功,无法发布**。要真发需:把 YominPost 部署到**公网域名**,并让海报以 **JPG + 公开 URL** 提供(这块我可以再帮你加)。

## 7. YouTube(Google Cloud)
- 后台:https://console.cloud.google.com → 新建项目
- **APIs & Services → Library** 启用 **YouTube Data API v3**
- **OAuth consent screen**:类型 External;加 scope `youtube.upload` `youtube.readonly` `userinfo.profile`;
  把你自己加为 **Test user**
- **Credentials → Create credentials → OAuth client ID → Web application**;
  **Authorized redirect URIs** 填回调地址
- 复制 **Client ID / Client Secret** → Settings 的 YouTube 卡(或 `GOOGLE_CLIENT_ID/SECRET`)
- ⚠️ YouTube「发布」= 上传视频文件;当前 YominPost 产文案/海报,YouTube 现阶段仅打通「授权连接」

---

## 完成后如何验证
1. 在 **Settings** 填好某平台 Client ID/Secret(或设好 env 重启)。
2. 到 **Connections** 点该平台 **Connect** → 会**整页跳转到该平台真实授权页** → 你登录并同意。
3. 回到应用即显示「已连接 @你的账号」。
4. **Create post** → 选该渠道 → 发布。可真实发布的:X / LinkedIn / Reddit / Facebook / Discord /
   Mastodon / Telegram / Bluesky / Webhook。Instagram / YouTube 现阶段仅连接(见上)。
