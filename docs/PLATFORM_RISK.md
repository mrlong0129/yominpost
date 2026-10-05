# 平台封控根因 + 全平台 AIGC 发布风控设计

> 2026-07-14。起因:Instagram 账号(一个新开的品牌营销号)在
> 开号第 3 天被 Meta `API access blocked` 全端点封锁,重新授权无效。本文定位根因,
> 并把「不再让 AIGC 发布成为系统性风险」落成 `platform_risk.py` 的产品化风控层。
> 研究结论均带官方/一手来源(4-agent 联网调研,2026-07-14 核实)。

---

## 一、根因判定:账号级 integrity/spam 封控(不是限流,不是应用级)

**结论:这是 Meta 对该 IG 账号的账号级(ig_user 维度)反垃圾/完整性执法,由一组
典型的「新号 + 自动化 + 低质 AIGC 营销」信号触发。不是 API 限流,也不是应用级封禁,
重新授权无效。**

### 排除法(证据链)

| 假设 | 判定 | 决定性证据 |
|---|---|---|
| API 限流 | ❌ 排除 | 官方上限 **100 帖/24h**(移动窗口),我们 30h 才发 3 帖,远未触限。限流会给 code 4/17/32,不是 `API access blocked`。 |
| 应用级封禁 | ❌ 排除 | 同一 Meta app family 下的 **Facebook 主页仍能发**;OAuth 授权+换 token 仍成功;07-09 `/me/media` 拉取正常。若是应用级封禁,FB 和 OAuth 会一起挂。 |
| 令牌/权限问题 | ❌ 排除 | 07-08~09 正常发了 3 帖;token 新换的仍被拒;`OAuthException code 200` 是权限/访问被封,不是令牌失效(那是 code 190)。 |
| **账号级 integrity 封控** | ✅ **判定** | **所有 `graph.instagram.com` 调用(含 `/me`)全挂 = ig_user 维度封锁**;错误码 200 不自述范围,需三角定位;触发信号齐全(见下)。 |

### 触发信号(我们自己踩的雷,按权重)

1. **新号第 1 天就上 API 自动化发布**(最大单一诱因)。账号 07-08 才有内容,零粉丝,当天即 API 发帖。实践共识:<90 天新号无信任分,接 API 自动化是被限最快的路径,需先 **2–4 周纯人工养号**(≤1 帖/天、先别带链接)。来源:multilogin/proxidize 养号指南、creatorflow ManyChat 合规文档。
2. **首帖把 LLM prompt 原文当 caption 发出去**(`为 <品牌> 的 <产品>…写一条 Instagram…`)。这正中 Meta **2025-04「Spammy Content」新政**的「caption 与内容无关 / 机器批量生产」+ **2025-07「unoriginal content」清理**(H1 2025 处置 ~50 万账号)。来源:about.fb.com/news/2025/04、techcrunch 2025-07-14。
3. **诱导互动 CTA**(`Comment or DM "ASIN" and I'll send…`)。Meta 明令 demote engagement-bait,对重复使用者升级处罚。来源:transparency.meta.com「Types of content we demote」。
4. **每帖同域外链**(品牌官网)。Meta Spam 标准:即使低频,只要有「重复内容/同链导流」等信号即可限制;对 URL 执法会**连带封禁当前及未来**含该链接的内容。来源:transparency.meta.com/policies/community-standards/spam。
5. **同产品重复营销 + FB/IG 交叉发**(交叉发本身低风险 —— Mosseri 2025:原创性只在平台内判定,这也解释了为何 FB 存活、IG 被封)。

> 一句话:**一个零信任的新号,在第一天就用 API 发了一条"看起来就是机器生成的"带诱导
> 互动和重复外链的营销帖 —— 这是 Meta 反垃圾分类器的教科书级正样本。**

---

## 二、解除路径(按优先级)

平台侧才能解,YominPost 侧只能停手 + 提示。顺序:

1. **手机 IG App → 设置 → 账户状态(Account Status)**:完成任何待处理的安全验证/checkpoint。
2. **删除/清理**首帖 prompt-leak 帖 + 停用所有诱导互动 CTA(留着=持续的违规证据)。
3. **申诉**:business.facebook.com/accountquality(Account Quality)提交复审。
4. **停用 API 发布 2–4 周**,期间**纯人工** ≤1 帖/天养号;第 3–4 周再 API ≤1 帖/天,每周 +~20%。
5. 解除后一般 24–48h 恢复;恢复后同一连接无需改码即可用。

> ⚠️ **绝不在封控期反复"重连/重试"** —— 每次调用都在加重自动化画像。这条已在代码里
> 用熔断硬性阻断(见下)。

---

## 三、全平台 AIGC 发布风控设计(`platform_risk.py`)

三道防线,**硬安全停无条件生效;软风控门 `YOMINPOST_RISK_GATE` 默认开(测试关)**:

```
发布前(pre_publish_issues)          发布后(错误分类)              后台(健康探针)
─────────────────────────           ──────────────────           ──────────────
① 熔断态?→ 硬拦(无条件)            is_platform_block?           frozen? → 跳过探针
② 内容 lint(prompt泄漏/诱导          → freeze_channel(熔断72h)    is_platform_block?
   互动/hashtag/多链)               is_auth_error?                → freeze
③ 节奏(24h上限/最小间隔/            → 标 config.error(重连)      auth? → 标 error
   新号warm-up爬坡)                 其它 → 普通失败(可重试)      好 → 自愈
④ 重复度(Jaccard) + 同域连发
   → block 拦下 / warn 留痕
```

### 关键设计决策

- **鉴权失败 vs 平台封锁,明确互斥**(本次事故的直接教训):`API access blocked` 含
  `OAuthException` 但**重连无效**,归 `is_platform_block`(熔断),不再误判成 auth error
  去提示用户"重新连接即可"。`is_auth_error` 已显式排除平台封锁。
- **熔断 = stop-and-alert,永不自动重试**:与 Buffer/Later/Postiz/Mixpost 一致 ——
  对被标记账号继续调 API 只会更糟。熔断期内**发布/健康探针/内容同步全部跳过**,
  只挂全局通知等人工。(Postiz IG 源码原话:session invalidated「常因频繁发布,
  请重新授权并等几天」;Mixpost `isUnauthorized()` 直接短路不碰 API。)
- **内容 lint 是我们比成熟工具多做的一层**:调研发现 Buffer/Later/SocialBee 都**没有**
  banned-phrase/spam 语言检测(「a gap」)。我们加了 **prompt 泄漏检测**(直接拦本次首帖
  那种事故)和**诱导互动检测**(Meta 系 block 级)。
- **新号 warm-up 爬坡**:接入未满 N 天按更严的 `warmup_max_per_day` 限流 —— 直击本次
  「第一天就 API 高频发」的头号诱因。

### 每平台策略表(`POLICY`,数值=官方上限与成熟工具保守实践的更小者)

| 平台 | 24h上限 | 最小间隔 | hashtag | warm-up | 官方硬约束 / 备注(来源核实 2026-07) |
|---|---|---|---|---|---|
| instagram | 2 | 180m | 5 | 14d/1帖 | 官方 100/24h,新号实际远低;`GET /{ig-user-id}/content_publishing_limit` 查额度 |
| facebook | 4 | 60m | 5 | 7d/2帖 | 无硬日上限,BUC 限流(4800×活跃用户/24h) |
| threads | 4 | 60m | 3 | 7d/2帖 | 官方 250 帖 + 1000 回复/24h |
| x | 6 | 30m | 3 | 5d/2帖 | **免费层 ~17 帖/24h/用户;2026-02 起新号无免费额度** —— 当稀缺渠道 |
| tiktok | 3 | 120m | 5 | 7d/1帖 | **⚠️ 未过审应用只能发 SELF_ONLY(私密)、24h≤5 用户;公开需过 client audit** |
| pinterest | 6 | 30m | 6 | 5d/2帖 | Trial 1000 req/日,建 pin 300/日 |
| linkedin | 3 | 120m | 5 | 5d/1帖 | 限额未公开,端点各异,UTC 午夜重置 |
| reddit | 2 | 240m | 0 | 14d/1帖 | 100 QPM;**社区对自我推广极敏感,同链跨版连发易被封** |
| youtube | 2 | 240m | 8 | 7d/1帖 | 视频上传;配额按 quota units |
| mastodon/bluesky/telegram/discord | 10–20 | 3–10m | 8–10 | 无 | 自有/联邦渠道,平台风控弱,只防节奏失控 |

数值可继续按实际收紧/放宽,单点定义在 `platform_risk.POLICY`。平台发布能力硬约束
(TikTok 私密、X 稀缺等)在 `PLATFORM_CAVEATS`,预检时以信息级(ℹ️)附带提示。

### 落点(enforcement points)

- **前端预检** `riskPreflight`(4 个发布入口):block → 弹窗阻止;warn → confirm;
  caveat → 只在已有 block/warn 时附带展示,不单独 nag。`GET /api/posts/{id}/risk-check`。
- **服务端硬闸** `services._publish_targets`:熔断无条件拦 → 软门(gate 开时)lint/节奏/
  重复 → 出手发布。**前端可绕过,服务端不可绕过。**
- **错误分类** 发布/健康探针失败 → `is_platform_block` 优先 `freeze_channel`。
- **熔断跳过** 发布 / `check_channel_health` / `sync_all_channels` 全部 skip frozen。
- **全局通知** 熔断/鉴权失败写 `config.error` → `channel_notifications()` → 顶部横幅。

---

## 四、本次事故 → 对应防线(逐条闭环)

| 我们犯的错 | 现在拦它的规则 |
|---|---|
| 首帖把 prompt 原文当 caption 发 | `lint_content` prompt_leak(**block**) |
| `Comment or DM "ASIN"` 诱导互动 | `lint_content` engagement_bait(Meta 系 **block**) |
| 新号第 1 天就 API 发 | `check_cadence` warm-up 爬坡(**block**,IG 14 天内 1 帖/天) |
| 30h 连发 3 帖 | `check_cadence` 最小间隔 + 24h 上限(**block**) |
| 每帖同域外链 | `check_duplicate` link_streak(**warn**) |
| 同产品重复文案 | `check_duplicate` Jaccard 重复度(≥85% **block**) |
| 8 个 hashtag | `lint_content` hashtag_over(**warn**) |
| 封控后反复重连/重试 | `freeze_channel` 熔断 + 无条件跳过(**hard stop**) |
| 把「API access blocked」当成"重连即可" | `is_auth_error` 排除平台封锁,提示改为"人工解封" |

---

## 五、待办 / 后续

- [ ] IG 账号按「二、解除路径」人工申诉恢复(平台侧,YominPost 侧无法代做)。
- [ ] 可选:接 `GET /{ig-user-id}/content_publishing_limit` 实时读 IG 剩余额度,替代静态估算。
- [ ] 可选:Connections 卡片常驻展示 `PLATFORM_CAVEATS`(如 TikTok 私密提示)。
- [ ] 可选:warm-up 期自动把「立即发布」降级为「排期到次日」而非直接拦下。

来源明细见本次调研 journal(4 个联网 agent,summary/findings 带 URL):
`.../subagents/workflows/wf_27a6d64d-b9c/journal.jsonl`。
