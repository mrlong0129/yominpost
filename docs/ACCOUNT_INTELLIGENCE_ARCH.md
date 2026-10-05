# 账号情报 + 运营状态 + 广告链接:系统架构

> 2026-07-14。目标(用户 goal):把「连接 → 内容 → 发布」升级成一个**以账号为中心**的
> 运营系统。核心动因是 2026-07 的 IG 封号事故 —— 我们对账号的**健康度与背景一无所知**,
> 直到平台封了才发现。本架构补上这一层,并把广告投放(AIGC+GTM)接进同一账号视图。
> 关联 [[yominpost-product]]、`docs/PLATFORM_RISK.md`。

---

## 一、分层框架(账号为中心)

```
                    ┌─────────────────────────────────────────────┐
                    │              一个社媒账号(Channel)          │
                    └─────────────────────────────────────────────┘
   ① 身份/连接        ② 账号情报(NEW)      ③ 运营状态(NEW)      ④ 广告(扩展)
   platform/OAuth     健康度+背景            渠道/发布/数据        ad account 链接
   token(加密)       风控画像              有无广告/配置         AIGC→GTM 投放
   connected_at       ─ account_intel.py    ─ operations.py       ─ ads_* providers
                      定期刷新              聚合只读              meta_ads/x_ads
        │                   │                     │                     │
        └───────────────────┴─── 单一真源:Channel + 派生存储 ──────────┴───────
                                    (SQLite hybrid,零迁移优先)
```

四层都挂在**同一个 Channel** 上,互相看得见:情报层给运营层风险信号,运营层给广告层
投放依据,广告层的数据回流到运营层。这解决了此前"发布只知道自己发了什么、不知道账号
本身怎么样"的断层。

---

## 二、引擎驱动:脚本 vs LLM 的明确分工

**原则:能用确定性脚本拿到的事实,绝不交给 LLM;只有"判断/叙述/归因"才用 LLM,且必须
有规则兜底(离线可跑,和整套 provider 链一致)。**

| 能力 | 引擎 | 为什么 | 产出 |
|---|---|---|---|
| 账号硬信号 | **脚本** `account_intel.build_signals` | 事实,必须精确、可复现、零成本 | followers/media_count/account_type、账号年龄、发布节奏、风控/熔断态 |
| 平台限额/能力 | **脚本** `platform_risk.POLICY/CAVEATS` | 静态规则 | 每平台上限、warm-up、TikTok 私密门 |
| 健康评分 | **脚本(规则)** `account_intel.score_health` | 可解释、可审计、可回归测试 | 0-100 分 + 扣分明细 |
| 背景/健康叙述 | **LLM**(provider)+ 规则兜底 | 需要综合叙述"这账号什么背景、现在什么状态、下一步" | 一段人话诊断 + 建议动作 |
| 内容质量/风险文案判断 | **LLM**(已有 qa/lint 混合) | 语义判断 | — |
| 发布节奏/上限判定 | **脚本** `platform_risk.check_cadence` | 硬规则 | block/warn |
| 广告投放决策 | **人审 + LLM 提案** `ads_*` | LLM 只提案,人点批准 | — |

> LLM 部分统一走 `services.active_provider()`(codex/agy/template 链),失败自动降级规则版,
> 绝不因为 LLM 掉线就没有账号情报。

---

## 三、数据模型(零迁移优先,存 Channel.config 派生)

不新建表(遵循混合 payload 模式 + 零迁移纪律),账号情报/广告链接作为 Channel 的
**派生子对象**存 `config` 内,各自带独立刷新时间戳,由专门引擎维护:

```
Channel.config = {
  ...(既有 token/handle/error/risk_frozen_until)...
  "account_profile": {            # account_intel.py 维护,定期刷新
    "checked_at": ISO,
    "signals": { followers, media_count, account_type, account_age_days,
                 posts_30d, cadence_per_week, last_posted_at, frozen, risk_flags[] },
    "health_score": 0-100,
    "health_grade": "healthy|watch|at_risk|blocked",
    "score_breakdown": [{factor, delta, why}],
    "assessment": "一段 LLM/规则诊断",
    "recommended_actions": [str],
    "engine": {"signals":"script","assessment":"codex|template"}
  },
  "ad_account": {                 # ads 链接(meta_ads/x_ads),ads_link.py 维护
    "provider": "meta_ads|x_ads",
    "ad_account_id": str, "status": "connected|pending|not_configured",
    "linked_at": ISO, "config": {...}
  }
}
```

好处:与账号 1:1、随 Channel 读写、旧数据零迁移、可随时提升为独立表。

---

## 四、连接时(Connection)就采集账号背景

`complete_oauth` 成功后立即跑一次 `account_intel.refresh_profile(channel)`(best-effort,
失败不挡连接),于是**从连上的第一刻就有健康基线**——这正是此次事故缺的:新号零信任、
零历史,应在连接时就标记"⚠️ 新账号,需 warm-up",而不是发了才知道。

定期维护:scheduler 每 6h 的健康 tick 里,除既有 token 探针外,顺带刷新 profile
(熔断渠道跳过)。手动:`POST /api/channels/{id}/profile/refresh`。

---

## 五、运营状态(Operations)聚合

`operations.py` 只读聚合,给用户"我的每个渠道现在到底怎么样"的单屏答案:

```
GET /api/workspaces/{ws}/operations  →
  channels: [{
    platform, handle, status, health_grade, health_score,
    publish: {posted, scheduled, failed, drafts, last_posted_at, cadence_per_week},
    performance: {impressions?, likes?, ...}   # 来自已同步帖 meta,缺则省略(不假造)
    ads: {linked, provider, status, spend?}     # 有无该渠道广告
    alerts: [来自 config.error / risk_frozen_until]
  }],
  rollup: {channels, healthy, at_risk, blocked, total_posts_30d, ad_linked}
```

前端:Connections 页每卡显示健康分/等级 + 一个新的「运营总览」视图。

---

## 六、广告链接(AIGC + GTM)

新增两个
广告 provider,与内容账号**同源链接**:

- **meta_ads**(覆盖 Instagram + Facebook,共用 Meta app + Marketing API `act_<id>`):
  一个 Meta 广告账户可同时投 IG/FB,天然贴合我们已连的 IG/FB 内容渠道。
- **x_ads**(X Ads API,`ads-api.x.com`)。
- 统一抽象:`status()`(连接/授权/就绪)+ `report(account)`(归一化 campaigns/
  spend/perf)。真实 API 需各自的广告权限审批,未就绪时
  **诚实返回 pending 而非假数据**(沿用现有纪律)。

GTM 闭环:内容(AIGC)→ 发布 → 运营数据 → 广告 provider 拉投放表现 → LLM 提案
→ 人审批 → 调整。账号情报的健康分作为"能不能加投"的前置闸。

---

## 七、落地顺序(每阶段独立可用 + 测试 + 验证)

1. **账号情报引擎** `account_intel.py`(脚本信号 + 规则评分 + LLM/规则叙述)+ 连接时/定时
   刷新 + API + Connections 卡片健康分。← 直接补此次事故的盲区
2. **运营状态聚合** `operations.py` + `/operations` API + 运营总览视图。
3. **广告链接框架** meta_ads / x_ads provider(连接 + status + 归一化 report 抽象,honest
   pending)。
4. 真实 Meta/X Ads API 深接(需广告权限审批)—— 框架就绪后逐步接入。

> 纪律:每阶段 pytest 绿 + 浏览器验证 + commit/push;生产部署前问用户。数值/阈值单点
> 定义;LLM 路径必有规则兜底;绝不假造平台数据。
