# YominPost 架构说明

一句话:**FastAPI + SQLite + 无构建原生 JS SPA** 的自托管社媒发布平台。一份素材 →
AI 规划 Loop 为每个渠道生成一版预发稿 → 运营者审阅授权 → 真实 OAuth 连接器发布 →
后台调度器按时自动发出定时帖。

本文回答「前端这些数据有 SQL 吗、怎么存的」并给出前后端一体的完整设计。

---

## 1. 数据到底存在哪(回答核心问题)

**有 SQL。** 所有业务数据落在一个 SQLite 库(默认 `<DATA_DIR>/yominpost.db`,WAL 模式)。
前端不持有任何权威数据 —— 它只是 REST 客户端,每次操作都读/写后端;浏览器里只有易失的
UI 态(当前草稿 `window._composeDrafts`、选中渠道等),刷新即由后端重建。

存储采用 **「可查询列 + JSON payload」混合模式**:高频过滤/排序的字段单独建列并加索引,
完整对象序列化成 JSON 存 `payload` 列。好处是既有关系型的查询能力,又不必为 Pydantic
模型的每次演进写 `ALTER TABLE`。

### 表清单(`yominpost/db.py` 的 `_BASE_DDL`)

| 表 | 关键列(建索引) | payload 装载模型 | 用途 |
|---|---|---|---|
| `workspaces` | id | Workspace | 工作区(多租户根) |
| `channels` | id, workspace_id | Channel | 渠道连接(含 Fernet 加密的 OAuth 令牌) |
| `posts` | id, workspace_id, status, scheduled_at | Post | 草稿/定时/已发帖,核心对象 |
| `assets` | id, workspace_id | Asset | 素材库(上传图片等) |
| `topics` | id, workspace_id | Topic | 选题库 |
| `compose_runs` | id, workspace_id, created_at | ComposeRun | 每次 Create-post 生成的可恢复记录 |
| `settings` | key | —(纯 KV) | 驱动选择、能力开关等运营配置 |
| `oauth_states` | state | —(短期 KV) | OAuth 授权中转态,10 分钟 TTL |

> 另有 `yominpost/store.py` 用**独立**的极简 SQLite 存 8 能力槽流水线的运行结果与品牌
> DNA(JSON blob),由 `orchestrator.py` 使用,与上表解耦。

### 迁移机制

`db.py` 用 `PRAGMA user_version` 做轻量迁移:`SCHEMA_VERSION` + `_MIGRATIONS` 列表,
`init_db()` 首次建表后按版本号顺序补齐索引/结构,`_INITED` 保证每个 db_path 只初始化一次。
连接统一 `PRAGMA busy_timeout=5000` 降低并发写锁冲突。

### 韧性装载

`_load(Model, rows)` 逐行 `model_validate_json`,遇到损坏行**跳过并记日志**而非整体崩溃 ——
一条脏数据不会让整个列表接口 500。

---

## 2. 模块地图

```
yominpost/
├── web/app.py          FastAPI 应用:REST API + SPA 托管 + 鉴权中间件 + lifespan
│   └── static/         前端 SPA(index.html / app.js / styles.css,零构建)
├── models.py           Pydantic 实体 + 平台目录(PLATFORM_CATALOG)
├── db.py               SQLite 数据层(建表/迁移/CRUD/原子认领/备份)
├── services.py         业务编排:compose 规划Loop / 发布 / 到期tick / 播种
├── scheduler.py        进程内 asyncio 后台循环(发布+备份+清理)
├── auth.py             操作员密钥鉴权(HMAC 签名 Cookie)
├── crypto.py           Fernet 令牌加解密(密钥落 <DATA_DIR>/.secret.key)
├── config.py           路径/驱动配置(全部可用环境变量覆盖)
├── capabilities.py     能力池(text/image/video 驱动的可用性探测 + 开关)
├── oauth_flow.py       OAuth2 授权码 + PKCE 通用流程
├── oauth_specs.py      各平台 OAuth 端点/scope 规格
├── connectors/         渠道连接器:connect→publish→delete 全链路
│   ├── real.py         token/webhook 类真实平台(Telegram/Discord/…)
│   ├── oauth_conn.py   OAuth 类真实平台(X/Facebook/Instagram/…)
│   └── mock.py         模拟连接器(未接真实应用的平台)
├── providers/          生成驱动:template / claude_code / codex / anthropic
├── stages/             8 能力槽:brand_dna/ideation/trends/router/copy/visual/video/qa
├── orchestrator.py     串联 8 能力槽的流水线
└── store.py            流水线运行/品牌 DNA 的独立持久化
```

---

## 3. 关键流程

### 3.1 Post Creating Loop(`services._compose_core`)

```
素材(文字/URL/图片/视频 混合)
  └─ parse_material   多模态识别 + 抓网页要点(_fetch_page 带 SSRF 防护,逐跳校验)
  └─ ① analyze        深度剖析素材:产品/受众/痛点/价值点/证据/语言推断
                      (notes 是运营者指令,绝不进内容)
  └─ 每渠道(按渠道语言 en/zh):
       ② research     该平台该领域什么有权重/优势/增长空间(真驱动可联网检索)
       ③ strategy     收敛唯一最优策略:形式/角度/要点/钩子方向/CTA目标
                      (受 target_format 与能力池强约束)
       ④ generate     copy.write(语言指令) → qa.evaluate → 带批评迭代(≤max_iters)
       资产            按策略产出海报/分镜(能力池裁剪)
  └─ save_compose_run 全程落库:progress 实时进度 + trace 驱动溯源 + fallback 标记
返回 每渠道一版预发稿(标注真实驱动) → 审阅编辑 → 授权发布
```

**异步执行:** `POST /compose/start` 立即返回 run_id,后台线程跑 Loop,
前端每 3s 轮询 `GET /api/compose-runs/{id}` 渲染 ①②③④ 阶段进度条。

**驱动溯源(反静默降级):** 每个 provider 调用都带 `_meta{driver,live,fallback_from}`,
`_Tracer` 汇总进 run.trace;任一渠道文案兜底 → 卡片标 ⚠️模板兜底,
全部兜底 → run.fallback=true + 前端红色横幅。CLI provider 兜底会打 warning 日志。

**语言:** 请求 language > 渠道 config.language(Connections 页 🌐 选择器,
PATCH /api/channels/{id})> 素材语言推断 > 中文。prompt 注入输出语言指令;
template 兜底也有英文脚手架(_t_copy_en),不会给英文渠道产出中文。

**驱动池:** template / claude_code / codex / **agy**(Antigravity CLI,`agy -p`)/
anthropic。嵌套 CLI 调用会清洗宿主会话注入的 env(ANTHROPIC_BASE_URL 等),
避免被会话代理劫持导致 401。

### 3.2 发布与定时(`publish_post` / `run_due_posts` / `scheduler.py`)

- 手动发布:`publish_post` 置 `publishing` → `_publish_targets` 逐渠道调 connector → 回填永久链接。
- 定时发布:后台调度器每 `TICK_SEC=30s` 调 `run_due_posts` →
  **`claim_scheduled_post` 原子认领**(`UPDATE … WHERE status='scheduled'`,同时用 `json_set`
  同步 payload,rowcount==1 才算认领)→ 防止并发/多 tick 重复发布。
- 崩溃恢复:启动时 `recover_stuck_publishing` 把卡在 `publishing` 超时的帖回退为可重试。
- 维护:调度器周期性 `purge_oauth_states`(清过期授权态)与一致性 `backup_to`(sqlite backup
  API,保留最近 7 份,轮转)。

### 3.3 请求生命周期(鉴权中间件)

```
请求 → _auth_middleware
  ├─ auth 未启用(无 ACCESS_KEY)→ 放行(仅本地开发)
  ├─ 开放路径(/login /api/health …)或 Cookie 签名有效 → 放行
  ├─ /api|/oauth|… 未登录 → 401(前端 _req 捕获 401 自动跳 /login)
  └─ 其他页面未登录 → 302 /login
```

Cookie = `ts.HMAC(key, ts)`,`check_cookie` 验签 + 30 天过期;**换 key 即令所有旧 Cookie 失效**。

---

## 4. 前后端契约

- 前端只经 `api.{get,post,patch,del}`(`app.js`)访问后端;统一错误处理:401 跳登录、其余 toast。
- **编辑已有帖用 PATCH**(`S.editingId` 存在时),避免重复创建;新建才 POST。
- 时间统一 **后端存 UTC、前端按本地时区渲染**(`localDayKey`/`localHM`/`isoToLocalInput`),
  日历分桶与 `datetime-local` 回填都走本地换算。
- 上传走流式落盘 + 大小上限(`YOMINPOST_MAX_UPLOAD_MB`,默认 15MB),超限即中止删半成品。

---

## 5. 安全与运维

| 面向 | 措施 |
|---|---|
| 访问控制 | 操作员密钥 HMAC Cookie 中间件;公网**必设** `YOMINPOST_ACCESS_KEY` |
| 令牌安全 | OAuth 令牌 Fernet 加密存 `channels.payload`;密钥独立文件 `.secret.key`(600) |
| SSRF | `_fetch_page` 只允许公网 http(s),逐跳解析 IP,拒绝内网/回环/云元数据 |
| 数据位置 | 数据/产物/密钥经 env 落在**源码树之外**(`~/.yominpost/`),不入 git |
| 备份 | 调度器每日 `backup_to` + 轮转;迁移数据目录务必带上 `.secret.key` |
| 并发 | 原子认领防重复发布;`busy_timeout` 降低写锁冲突 |

### 部署

本地 uvicorn(`127.0.0.1:8300`)→ Cloudflare Tunnel → 你的域名(如 `https://post.example.com`)。
`deploy/serve.sh` 加载 `~/.yominpost.env` 后启动。配置全部走环境变量(见 `.env.example`)。

---

## 6. 已知取舍 / 后续

- 单文件 `web/app.py` 偏大,后续可按 router 拆分(workspaces/posts/channels/oauth)。
- 列表接口暂无分页;数据量上来后需加 limit/offset。
- 演示用内置 IdP(`/oauthtest/*`)生产环境应关闭或移除。
- SQLite 适合单机自托管;多副本需改用 Postgres 并把 `db.py` 换成连接池。
