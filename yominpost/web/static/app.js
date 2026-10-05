/* YominPost SPA — vanilla JS。视图: 日历 / Studio / Posts / Connections / Assets / Topics + 组合器抽屉 */
const S = { ws: null, workspaces: [], platforms: [], platMap: {}, view: 'calendar',
            channels: [], weekStart: null, _renderTok: 0,   // 必须初始化:++undefined=NaN 会让加载失败兜底永不渲染
            settings: { provider: 'template', available: {} } };

const DRIVER_LABELS = { template: '模板(离线·即时)', claude_code: 'Claude Code(真实·较慢)',
                        codex: 'Codex(真实·慢)', agy: 'agy CLI(真实·较慢)',
                        anthropic: 'Anthropic API(真实)' };

const CAP_SLOTS = [
  ['brand_dna', '① 品牌知识库 / DNA', '官网项目 + 旧内容整理'],
  ['ideation', '② 选题 / Ideation / 内容日历', '生成选题并排入日历'],
  ['trends', '③ 趋势 / 洞察输入', '该写什么'],
  ['router', '④ 形式选择 Router', '规则化决策'],
  ['copy', '⑤ 文案生成', ''],
  ['visual', '⑥ 图文 / 视觉', ''],
  ['video', '⑦ 短视频', ''],
  ['qa', '⑧ 质量评估 / 自我批评', 'QA'],
];

/* ---------------- API(统一错误处理:401 跳登录,其余 toast) ---------------- */
async function _req(method, u, b) {
  let res;
  try {
    res = await fetch(u, {
      method,
      headers: b !== undefined ? { 'content-type': 'application/json' } : undefined,
      body: b !== undefined ? JSON.stringify(b || {}) : undefined,
    });
  } catch (e) { toast('网络错误:' + e.message); throw e; }
  if (res.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast('出错(' + res.status + '):' + (data.detail || data.error || '')); const err = new Error(data.detail || res.status); err.status = res.status; throw err; }
  return data;
}
const api = {
  get: (u) => _req('GET', u),
  post: (u, b) => _req('POST', u, b || {}),
  patch: (u, b) => _req('PATCH', u, b || {}),
  del: (u) => _req('DELETE', u),
};
const esc = (s) => (s == null ? '' : String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));
/* 发布前风控预检:block → 弹清单并阻止;warn → confirm 放行;platform_caveat 仅信息级
   (不单独触发弹窗,只在已有真实 block/warn 时附带展示)。服务端另有硬闸,这里是提前亮牌 */
async function riskPreflight(postId) {
  let rc;
  try { rc = await api.get(`/api/posts/${postId}/risk-check`); } catch (e) { return true; }
  const all = Object.entries(rc.issues || {}).flatMap(([pf, list]) =>
    (list || []).map((i) => ({ ...i, pf })));
  const gating = all.filter((i) => i.code !== 'platform_caveat');
  if (!gating.length) return true;
  const line = (i) => `[${i.pf}] ${i.level === 'block' ? '⛔' : i.code === 'platform_caveat' ? 'ℹ️' : '⚠️'} ${i.msg}`;
  const msgs = all.map(line);
  if (rc.has_block) { alert('风控拦截,已阻止发布:\n\n' + msgs.join('\n\n')); return false; }
  return confirm('风控提示(可继续,但建议先改):\n\n' + msgs.join('\n\n') + '\n\n仍要发布吗?');
}
// 仅放行 http(s) 链接(挡 javascript:/data: 等),再交给 esc 转义进属性
const safeHref = (u) => { const s = String(u || ''); return /^https?:\/\//i.test(s) ? s : ''; };

/* 时间:UTC ISO ↔ 本地。datetime-local 无时区,需按本地时区转换 */
function isoToLocalInput(iso) {                       // "2026-07-08T12:00:00+00:00" → 本地 "YYYY-MM-DDTHH:mm"
  const d = new Date(iso); if (isNaN(d)) return '';
  const off = d.getTimezoneOffset() * 60000;
  return new Date(d - off).toISOString().slice(0, 16);
}
function localDayKey(iso) {                           // UTC ISO → 本地日 "YYYY-MM-DD"(用于日历分桶)
  const d = new Date(iso); if (isNaN(d)) return (iso || '').slice(0, 10);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function localHM(iso) {                                // UTC ISO → 本地 "HH:mm"
  const d = new Date(iso); if (isNaN(d)) return (iso || '').slice(11, 16);
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}
function localStamp(iso) {                             // UTC ISO → 本地 "YYYY-MM-DD HH:mm"
  return iso ? `${localDayKey(iso)} ${localHM(iso)}` : '';
}
const toast = (m) => { const t = document.getElementById('toast'); t.textContent = m; t.classList.remove('hidden'); setTimeout(() => t.classList.add('hidden'), 2200); };

function pf(platform, size = 16) {
  const m = S.platMap[platform] || { label: platform, color: '#888' };
  const ch = (m.label || platform).slice(0, 1).toUpperCase();
  return `<span class="pf" title="${esc(m.label)}" style="display:inline-grid;place-items:center;width:${size}px;height:${size}px;border-radius:4px;background:${m.color};color:#fff;font-size:${Math.round(size*0.6)}px;font-weight:800;border:1px solid #14121a">${ch}</span>`;
}
const platColor = (platform) => (S.platMap[platform] || {}).color || '#888';

const icon = (name, cls = '') => `<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const ICO_EMPTY = '<span style="display:inline-block;width:12px;height:12px;border:2px solid currentColor;border-radius:3px;vertical-align:-1px"></span>';

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    try { const h = await api.get('/api/health'); if (h.auth_enabled) document.getElementById('logoutBtn').style.display = ''; } catch (e) {}
    S.platforms = await api.get('/api/platforms');
    S.platMap = Object.fromEntries(S.platforms.map((p) => [p.id, p]));
    try { S.settings = await api.get('/api/settings'); } catch (e) {}
    S.workspaces = await api.get('/api/workspaces');
    if (!S.workspaces.length) S.workspaces = [await api.post('/api/workspaces', { name: 'My Workspace' })];
    S.ws = S.workspaces[0];
    S.weekStart = mondayOf(new Date());
    renderWsSwitcher();
    bindNav();
    go('calendar');
    pollNotices();
    setInterval(pollNotices, 90000);   // 全局通知:渠道自动重连失败 → 人工介入
  } catch (e) {
    // 启动关键接口失败(401 会在 _req 里跳登录):给出可重试提示,而非留空白页
    if (e && e.message === 'unauthorized') return;
    main().innerHTML = '<div class="empty">初始化失败,请刷新页面重试。</div>';
  }
}

async function pollNotices() {
  let n;
  try { n = await api.get('/api/notifications'); } catch (e) { return; }
  S.notices = n;
  // 红点分流:健康问题(平台封控)挂「账号管理」;连接问题(重连)挂「Connections」
  const setDot = (view, count) => {
    const nav = document.querySelector(`.nav-item[data-view="${view}"]`); if (!nav) return;
    let dot = nav.querySelector('.nav-dot');
    if (count > 0) { if (!dot) { dot = document.createElement('span'); dot.className = 'nav-dot'; nav.appendChild(dot); } dot.textContent = count; }
    else if (dot) dot.remove();
  };
  const hc = n.health_count || 0, cc = n.connection_count || 0;
  setDot('accounts', hc);
  setDot('connections', cc);
  let bar = document.getElementById('globalNotice');
  if (!bar) { bar = document.createElement('div'); bar.id = 'globalNotice'; bar.className = 'gnotice hidden'; document.body.appendChild(bar); }
  const parts = [];
  if (hc > 0) parts.push(`${icon('warning','ico-sm')} <b>${hc} 个渠道健康异常</b>(${esc((n.health_alerts||[]).map((c)=>c.platform).join('、'))},平台封控/限制,非连接问题)
      <button class="btn" style="margin-left:8px;padding:3px 10px;font-size:12px" data-action="go-accounts">去渠道管理</button>`);
  if (cc > 0) parts.push(`${icon('plug','ico-sm')} <b>${cc} 个渠道需重新连接</b>(${esc((n.connection_errors||[]).map((c)=>c.platform).join('、'))})
      <button class="btn" style="margin-left:8px;padding:3px 10px;font-size:12px" data-action="go-connections">去 Connections</button>`);
  if (parts.length) { bar.innerHTML = parts.join('<span style="margin:0 10px;color:#cbd5e1">|</span>'); bar.classList.remove('hidden'); }
  else bar.classList.add('hidden');
}

function renderWsSwitcher() {
  document.getElementById('wsName').textContent = S.ws.name;
  document.getElementById('wsAvatar').textContent = S.ws.name.split(' ').map((w) => w[0]).join('').slice(0, 2).toUpperCase();
  const menu = document.getElementById('wsMenu');
  menu.innerHTML = S.workspaces.map((w) => `<div class="ws-opt" data-ws="${w.id}">${esc(w.name)}</div>`).join('')
    + `<div class="ws-opt add" data-action="new-ws">+ Create workspace</div>`;
}

function bindNav() {
  document.querySelector('.nav').addEventListener('click', (e) => {
    const item = e.target.closest('.nav-item'); if (!item) return;
    document.querySelectorAll('.nav-item').forEach((n) => n.classList.remove('active'));
    item.classList.add('active');
    go(item.dataset.view);
  });
  document.getElementById('wsSwitcher').addEventListener('click', () => document.getElementById('wsMenu').classList.toggle('hidden'));
  document.getElementById('wsMenu').addEventListener('click', async (e) => {
    const opt = e.target.closest('.ws-opt'); if (!opt) return;
    document.getElementById('wsMenu').classList.add('hidden');
    if (opt.dataset.action === 'new-ws') {
      const name = prompt('新 workspace 名称:'); if (!name) return;
      const w = await api.post('/api/workspaces', { name }); S.workspaces.push(w); S.ws = w;
      renderWsSwitcher(); go(S.view); toast('已创建 workspace');
    } else if (opt.dataset.ws) {
      S.ws = S.workspaces.find((w) => w.id === opt.dataset.ws);
      S.channels = [];                     // 换 workspace 必须清缓存渠道,否则编辑器会串用上个 ws 的渠道
      S.editingId = null; S.editTargets = [];
      renderWsSwitcher(); go(S.view);
    }
  });
  window.addEventListener('message', (e) => {
    if (e.data === 'oauth-done') { toast('授权完成'); if (S.view === 'connections') viewConnections(); }
  });
  document.body.addEventListener('click', globalActions);
  document.body.addEventListener('change', async (e) => {
    const cap = e.target.closest('[data-action="cap-toggle"]');
    if (cap) {
      await api.post('/api/capabilities', { id: cap.dataset.cap, enabled: cap.checked });
      toast((cap.checked ? '已启用 ' : '已停用 ') + cap.dataset.cap);
      return;
    }
    const taxo = e.target.closest('[data-action="ch-taxo"]');
    if (taxo) {
      await api.patch(`/api/channels/${taxo.dataset.id}`, { [taxo.dataset.kind]: taxo.value });
      toast(taxo.dataset.kind === 'positioning' ? '定位已更新' : '打法状态已更新');
      return;
    }
    const el = e.target.closest('[data-action="set-driver"]'); if (!el) return;
    const v = el.value;
    await api.post('/api/settings', { provider: v }); S.settings.provider = v;
    const st = document.getElementById('driverStatus'); if (st) st.textContent = '当前:' + (DRIVER_LABELS[v] || v);
    toast('已切换驱动:' + (DRIVER_LABELS[v] || v));
  });
}

function go(view) {
  S.view = view;
  const r = VIEWS[view] || VIEWS.calendar;
  const tok = ++S._renderTok;              // 视图竞态:慢响应回来时若已切走则不再覆盖
  Promise.resolve().then(r).catch((e) => {
    if (tok !== S._renderTok) return;       // 已切到别的视图,忽略这次失败
    main().innerHTML = '<div class="empty">加载失败,请重试或切换其它页面。</div>';
  });
}

/* ================= 视图 ================= */
const main = () => document.getElementById('main');

/* ---- 日历 ---- */
function mondayOf(d) { const x = new Date(d); const day = (x.getDay() + 6) % 7; x.setDate(x.getDate() - day); x.setHours(0,0,0,0); return x; }
async function viewCalendar() {
  S.calPosts = await api.get(`/api/workspaces/${S.ws.id}/calendar`);
  if (!S.calFilter) S.calFilter = { status: 'all', channel: 'all' };
  if (!S.calView) S.calView = 'week';          // week=周视图 | list=列表(原 POSTS 融合进来)
  // 校正陈旧的筛选:切 workspace/删帖/重试成功后,选中的渠道或状态可能已无帖——
  // 而零计数 chip 不渲染,残留筛选会把日历静默过滤成空且不可见、不可复位。
  const chans = new Set((S.calPosts || []).flatMap((p) => (p.targets || []).map((t) => t.platform)));
  if (S.calFilter.channel !== 'all' && !chans.has(S.calFilter.channel)) S.calFilter.channel = 'all';
  if (S.calFilter.status !== 'all' && !(S.calPosts || []).some((p) => p.status === S.calFilter.status)) S.calFilter.status = 'all';
  renderCalendar();
}

function calFilterMatch(p) {
  const f = S.calFilter || { status: 'all', channel: 'all' };
  if (f.status !== 'all' && p.status !== f.status) return false;
  if (f.channel !== 'all' && !(p.targets || []).some((t) => t.platform === f.channel)) return false;
  return true;
}

function calCard(p) {
  const dated = p.scheduled_at || p.posted_at;
  const time = dated ? localHM(dated) : ({ failed: '未发布', publishing: '…' }[p.status] || 'Draft');
  const plats = (p.targets || []).map((t) => pf(t.platform)).join('');
  // 同步来的真帖不给一键删除(会真删平台原帖,不可逆);发布中也不给(服务端 409,发布线程会复活它)
  const delBtn = (p.synced_from || p.status === 'publishing') ? '' : `<button class="cal-del" data-action="del-post" data-id="${p.id}" title="删除">${icon('trash','ico-sm')}</button>`;
  return `<div class="cal-post ${p.status}" data-action="open-post" data-id="${p.id}">
      ${delBtn}
      <div class="t">${time} <span class="cal-badge">${p.status}</span></div>
      <div>${plats} ${esc((p.text || p.title || '').slice(0, 42))}…</div>
    </div>`;
}

function calFilterBar() {
  const f = S.calFilter || { status: 'all', channel: 'all' };
  const n = (v) => (S.calPosts || []).filter((p) => v === 'all' ? true : p.status === v).length;
  const sts = [['all', '全部'], ['draft', 'Draft'], ['scheduled', 'Scheduled'], ['posted', 'Posted'], ['failed', 'Failed']];
  const stChips = sts.filter(([v]) => v === 'all' || n(v))   // 没有该状态的帖就不渲染 chip,少噪音
    .map(([v, l]) => `<span class="chip ${f.status === v ? 'on' : ''}" data-action="cal-fstatus" data-v="${v}">${l} ${n(v)}</span>`).join('');
  const chans = [...new Set((S.calPosts || []).flatMap((p) => (p.targets || []).map((t) => t.platform)))];
  const chChips = chans.length
    ? `<span class="cfl" style="margin-left:10px">渠道</span>
       <span class="chip ${f.channel === 'all' ? 'on' : ''}" data-action="cal-fchan" data-v="all">全部</span>` +
      chans.map((c) => `<span class="chip ${f.channel === c ? 'on' : ''}" data-action="cal-fchan" data-v="${esc(c)}">${esc(c)}</span>`).join('')
    : '';
  const viewSeg = `<span class="cal-viewseg">
      <span class="chip ${S.calView === 'week' ? 'on' : ''}" data-action="cal-view" data-v="week">📅 周视图</span>
      <span class="chip ${S.calView === 'list' ? 'on' : ''}" data-action="cal-view" data-v="list">📄 列表</span></span>`;
  return `<div class="cal-filter">${viewSeg}<span class="cfl" style="margin-left:10px">状态</span>${stChips}${chChips}</div>`;
}

function calWeekHtml(posts) {
  const days = [...Array(7)].map((_, i) => { const d = new Date(S.weekStart); d.setDate(d.getDate() + i); return d; });
  const dayName = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
  const byDay = {};
  // 全部按 cal_date 落到某天(草稿/失败无排期则用创建日),统一在日历管理,靠筛选分状态查看
  posts.forEach((p) => {
    const k = localDayKey(p.scheduled_at || p.posted_at || p.cal_date || '');
    (byDay[k] = byDay[k] || []).push(p);
  });
  const cols = days.map((d, i) => {
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const items = (byDay[iso] || []).map(calCard).join('');
    return `<div class="cal-col"><div class="cal-daylabel">${dayName[i]}<div class="d">${d.getDate()}/${d.getMonth() + 1}</div></div>
      <div class="cal-body"><div class="cal-add" data-action="compose">+</div>${items}</div></div>`;
  }).join('');
  return `<div class="cal-grid">${cols}</div>`;
}

/* 列表模式(原 POSTS 列表融合进 Calendar):按时间倒序,状态感知操作 */
function calListItem(p) {
  const canPublish = !p.synced_from && p.status !== 'posted' && p.status !== 'publishing';
  const openLabel = (p.status === 'posted' || p.status === 'publishing') ? '查看数据' : (p.status === 'failed' ? '详情' : '编辑');
  return `
    <div class="card post-item">
      ${p.thumb_url ? `<a href="${esc(safeHref((p.targets && p.targets[0] && p.targets[0].permalink) || '') || p.thumb_url)}" target="_blank" rel="noopener noreferrer" class="post-thumb"><img src="${esc(p.thumb_url)}" alt="" loading="lazy"/></a>` : ''}
      <div class="body">
        <div class="row"><span class="badge ${p.status}">${p.status}</span>
          ${p.synced_from ? `<span class="badge" title="从账号同步的真实已发帖">${icon('sync','ico-sm')} 已同步</span>` : ''}
          ${(p.targets || []).some((t) => t.sandbox) ? `<span class="badge" title="模拟渠道,未真实发布到平台">${icon('flask','ico-sm')} 模拟</span>` : ''}
          ${(p.targets || []).map((t) => pf(t.platform, 18)).join(' ')}
          <span class="page-sub">${esc(localStamp(p.scheduled_at || p.posted_at || p.created_at || ''))}</span></div>
        <h4 style="margin-top:8px">${esc(p.title || '未命名')}</h4>
        <div class="snippet">${esc(p.text || '')}</div>
        ${(p.targets || []).filter((t) => safeHref(t.permalink)).map((t) => `<a class="tag" href="${esc(safeHref(t.permalink))}" target="_blank" rel="noopener noreferrer">🔗 ${esc(t.platform)}</a>`).join('')}
      </div>
      <div class="row" style="flex-direction:column;align-items:stretch;gap:6px;min-width:120px">
        ${canPublish ? `<button class="btn primary" data-action="publish-now" data-id="${p.id}">发布</button>` : ''}
        <button class="btn" data-action="open-post" data-id="${p.id}">${openLabel}</button>
        ${(!p.synced_from && p.status !== 'publishing') ? `<button class="btn danger" data-action="del-post" data-id="${p.id}">删除</button>` : ''}
      </div>
    </div>`;
}

function calListHtml(posts) {
  const key = (p) => p.posted_at || p.scheduled_at || p.cal_date || p.created_at || '';
  const sorted = [...posts].sort((a, b) => key(b).localeCompare(key(a)));
  const list = sorted.length ? sorted.map(calListItem).join('')
    : '<div class="empty">没有符合筛选的内容。点 + Create post 生成,或「同步账号内容」拉取已发帖。</div>';
  return `<div class="post-list">${list}</div>`;
}

function renderCalendar() {
  const posts = (S.calPosts || []).filter(calFilterMatch);
  const isWeek = S.calView !== 'list';
  const weekNav = isWeek
    ? `<button class="btn" data-action="cal-prev">‹</button>
       <button class="btn" data-action="cal-today">Today</button>
       <button class="btn" data-action="cal-next">›</button>` : '';
  main().innerHTML = `
    <div class="page-head"><div><div class="page-title">Calendar</div>
      <div class="page-sub">${isWeek ? '本周排期、草稿与已发布' : '全部内容'} · ${posts.length} 条</div></div>
      <div class="row">${weekNav}
      <button class="btn" data-action="sync-posts">${icon('sync')} 同步账号内容</button>
      <button class="btn mint" data-action="tick">${icon('play')} 运行到期定时帖</button>
      <button class="btn-primary" data-action="compose">+ Create post</button></div></div>
    ${calFilterBar()}
    ${isWeek ? calWeekHtml(posts) : calListHtml(posts)}`;
}

/* ---- Content Studio (8 能力槽 + 生成) ---- */
async function viewStudio() {
  const topics = await api.get(`/api/workspaces/${S.ws.id}/topics`);
  const platOpts = S.platforms.filter((p) => (p.category || 'social_media') !== 'ads').map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join('');
  const topicOpts = topics.map((t) => `<option value="${esc(t.title)}">${esc(t.title)}</option>`).join('');
  const slots = CAP_SLOTS.map(([k, label, sub]) => `<div class="slot" data-slot="${k}"><span class="dot"></span>${esc(label)}${sub?`<small>${esc(sub)}</small>`:''}</div>`).join('');
  main().innerHTML = `
    <div class="page-head"><div><div class="page-title">Content Studio</div>
      <div class="page-sub">8 能力槽内容引擎:选题 → Router → 文案 → 图文/短视频 → QA 自我批评</div></div></div>
    ${driverBar()}
    <div class="studio-grid">
      <div>
        <div class="card" style="margin-bottom:16px">
          <div class="field"><label>选题(可从话题库选,或直接输入)</label>
            <input id="g_topic" list="topicList" placeholder="例:用一条 thread 讲清 ASIN 优化的 5 个动作"/>
            <datalist id="topicList">${topicOpts}</datalist></div>
          <div class="field"><label>目标平台</label><select id="g_platform">${platOpts}</select></div>
          <div class="field"><label>品牌素材(可选,一行一条)</label><textarea id="g_sources" placeholder="官网文案 / 旧帖片段…"></textarea></div>
          <div class="field"><label>趋势输入(可选,一行一条)</label><textarea id="g_trends" placeholder="该写什么的趋势信号…"></textarea></div>
          <label style="font-weight:700;font-size:13px"><input type="checkbox" id="g_opt" checked/> ⑧ 启用 QA 自我批评重写</label>
          <div style="margin-top:12px"><button class="btn-primary" style="width:100%" data-action="generate">${icon('sparkles')} 生成内容</button></div>
        </div>
        <div class="slots"><h3>能力槽</h3>${slots}</div>
      </div>
      <div id="genOut" class="gen-output"><div class="empty">填写选题后点「生成内容」,右侧会显示各能力槽的产出。</div></div>
    </div>`;
}

function driverBar() {
  const avail = S.settings.available || {};
  const opts = Object.keys(DRIVER_LABELS).map((k) => {
    const ok = avail[k];
    const cur = S.settings.provider === k ? ' selected' : '';
    const dis = ok ? '' : ' disabled';
    const note = ok ? '' : ' (不可用)';
    return `<option value="${k}"${cur}${dis}>${DRIVER_LABELS[k]}${note}</option>`;
  }).join('');
  return `<div class="driver-bar">
    <b>${icon('gear')} 生成驱动</b>
    <select id="driverSel" data-action="set-driver">${opts}</select>
    <button class="btn" data-action="test-driver">${icon('play')} 测试驱动</button>
    <span class="status" id="driverStatus">当前:${DRIVER_LABELS[S.settings.provider] || S.settings.provider}${S.settings.cli_tasks ? ' · CLI 任务:' + S.settings.cli_tasks : ''}</span>
  </div>`;
}

function markSlots(active) {
  CAP_SLOTS.forEach(([k]) => {
    const el = document.querySelector(`.slot[data-slot="${k}"]`); if (!el) return;
    el.classList.remove('active');
    if (active.includes(k)) el.classList.add('done');
  });
}

async function doGenerate() {
  if (S._generating) return;                 // 防重复点击(CLI 驱动一次要 30-60s)
  const topic = document.getElementById('g_topic').value.trim();
  if (!topic) return toast('请先填写选题');
  const platform = document.getElementById('g_platform').value;
  const sources = document.getElementById('g_sources').value.split('\n').map(s=>s.trim()).filter(Boolean);
  const trends = document.getElementById('g_trends').value.split('\n').map(s=>s.trim()).filter(Boolean);
  const optimize = document.getElementById('g_opt').checked;
  const btn = document.querySelector('[data-action="generate"]');
  if (btn) btn.disabled = true;
  S._generating = true;
  document.getElementById('genOut').innerHTML = '<div class="empty">生成中…(品牌DNA→趋势→Router→文案→视觉/短视频→QA)</div>';
  let r;
  try {
    r = await api.post(`/api/workspaces/${S.ws.id}/generate`, { topic, platform, brand_sources: sources, trends, optimize });
  } catch (e) {
    document.getElementById('genOut').innerHTML = '<div class="empty">生成失败,请重试。</div>';
    return;
  } finally {
    S._generating = false;
    if (btn) btn.disabled = false;
  }
  window._lastGen = { r, topic, platform };
  const done = ['brand_dna','trends','ideation','router','copy','qa'];
  if (r.visual_url) done.push('visual'); if (r.video_url) done.push('video');
  markSlots(done);
  const qa = r.qa || {}; const sc = qa.scores || {};
  const scoreCells = Object.entries(sc).map(([k,v]) => `<div class="qa-cell"><div class="k">${k}</div><div class="v">${Math.round(v*100)}</div><div class="meter"><span style="width:${Math.round(v*100)}%"></span></div></div>`).join('');
  const issues = (qa.issues||[]).map(i=>`<div class="tag">[${i.severity}] ${esc(i.dimension)}: ${esc(i.message)}</div>`).join('') || '<span class="page-sub">无问题</span>';
  const c = r.content || {};
  document.getElementById('genOut').innerHTML = `
    <div class="card">
      <div class="row"><span class="badge">形式 ${esc(r.format)}</span>
        <span class="badge ${qa.passed?'posted':'failed'}">QA ${Math.round((qa.overall||0)*100)} ${qa.passed?'PASS':'需改'}</span>
        ${qa.revised?'<span class="badge scheduled">已自我批评重写</span>':''}<div class="spacer"></div>
        <button class="btn primary" data-action="use-gen">用到 Create post →</button></div>
      <h3 style="margin:12px 0 4px">${esc(c.hook||'')}</h3>
      <div style="white-space:pre-wrap;line-height:1.6">${esc(c.body||'')}</div>
      <div style="margin-top:8px;color:var(--muted)">${esc(c.cta||'')}</div>
      <div style="margin-top:8px">${(c.hashtags||[]).map(h=>`<span class="tag">${esc(h)}</span>`).join('')}</div>
    </div>
    <div class="card"><b>④ Router 规则轨迹</b><div style="margin-top:6px">${(r.router?.rule_trace||[]).map(t=>`<div class="tag">${esc(t)}</div>`).join('')}</div></div>
    <div class="card"><b>⑧ QA 打分</b><div class="qa-scores" style="margin-top:8px">${scoreCells}</div><div style="margin-top:8px">${issues}</div></div>
    ${r.visual_url?`<div class="card"><b>⑥ 图文 / 海报</b><div class="poster-preview" style="margin-top:8px"><img src="${r.visual_url}"/></div></div>`:''}
    ${r.video_url?`<div class="card"><b>⑦ 短视频字幕</b> <a class="btn" href="${r.video_url}" target="_blank">打开 SRT</a></div>`:''}`;
}

/* ---- Connections:只管「链接」这一件事 ----
   刻意不放健康/数据/广告/账号问题 —— 那些属于「账号管理」tab。平台封控(如 IG
   API access blocked)不是连接问题(OAuth 仍成功),故连接卡不标红、只给中性指引。 */
function _socialCard(p, byPlat) {
  const pillFor = (p) => p.auth === 'oauth' ? '<span class="pill-real">OAuth 授权</span>'
    : p.auth === 'credentials' ? '<span class="pill-real">真实可发</span>'
    : '<span class="pill-mock">模拟</span>';
  const connectAction = (p) => p.auth === 'oauth' ? 'oauth-connect'
    : p.auth === 'credentials' ? 'open-connect' : 'open-connect';
  const ch = byPlat[p.id]; const on = ch && ch.status === 'connected';
  const err = ch && ch.has_error;                 // 真·连接/鉴权错误(重连可解)才标红
  const healthAlert = ch && ch.health_alert;      // 平台封控 → 中性指引到账号管理
  const desc = err ? `<span style="color:#dc2626">⚠ 需重新连接:${esc(ch.config?.error||'鉴权失效')}</span>`
    : on ? ('@'+esc(ch.handle||ch.display_name))
    : ('连接你的 '+esc(p.label)+' 账号');
  return `<div class="conn-card ${on?'connected':''} ${err?'error':''}">
    ${on?'<div class="conn-check">✓</div>':''}
    <div class="conn-icon" style="background:${p.color}">${esc(p.label[0])}</div>
    <h4>${esc(p.label)} ${pillFor(p)}</h4>
    <p>${desc}</p>
    ${on && healthAlert ? `<div class="page-sub" style="margin-top:2px;color:#ca8a04;cursor:pointer" data-action="go-accounts">⚑ 渠道状态异常 → 渠道管理</div>` : ''}
    ${on
      ? `<div class="row" style="gap:6px;align-items:center;margin-top:6px">
           ${err ? `<button class="btn primary" data-action="${connectAction(p)}" data-platform="${p.id}">重新连接</button>` : ''}
           <button class="btn danger" data-action="disconnect" data-id="${ch.id}">Disconnect</button>
           <select class="ch-lang" data-id="${ch.id}" title="该渠道的内容输出语言"
             style="border:2px solid var(--line);border-radius:8px;padding:4px;font-size:12px">
             <option value="auto"${(ch.language||'auto')==='auto'?' selected':''}>🌐 auto</option>
             <option value="en"${ch.language==='en'?' selected':''}>🌐 EN</option>
             <option value="zh"${ch.language==='zh'?' selected':''}>🌐 中文</option></select></div>`
      : `<button class="btn primary" data-action="${connectAction(p)}" data-platform="${p.id}">Connect</button>`}
  </div>`;
}

/* 健康等级徽章(数据来自 _channel_out 的 health 摘要) */
const HEALTH_META = { healthy: ['#16a34a', '健康'], watch: ['#ca8a04', '留意'],
  at_risk: ['#ea580c', '高风险'], blocked: ['#dc2626', '已封控'] };
function healthBadge(ch) {
  const h = ch.health; if (!h || !h.grade) return '';
  const [color, label] = HEALTH_META[h.grade] || ['#94a3b8', h.grade];
  return `<div style="margin-top:4px"><span class="badge" style="border-color:${color};color:${color}">
    ● 健康 ${h.score ?? '—'} · ${label}</span>${h.followers != null ? `<span class="page-sub" style="margin-left:6px">粉丝 ${h.followers}</span>` : ''}</div>`;
}

async function viewConnections() {
  S.channels = await api.get(`/api/workspaces/${S.ws.id}/channels`);
  const byPlat = {}; S.channels.forEach((c) => { byPlat[c.platform] = c; });
  const socials = S.platforms.filter((p) => (p.category || 'social_media') === 'social_media');
  const socialCards = socials.map((p) => _socialCard(p, byPlat)).join('');
  main().innerHTML = `<div class="page-head"><div><div class="page-title">Connections</div>
    <div class="page-sub">发布渠道:连接 → 授权 → 发布</div></div></div>
    <div style="font-weight:900;color:#334155;margin:6px 0 10px">📣 Social Media</div>
    <div class="conn-grid">${socialCards}</div>`;
  main().querySelectorAll('.ch-lang').forEach((sel) => sel.addEventListener('change', async (e) => {
    await api.patch(`/api/channels/${e.target.dataset.id}`, { language: e.target.value });
    toast('渠道语言已更新:' + e.target.value.toUpperCase());
  }));
}


/* ---- Posts:原 POSTS 列表已融合进 Calendar 列表模式(calListHtml);旧 posts-* 路由重定向 ---- */
function gotoCalList(status) {
  S.calView = 'list';
  S.calFilter = { ...(S.calFilter || { channel: 'all' }), status };
  return viewCalendar();
}

/* ---- Scheduled:顶级「全局定时任务」视图(post 自动发布是其中一种能力)---- */
async function viewScheduled() {
  const [scheduled, publishing, failed] = await Promise.all([
    api.get(`/api/workspaces/${S.ws.id}/posts?status=scheduled`),
    api.get(`/api/workspaces/${S.ws.id}/posts?status=publishing`),
    api.get(`/api/workspaces/${S.ws.id}/posts?status=failed`),
  ]);
  scheduled.sort((a, b) => (a.scheduled_at || '').localeCompare(b.scheduled_at || ''));
  const row = (p, actions) => `
    <div class="card sched-row">
      <div class="body">
        <div class="row"><span class="badge ${p.status}">${p.status}</span>
          ${(p.targets || []).map((t) => pf(t.platform, 18)).join(' ')}
          <span class="page-sub">${p.scheduled_at ? '🕒 ' + esc(localStamp(p.scheduled_at)) : (p.posted_at ? esc(localStamp(p.posted_at)) : '')}</span></div>
        <h4 style="margin:6px 0 2px">${esc(p.title || '未命名')}</h4>
        <div class="snippet">${esc((p.text || '').slice(0, 110))}</div>
        ${p.status === 'failed' && p.meta && p.meta.error ? `<div class="page-sub" style="color:var(--danger)">${icon('warning','ico-sm')} ${esc(p.meta.error)}</div>` : ''}
      </div>
      <div class="row" style="flex-direction:column;align-items:stretch;gap:6px;min-width:120px">${actions}</div>
    </div>`;
  const upcoming = scheduled.length
    ? scheduled.map((p) => row(p, `
        <button class="btn primary" data-action="publish-now" data-id="${p.id}">立即发布</button>
        <button class="btn" data-action="open-post" data-id="${p.id}">改期</button>
        <button class="btn" data-action="unschedule" data-id="${p.id}">取消排期</button>`)).join('')
    : '<div class="empty">没有排期中的定时帖。到 Create post 生成后设定时间即可。</div>';
  const inflight = publishing.map((p) => row(p, '<span class="page-sub">发布中…</span>')).join('');
  const failedRows = failed.map((p) => row(p, `
        <button class="btn primary" data-action="retry-sched" data-id="${p.id}">重试</button>
        <button class="btn" data-action="open-post" data-id="${p.id}">详情</button>
        <button class="btn danger" data-action="del-post" data-id="${p.id}">删除</button>`)).join('');
  main().innerHTML = `
    <div class="page-head"><div><div class="page-title">Scheduled</div>
      <div class="page-sub">全局定时任务 · post 自动发布是其中一种(调度器每 30s 检查到期帖)</div></div>
      <div class="row"><button class="btn mint" data-action="tick-sched">${icon('play')} 运行到期定时帖</button>
      <button class="btn-primary" data-action="compose">+ Create post</button></div></div>
    <div class="sched-sec"><div class="sched-h">${icon('clock')} 即将执行 · ${scheduled.length}</div>
      <div class="post-list">${upcoming}</div></div>
    ${publishing.length ? `<div class="sched-sec"><div class="sched-h">⟳ 执行中 · ${publishing.length}</div><div class="post-list">${inflight}</div></div>` : ''}
    ${failed.length ? `<div class="sched-sec"><div class="sched-h">${icon('warning')} 失败可重试 · ${failed.length}</div><div class="post-list">${failedRows}</div></div>` : ''}
    <div class="sched-sec"><div class="sched-h">🧩 其它定时任务</div>
      <div class="page-sub" style="padding:4px 2px">系统维护(一致性备份 / 回收卡死的发布 / 清理过期 OAuth state)由后台调度器自动运行。</div></div>`;
}

async function syncPosts() {
  toast('正在从 FB/IG 账号同步…');
  try {
    const r = await api.post(`/api/workspaces/${S.ws.id}/sync-posts`, {});
    toast(`同步完成:回填 ${r.updated||0} 条 · 新增 ${r.imported||0} 条(${r.channels||0} 渠道)`);
    go(S.view);
  } catch (e) { /* _req 已提示错误 */ }
}

/* ---- Assets(素材库:文件夹式分类 —— 历史发布 / 本地上传 / 从别处获取)---- */
const ASSET_BUCKETS = [
  { k: 'all',       label: '全部' },
  { k: 'published', label: '🗂 历史发布' },
  { k: 'uploaded',  label: '⬆️ 本地上传' },
  { k: 'external',  label: '🌐 从别处获取' },
];
const ASSET_SITE = {
  helium10: 'Helium 10', perci: 'Perci.ai', storeclaw: 'StoreClaw',
  junglescout: 'Jungle Scout', zonguru: 'ZonGuru', amzscout: 'AMZScout',
  virallaunch: 'Viral Launch', sellerapp: 'SellerApp', sellersprite: 'SellerSprite',
  smartscout: 'SmartScout', datadive: 'Data Dive', copymonkey: 'CopyMonkey',
  sellesta: 'Sellesta', ecomtent: 'Ecomtent', epinium: 'Epinium',
  myamazonguy: 'My Amazon Guy', intentwise: 'Intentwise', keywordsam: 'Keywords.am',
};
const assetSiteLabel = (s) => ASSET_SITE[s] || (s ? s[0].toUpperCase() + s.slice(1) : '其它');
// 内容形式(kind)与渠道(channel)的展示标签 —— 供多维筛选
const ASSET_FORMS = [
  { k: 'article', label: '📄 文章' }, { k: 'image', label: '🖼 图文' },
  { k: 'text', label: '💬 文字' },   { k: 'video', label: '🎬 视频' },
  { k: 'podcast', label: '🎙 音频' }, { k: 'link', label: '🔗 链接' },
  { k: 'social', label: '👥 社媒' },
];
const ASSET_FORM_LABEL = Object.fromEntries(ASSET_FORMS.map((x) => [x.k, x.label]));
const ASSET_CH = { blog: '博客', youtube: 'YouTube', x: 'X', instagram: 'Instagram',
  facebook: 'Facebook', linkedin: 'LinkedIn', podcast: '播客', website: '官网',
  site: '官网', academy: '学院', resources: '资源' };
const ASSET_CH_ICON = { blog: '📝', youtube: '▶️', x: '🐦', instagram: '📸',
  facebook: '📘', linkedin: '💼', podcast: '🎙', website: '🌐', site: '🌐',
  academy: '🎓', resources: '📚' };
const assetChLabel = (c) => ASSET_CH[c] || c || '其它';
const assetChIcon = (c) => ASSET_CH_ICON[c] || '🌐';
function assetEmptyHint() {
  return '当前筛选没有素材。换个筛选,或去生成 / 上传 / 竞品调研。';
}

// 取某素材在某维度的值:source / form(内容形式) / brand(来源品牌) / channel(渠道)
function assetVal(a, dim) {
  if (dim === 'source') return a.source;
  if (dim === 'form') return a.kind || '';
  const m = a.meta || {};
  if (dim === 'brand') return m.site || '';
  if (dim === 'channel') return m.channel || (a.folder ? String(a.folder).split('/')[1] : '') || '';
  return '';
}
// 多维 AND 命中(可排除某一维,用于算该维的分面计数)
function assetHit(a, f, except) {
  for (const dim of ['source', 'form', 'brand', 'channel']) {
    if (dim === except) continue;
    const fv = f[dim];
    if (!fv || fv === 'all') continue;
    if (assetVal(a, dim) !== fv) return false;
  }
  return true;
}
function facetChip(dim, val, label, cur, count) {
  const act = { source: 'asset-src', form: 'asset-form', brand: 'asset-brand', channel: 'asset-chan' }[dim];
  const attr = { source: 'src', form: 'form', brand: 'brand', channel: 'chan' }[dim];
  return `<span class="chip ${cur === val ? 'on' : ''}" data-action="${act}" data-${attr}="${esc(val)}">${label}${count != null ? ' ' + count : ''}</span>`;
}

async function viewAssets() {
  S.assetFilter = S.assetFilter || { source: 'all', form: 'all', brand: 'all', channel: 'all' };
  const f = S.assetFilter;
  const all = await api.get(`/api/workspaces/${S.ws.id}/assets`);

  // 分面计数:某维计数 = 应用其余所有维筛选后、该维各取值的数量
  const cnt = (dim, val) => all.filter((a) => assetHit(a, f, dim) && assetVal(a, dim) === val).length;
  const cntAll = (dim) => all.filter((a) => assetHit(a, f, dim)).length;
  const present = (dim, pool) => [...new Set(pool.filter((a) => assetHit(a, f, dim)).map((a) => assetVal(a, dim)).filter(Boolean))];

  const items = all.filter((a) => assetHit(a, f, null));

  // Row1 来源
  const srcBar = ASSET_BUCKETS.map((b) =>
    facetChip('source', b.k, b.label, f.source, b.k === 'all' ? cntAll('source') : cnt('source', b.k))).join('');

  // Row2 内容形式(仅列当前来源下存在的形式)
  const formKeys = present('form', all).sort((x, y) =>
    ASSET_FORMS.findIndex((z) => z.k === x) - ASSET_FORMS.findIndex((z) => z.k === y));
  const formBar = formKeys.length > 1 ? `<div class="chips-row"><span class="chips-lbl">形式</span>
      ${facetChip('form', 'all', '全部', f.form, cntAll('form'))}
      ${formKeys.map((k) => facetChip('form', k, ASSET_FORM_LABEL[k] || k, f.form, cnt('form', k))).join('')}
    </div>` : '';

  // Row3/4 品牌 + 渠道(仅『从别处获取』时)
  let brandBar = '', chanBar = '';
  if (f.source === 'external') {
    const brands = present('brand', all).sort((x, y) => cnt('brand', y) - cnt('brand', x));
    const chans = present('channel', all).sort((x, y) => cnt('channel', y) - cnt('channel', x));
    if (brands.length) brandBar = `<div class="chips-row"><span class="chips-lbl">品牌</span>
        ${facetChip('brand', 'all', '全部', f.brand, cntAll('brand'))}
        ${brands.map((b) => facetChip('brand', b, esc(assetSiteLabel(b)), f.brand, cnt('brand', b))).join('')}</div>`;
    if (chans.length) chanBar = `<div class="chips-row"><span class="chips-lbl">渠道</span>
        ${facetChip('channel', 'all', '全部', f.channel, cntAll('channel'))}
        ${chans.map((c) => facetChip('channel', c, esc(assetChLabel(c)), f.channel, cnt('channel', c))).join('')}</div>`;
  }

  const grid = items.length ? `<div class="asset-grid">${items.map(assetCard).join('')}</div>`
    : `<div class="empty">${assetEmptyHint()}</div>`;

  main().innerHTML = `<div class="page-head"><div>
      <div class="page-title">Assets · 素材库</div>
      <div class="page-sub">按 来源 · 品牌 · 渠道 · 内容形式 多维筛选 · 共 ${items.length} 条</div></div>
      <div class="row"><button class="btn" data-action="asset-upload">${icon('image')} 上传素材</button></div></div>
    <div class="chips-row"><span class="chips-lbl">来源</span>${srcBar}</div>${formBar}${brandBar}${chanBar}
    <input id="assetUpload" type="file" accept="image/*" multiple style="display:none"/>
    ${grid}`;
  const inp = document.getElementById('assetUpload');
  if (inp) inp.addEventListener('change', onAssetUpload);
}

const ASSET_FORM_SHORT = { video: '视频', image: '图文', text: '文字', article: '文章', podcast: '音频', link: '链接', social: '社媒', svg: '海报' };
function assetCard(a) {
  const del = `<button class="asset-del" data-action="asset-del" data-id="${a.id}" title="删除">${icon('trash','ico-sm')}</button>`;
  if (a.source === 'external') {
    const m = a.meta || {};
    const href = safeHref(a.url);
    const thumb = safeHref(m.thumb);
    const tags = (m.tags || []).slice(0, 5).map((t) => `<span class="tag">${esc(t)}</span>`).join('');
    return `<div class="asset-card ext">
      <div class="asset-card-head">
        <span class="site-badge">${esc(assetSiteLabel(m.site))}</span>
        <span class="ch-badge">${assetChIcon(m.channel)} ${esc(assetChLabel(m.channel))}</span>
        <span class="form-tag">${esc(ASSET_FORM_SHORT[a.kind] || a.kind)}</span>${del}</div>
      ${thumb ? `<div class="poster-preview thumb"><img src="${esc(thumb)}" loading="lazy" onerror="this.parentNode.remove()"/></div>` : ''}
      <div class="asset-title">${esc(a.title || a.url)}</div>
      ${m.summary ? `<div class="page-sub asset-sum">${esc(m.summary)}</div>` : ''}
      ${tags ? `<div class="tags">${tags}</div>` : ''}
      ${href ? `<a class="asset-open" href="${esc(href)}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>` : ''}
    </div>`;
  }
  const badge = a.source === 'uploaded' ? '本地上传' : '历史发布';
  return `<div class="asset-card img">
    <div class="asset-card-head"><span class="site-badge muted">${badge}</span>${a.source === 'uploaded' ? del : ''}</div>
    ${a.url ? `<div class="poster-preview"><img src="${esc(a.url)}" loading="lazy"/></div>` : `<div class="asset-noimg">${icon('image','ico-lg')}</div>`}
    <div class="asset-title sm">${esc(a.title || a.kind)}</div></div>`;
}

async function onAssetUpload(e) {
  let ok = 0, failed = 0;
  for (const file of e.target.files) {
    const fd = new FormData(); fd.append('file', file);
    try {
      const res = await fetch(`/api/workspaces/${S.ws.id}/assets/upload`, { method: 'POST', body: fd });
      if (res.status === 401) { location.href = '/login'; return; }
      const r = await res.json().catch(() => ({}));
      if (res.ok && r.id) ok++; else { failed++; toast(`${file.name}: ${r.detail || '上传失败'}`); }
    } catch (err) { failed++; toast(`${file.name}: 网络错误`); }
  }
  if (ok) { toast(failed ? `已上传 ${ok} 张,${failed} 张失败` : `已上传 ${ok} 张到「本地上传」`); S.assetFilter = { source: 'uploaded', form: 'all', brand: 'all', channel: 'all' }; }
  viewAssets();
}

/* ---- 素材引用:在生成时从素材库检索/挑选/由 agent 推荐 ---- */
function assetRefLine(a) {
  const m = a.meta || {};
  if (a.source === 'external') {
    const who = `${assetSiteLabel(m.site)}·${assetChLabel(m.channel)}`;
    return `- [参考|${who}] ${a.title || ''}${m.summary ? ' — ' + m.summary : ''} ${a.url || ''}`.trim();
  }
  // 自有图/海报:注入可抓取的绝对图片链接(生成流水线会当配图素材解析)
  const u = a.url && a.url.startsWith('/') ? location.origin + a.url : a.url;
  return `- [配图] ${a.title || ''} ${u || ''}`.trim();
}
function injectRefs(targetId, assets) {
  const ta = document.getElementById(targetId); if (!ta || !assets.length) return;
  const block = '参考素材(供风格/角度/选题借鉴):\n' + assets.map(assetRefLine).join('\n');
  ta.value = (ta.value.trim() ? ta.value.trim() + '\n\n' : '') + block + '\n';
  ta.focus(); ta.dispatchEvent(new Event('input'));
}

async function openRefPicker(targetId) {
  const all = await api.get(`/api/workspaces/${S.ws.id}/assets`);
  S.refPick = { target: targetId || 'cvInput', all, sel: new Set(), q: '', source: 'external', form: 'all', channel: 'all' };
  refPickRender();
  document.getElementById('modal').classList.remove('hidden');
}
function refPickFiltered() {
  const p = S.refPick;
  const q = p.q.trim().toLowerCase();
  return p.all.filter((a) => {
    if (p.source !== 'all' && a.source !== p.source) return false;
    if (p.form !== 'all' && (a.kind || '') !== p.form) return false;
    if (p.channel !== 'all' && ((a.meta || {}).channel || '') !== p.channel) return false;
    if (q) { const hay = ((a.title || '') + ' ' + ((a.meta || {}).summary || '') + ' ' + ((a.meta || {}).tags || []).join(' ') + ' ' + ((a.meta || {}).site || '') + ' ' + (a.url || '')).toLowerCase(); if (!q.split(/\s+/).every((t) => hay.includes(t))) return false; }
    return true;
  });
}
function refPickRender() {
  const p = S.refPick;
  const srcs = [['external', '🌐 竞品'], ['uploaded', '⬆️ 上传'], ['published', '🗂 发布'], ['all', '全部']];
  const forms = [...new Set(p.all.filter((a) => p.source === 'all' || a.source === p.source).map((a) => a.kind).filter(Boolean))];
  const chans = [...new Set(p.all.filter((a) => (p.source === 'all' || a.source === p.source) && (a.meta || {}).channel).map((a) => a.meta.channel))];
  const items = refPickFiltered();
  const chip = (dim, val, label) => `<span class="chip ${p[dim] === val ? 'on' : ''}" data-action="ref-facet" data-dim="${dim}" data-val="${esc(val)}">${label}</span>`;
  const cards = items.slice(0, 200).map((a) => {
    const m = a.meta || {}; const on = p.sel.has(a.id);
    const meta = a.source === 'external' ? `${assetSiteLabel(m.site)} · ${assetChLabel(m.channel)}` : (a.source === 'uploaded' ? '本地上传' : '历史发布');
    return `<div class="ref-item ${on ? 'on' : ''}" data-action="ref-toggle" data-id="${a.id}">
      <div class="ref-check">${on ? '✓' : ''}</div>
      <div class="ref-body"><div class="ref-t">${esc(a.title || a.url)}</div>
        <div class="ref-m">${esc(ASSET_FORM_SHORT[a.kind] || a.kind || '')} · ${esc(meta)}</div>
        ${m.summary ? `<div class="ref-s">${esc(String(m.summary).slice(0, 80))}</div>` : ''}</div></div>`;
  }).join('') || '<div class="empty" style="margin:10px">没有匹配的素材,换个搜索/筛选。</div>';
  document.getElementById('modalBox').innerHTML = `
    <h3 style="margin:0 0 8px">📎 引用素材库 <span class="page-sub" style="font-weight:600">选中的素材会作为参考注入到生成</span></h3>
    <input id="refSearch" placeholder="🔍 搜索标题 / 摘要 / 标签 / 品牌…" value="${esc(p.q)}"
      oninput="S.refPick.q=this.value; refPickRender(); const el=document.getElementById('refSearch'); el.focus(); el.setSelectionRange(el.value.length,el.value.length);"
      style="width:100%;box-sizing:border-box;border:2px solid var(--line);border-radius:10px;padding:9px;margin-bottom:8px"/>
    <div class="chips-row" style="margin-bottom:6px"><span class="chips-lbl">来源</span>${srcs.map(([v, l]) => chip('source', v, l)).join('')}</div>
    ${forms.length ? `<div class="chips-row" style="margin-bottom:6px"><span class="chips-lbl">形式</span>${chip('form', 'all', '全部')}${forms.map((k) => chip('form', k, ASSET_FORM_SHORT[k] || k)).join('')}</div>` : ''}
    ${chans.length ? `<div class="chips-row" style="margin-bottom:6px"><span class="chips-lbl">渠道</span>${chip('channel', 'all', '全部')}${chans.map((c) => chip('channel', c, assetChLabel(c))).join('')}</div>` : ''}
    <div class="ref-grid">${cards}</div>
    <div class="row" style="margin-top:12px;align-items:center"><div class="page-sub">已选 ${p.sel.size} 条 · 命中 ${items.length}</div><div class="spacer"></div>
      <button class="btn" data-action="close-modal">取消</button>
      <button class="btn-primary" data-action="ref-confirm">引用选中 (${p.sel.size})</button></div>`;
}
function refConfirm() {
  const p = S.refPick;
  const chosen = p.all.filter((a) => p.sel.has(a.id));
  injectRefs(p.target, chosen);
  closeModal();
  toast(`已引用 ${chosen.length} 条素材到生成框`);
}

async function cvRecommend() {
  const ta = document.getElementById('cvInput');
  const text = (ta && ta.value || '').trim();
  if (text.length < 4) { toast('先贴一点素材/主题,我再按它推荐'); return; }
  const box = document.getElementById('cvSuggest'); if (box) box.innerHTML = '<span class="page-sub">💡 正在按你的素材找相关竞品参考…</span>';
  try {
    const r = await api.post(`/api/workspaces/${S.ws.id}/assets/recommend`, { text, limit: 6 });
    window._cvRecs = r.items || [];
    if (!window._cvRecs.length) { if (box) box.innerHTML = '<span class="page-sub">暂无高相关素材,可点「📎 引用素材库」自己挑。</span>'; return; }
    if (box) box.innerHTML = '<span class="chips-lbl" style="width:auto">💡 推荐参考</span>' + window._cvRecs.map((a, i) =>
      `<span class="qc" data-action="cv-ref-insert" data-i="${i}" title="${esc((a.meta || {}).summary || '')}">+ ${esc((assetSiteLabel((a.meta || {}).site)) + '·' + (ASSET_FORM_SHORT[a.kind] || a.kind))}:${esc(String(a.title || '').slice(0, 26))}</span>`).join('');
  } catch (e) { if (box) box.innerHTML = '<span class="page-sub">推荐失败,可稍后重试。</span>'; }
}
function cvRefInsert(i) {
  const a = (window._cvRecs || [])[+i]; if (!a) return;
  injectRefs('cvInput', [a]);
  const el = document.querySelector(`[data-action="cv-ref-insert"][data-i="${i}"]`); if (el) { el.classList.add('used'); el.textContent = '✓ 已加'; }
}

/* ---- Topics ---- */
async function viewTopics() {
  const topics = await api.get(`/api/workspaces/${S.ws.id}/topics`);
  const list = topics.length ? topics.map((t)=>`
    <div class="card post-item"><div class="body">
      <div class="row"><span class="chip">${esc(t.pillar||t.source)}</span><span class="page-sub">优先级 ${Math.round((t.priority||0)*100)}</span></div>
      <h4 style="margin-top:6px">${esc(t.title)}</h4>${t.angle?`<div class="page-sub">${esc(t.angle)}</div>`:''}</div>
      <div class="row" style="flex-direction:column;gap:6px">
        <button class="btn primary" data-action="topic-to-studio" data-title="${esc(t.title)}">去生成</button>
        <button class="btn danger" data-action="del-topic" data-id="${t.id}">删除</button></div></div>`).join('')
    : '<div class="empty">还没有话题。</div>';
  main().innerHTML = `<div class="page-head"><div><div class="page-title">Topics</div>
    <div class="page-sub">选题库 · 可来自 ② Ideation 或手动添加</div></div></div>
    <div class="card" style="margin-bottom:16px"><div class="row">
      <input id="t_title" class="field" style="flex:1" placeholder="新话题标题…"/>
      <input id="t_pillar" placeholder="支柱(可选)" style="border:2px solid var(--line);border-radius:10px;padding:9px"/>
      <button class="btn-primary" data-action="add-topic">添加</button></div></div>
    <div class="post-list">${list}</div>`;
}

/* ---- Settings(运营者一次性配置:驱动 + OAuth 应用凭据) ---- */
function envName(pid) {
  const m = { x: 'X_CLIENT_ID', linkedin: 'LINKEDIN_CLIENT_ID', reddit: 'REDDIT_CLIENT_ID',
    facebook: 'FACEBOOK_CLIENT_ID', instagram: 'FACEBOOK_CLIENT_ID (与 FB 同应用)',
    youtube: 'GOOGLE_CLIENT_ID', discord: 'DISCORD_CLIENT_ID' };
  return m[pid] || ('YOMINPOST_' + pid.toUpperCase() + '_CLIENT_ID');
}
function capSection(caps) {
  const groups = { text: '✍️ 文本生成', image: '🎨 图像生成', video: '🎬 视频生成' };
  return Object.entries(groups).map(([kind, label]) => {
    const items = caps.filter((c) => c.kind === kind).map((c) => `
      <label class="cp ${c.enabled && c.available ? 'on' : ''}" style="opacity:${c.available ? 1 : 0.55}">
        <input type="checkbox" data-action="cap-toggle" data-cap="${c.id}" ${c.enabled ? 'checked' : ''}
          ${c.available ? '' : ''} style="accent-color:var(--primary)"/>
        ${esc(c.label)} <span class="page-sub" style="font-size:11px">${c.available ? '可用' : esc(c.note)}</span>
      </label>`).join('');
    return `<div style="margin-bottom:10px"><b style="font-size:13px">${label}</b>
      <div class="channel-pick" style="margin-top:6px">${items}</div></div>`;
  }).join('');
}

async function viewSettings() {
  const apps = S.platforms.filter((p) => p.auth === 'oauth' && p.needs_app);
  const [statuses, caps] = await Promise.all([
    Promise.all(apps.map((p) => api.get(`/api/apps/${p.id}`))),
    api.get('/api/capabilities')]);
  const redirect = location.origin + '/oauth/callback';
  const cards = apps.map((p, i) => `
    <div class="card" style="margin-bottom:12px">
      <div class="row"><h4 style="margin:0">${pf(p.id,18)} ${esc(p.label)}</h4>
        ${statuses[i].configured ? '<span class="pill-real">已配置</span>' : '<span class="pill-mock">未配置</span>'}</div>
      <div class="page-sub" style="margin:6px 0">回调地址(登记到 ${esc(p.label)} 应用):
        <code>${esc(redirect)}</code> · 或设服务器变量 <code>${envName(p.id)}</code></div>
      <div class="row">
        <input id="app_${p.id}_id" placeholder="Client ID" style="flex:1;border:2px solid var(--line);border-radius:8px;padding:8px"/>
        <input id="app_${p.id}_secret" type="password" placeholder="Client Secret" style="flex:1;border:2px solid var(--line);border-radius:8px;padding:8px"/>
        <button class="btn-primary" data-action="save-app" data-platform="${p.id}">保存</button></div>
    </div>`).join('');
  main().innerHTML = `<div class="page-head"><div><div class="page-title">Settings</div>
    <div class="page-sub">生成驱动 + OAuth 应用(运营者配一次 → 之后所有人点 Connect 一键直达平台授权页)</div></div></div>
    ${driverBar()}
    <h3 style="margin:18px 0 8px">能力池(Create post 的规划 Loop 从这里选择驱动)</h3>
    <div class="card" style="margin-bottom:16px">${capSection(caps)}
      <div class="page-sub">灰色=未就绪(缺 CLI 或 API Key,如 <code>JIMENG_API_KEY</code>)。勾选=允许规划器使用。</div></div>
    <h3 style="margin:18px 0 8px">OAuth 应用凭据</h3>
    <div class="page-sub" style="margin-bottom:10px">在各平台开发者后台注册一个应用、登记上面的回调地址,填入 Client ID/Secret(或用环境变量)。</div>
    ${cards}
    <div class="card"><b>零配置平台</b><div class="page-sub" style="margin-top:6px">
      Mastodon(在该实例自助注册)、OAuth Demo(内置)无需任何配置 —— 直接到 Connections 点 Connect 即可授权。</div></div>`;
}
async function saveApp(platform) {
  const cid = document.getElementById(`app_${platform}_id`).value.trim();
  const csec = document.getElementById(`app_${platform}_secret`).value.trim();
  if (!cid) return toast('请填 Client ID');
  await api.post(`/api/apps/${platform}`, { client_id: cid, client_secret: csec });
  toast('已保存 ' + platform + ' 应用凭据'); viewSettings();
}

const VIEWS = {
  calendar: viewCalendar, scheduled: viewScheduled, studio: viewStudio, connections: viewConnections,
  assets: viewAssets, topics: viewTopics, settings: viewSettings, accounts: viewAccounts,
  'posts-all': () => gotoCalList('all'), 'posts-scheduled': viewScheduled,
  'posts-posted': () => gotoCalList('posted'), 'posts-draft': () => gotoCalList('draft'),
};

/* ---- 渠道管理:连接之后的一切 —— 平台原生健康(各平台真实机制)· 数据表现 · 运营 ·
   广告 · 定位/打法状态。与 Connections(只管链接)分工:这里是渠道视角的主场。 ---- */
async function viewAccounts() {
  if (!S.taxonomy) { try { S.taxonomy = await api.get('/api/channel-taxonomy'); } catch (e) { S.taxonomy = { positioning: {}, play_state: {} }; } }
  main().innerHTML = `<div class="page-head"><div><div class="page-title">渠道管理</div>
    <div class="page-sub">连接之后的渠道视角:平台原生健康(各平台真实机制)· 数据表现 · 运营 · 广告 · 定位与打法</div>
    <div class="page-sub" id="accSyncInfo" style="margin-top:2px"></div></div>
    <button class="btn primary" data-action="acc-sync">${icon('sync','ico-sm')} 立即同步</button></div>
    <div id="accBody"><div class="loading">加载中…</div></div>`;
  await accRender();
}
async function accRender() {
  const box = document.getElementById('accBody'); if (!box) return;
  let d; try { d = await api.get(`/api/workspaces/${S.ws.id}/operations`); }
  catch (e) { box.innerHTML = '<p style="color:#dc2626">加载失败</p>'; return; }
  const info = document.getElementById('accSyncInfo');
  if (info) info.textContent = (d.synced_at ? '上次同步 ' + localStamp(d.synced_at) : '尚未同步') + ' · 每天自动一次,可随时点「立即同步」';
  const r = d.rollup || {};
  const stat = (n, label, color) => `<div class="card" style="text-align:center;padding:12px 8px">
    <div style="font-size:24px;font-weight:900;color:${color||'#14121a'}">${n}</div>
    <div class="page-sub">${label}</div></div>`;
  const rows = (d.channels || []).map(accCardHtml).join('') || '<p class="page-sub">还没有连接的渠道,先去 Connections 连接。</p>';
  box.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(92px,1fr));gap:10px;margin-bottom:16px">
      ${stat(r.channels||0,'渠道')}${stat(r.healthy||0,'良好','#16a34a')}${stat(r.watch||0,'留意','#ca8a04')}
      ${stat(r.at_risk||0,'高风险','#ea580c')}${stat(r.blocked||0,'已封控','#dc2626')}
      ${stat(r.ad_linked||0,'广告已链接','#6d28d9')}${stat(r.total_posted_30d||0,'30天已发')}</div>
    <div style="display:flex;flex-direction:column;gap:12px">${rows}</div>`;
}
const GRADE_LABEL = { healthy: '良好', watch: '留意', at_risk: '高风险', blocked: '已封控' };
const SRC_BADGE = { api: ['#16a34a', '实时'], dashboard: ['#ca8a04', '需平台后台'], derived: ['#94a3b8', '代理指标'] };
/* 一个渠道 = 一整行管理面板,以「平台原生机制」为主角 */
function accCardHtml(c) {
  const h = c.health || {}; const grade = h.grade;
  const [color] = HEALTH_META[grade] || ['#94a3b8']; const label = GRADE_LABEL[grade] || '未评估';
  const cn = (c.publish?.counts) || {};
  const ads = c.ads || {};
  const perf = c.publish?.performance;
  const mech = h.mechanism || {}; const [sc, sl] = SRC_BADGE[h.data_source] || ['#94a3b8', ''];
  const metric = (v, lbl) => `<div style="min-width:70px"><div style="font-size:17px;font-weight:800">${v ?? '—'}</div><div class="page-sub">${lbl}</div></div>`;
  const issue = (c.alerts || []).length ? `
    <div style="margin-top:10px;background:#fef2f2;border:2px solid #dc2626;border-radius:10px;padding:10px 12px">
      <div style="font-weight:800;color:#dc2626">${icon('warning','ico-sm')} 渠道问题(非连接问题,需人工处理)</div>
      ${(c.alerts||[]).map((a)=>`<div style="font-size:12.5px;color:#7f1d1d;margin-top:4px">${esc(a)}</div>`).join('')}
      ${(h.actions||[]).length ? `<div style="font-weight:700;margin-top:6px;font-size:12.5px">建议动作</div><ul style="margin:2px 0 0;padding-left:18px;font-size:12.5px;color:#7f1d1d">${h.actions.map((a)=>`<li>${esc(a)}</li>`).join('')}</ul>` : ''}
    </div>` : '';
  const pos = S.taxonomy?.positioning || {}; const play = S.taxonomy?.play_state || {};
  const sel = (kind, cur, opts) => `<select data-action="ch-taxo" data-id="${esc(c.channel_id)}" data-kind="${kind}"
      style="border:2px solid var(--line);border-radius:8px;padding:4px 6px;font-size:12px">
      ${Object.entries(opts).map(([k,v])=>`<option value="${esc(k)}"${cur===k?' selected':''}>${esc(v)}</option>`).join('')}</select>`;
  return `<div class="card" style="padding:14px 16px">
    <div class="row" style="justify-content:space-between;align-items:flex-start">
      <div class="row" style="gap:10px;align-items:center">
        <div class="conn-icon" style="width:38px;height:38px;font-size:16px;background:${platColor(c.platform)}">${esc((c.label||'?')[0])}</div>
        <div><b style="font-size:15px">${esc(c.label)}</b> <span class="page-sub">@${esc(c.handle||'—')}</span>
          <div class="row" style="gap:6px;margin-top:2px">
            <span class="badge" style="border-color:${color};color:${color}">● ${esc(mech.term||'健康')} · ${label}</span>
            ${sl?`<span class="badge" style="border-color:${sc};color:${sc}">${sl}</span>`:''}</div></div>
      </div>
      <div class="row" style="gap:6px">
        <button class="btn" data-action="ch-profile" data-id="${esc(c.channel_id)}">刷新画像</button>
        ${ads.linkable ? `<button class="btn" data-action="ch-ads-link" data-id="${esc(c.channel_id)}" data-platform="${esc(c.platform)}">${ads.linked?'广告':'🎯 链接广告'}</button>` : ''}
      </div>
    </div>
    ${h.native_headline ? `<div style="margin-top:8px;font-size:13.5px"><b>${esc(mech.term||'状态')}:</b> ${esc(h.native_headline)}　<span class="page-sub">${esc(h.standing||'')}</span>${h.data_source==='dashboard'&&h.dashboard_hint?`　<span style="color:#ca8a04">· 详情见 ${esc(h.dashboard_hint)}</span>`:''}</div>` : ''}
    ${issue}
    <div class="row" style="gap:22px;margin-top:12px;flex-wrap:wrap;align-items:flex-start">
      <div style="min-width:170px"><div class="page-sub" style="font-weight:700;margin-bottom:4px">数据表现</div>
        <div class="row" style="gap:16px">${metric(h.followers,'粉丝')}${metric(h.media_count,'内容')}${metric(h.cadence_per_week,'周频')}${perf?metric(perf.likes,'赞'):''}${perf?metric(perf.impressions,'曝光'):''}</div>
        ${h.account_age_days!=null?`<div class="page-sub" style="margin-top:2px">可观测年龄 ${h.account_age_days} 天</div>`:''}</div>
      <div style="min-width:190px"><div class="page-sub" style="font-weight:700;margin-bottom:4px">运营</div>
        <div class="row" style="gap:16px">${metric(cn.posted||0,'已发')}${metric(cn.scheduled||0,'定时')}${metric(cn.failed||0,'失败')}${metric(cn.draft||0,'草稿')}</div>
        ${c.publish?.last_posted_at ? `<div class="page-sub" style="margin-top:2px">最近发布 ${esc((c.publish.last_posted_at||'').slice(0,10))}</div>` : ''}</div>
      <div style="min-width:140px"><div class="page-sub" style="font-weight:700;margin-bottom:4px">广告</div>
        <div style="font-size:13px">${ads.linked ? esc(ads.provider)+' · '+esc(ads.status) : (ads.linkable ? '可链接 '+esc(ads.provider||'') : '不支持')}</div></div>
      <div style="min-width:220px"><div class="page-sub" style="font-weight:700;margin-bottom:4px">定位 / 打法状态</div>
        <div class="row" style="gap:6px">${sel('positioning', c.positioning, pos)}${sel('play_state', c.play_state, play)}</div></div>
    </div>
    ${grade!=='blocked' && h.assessment ? `<div style="margin-top:10px;font-size:12.5px;background:#faf9f6;border:1.5px solid var(--line);border-radius:8px;padding:8px">${esc(h.assessment)}${(h.actions||[]).length?'　·　'+h.actions.map((a)=>esc(a)).join(' / '):''}</div>` : ''}
  </div>`;
}

/* ================= 组合器抽屉 ================= */
function openComposer(prefill) {
  const g = prefill || {};
  S.editingId = g.id || null;                 // 有 id = 编辑已存在帖子 → PATCH
  const _h2 = document.querySelector('#composer .drawer-head h2');   // 预览改过标题,恢复
  if (_h2) _h2.textContent = g.id ? '编辑帖子' : 'Create post';
  S.editTargets = (g.targets || []).map((t)=>t.channel_id);
  const chans = (S.channels.length ? S.channels : []).filter((c)=>c.status==='connected');
  // 若还没加载渠道,拉一次
  const build = (channels) => {
    const chips = channels.map((c)=>`<div class="cp" data-cid="${c.id}" data-platform="${c.platform}">${pf(c.platform)} ${esc(c.display_name||c.platform)}</div>`).join('') || '<span class="page-sub">无已连接渠道,请先到 Connections 连接。</span>';
    document.getElementById('composerBody').innerHTML = `
      <div class="field"><label>选题 / 标题</label><input id="c_topic" value="${esc(g.topic||g.title||'')}" placeholder="要发什么?"/></div>
      <div class="row">
        <button class="btn" data-action="c-generate">${icon('sparkles')} AI 生成内容</button>
        <label style="font-size:13px;font-weight:700"><input type="checkbox" id="c_opt" checked/> QA 优化</label>
        <span id="c_meta" class="page-sub"></span></div>
      <div class="field"><label>Hook 钩子</label><input id="c_hook" value="${esc(g.content?.hook||'')}"/></div>
      <div class="field"><label>正文</label><textarea id="c_body" style="min-height:120px">${esc(g.content?.body||'')}</textarea></div>
      <div class="field"><label>CTA</label><input id="c_cta" value="${esc(g.content?.cta||'')}"/></div>
      <div class="field"><label>Hashtags(空格分隔)</label><input id="c_tags" value="${esc((g.content?.hashtags||[]).join(' '))}"/></div>
      <div class="field"><label>发布渠道</label><div class="channel-pick" id="c_channels">${chips}</div></div>
      <div id="c_poster">${g.visual_url?`<div class="poster-preview"><img src="${g.visual_url}"/></div>`:''}</div>
      <div class="field"><label>图片(可多张,按选择顺序配图发布)</label>
        <input type="file" id="c_files" accept="image/*" multiple/>
        <div class="row" id="c_media" style="margin-top:6px;gap:6px;flex-wrap:wrap"></div></div>
      <div class="field"><label>定时(留空=立即)</label><input type="datetime-local" id="c_sched"/></div>
      <div class="row">
        <button class="btn" data-action="save-draft">存草稿</button>
        <button class="btn" data-action="schedule">排期</button>
        <div class="spacer"></div>
        <button class="btn-primary" data-action="publish">立即发布</button></div>`;
    if (g.format) document.getElementById('composerBody').dataset.format = g.format;
    // 存真实文件路径(visual_svg),不是展示 URL —— 否则发布时 os.path.exists 失败,平台丢图
    document.getElementById('composerBody').dataset.visual = g.visual_svg || '';
    document.getElementById('composerBody').dataset.video = g.video_srt || '';
    document.querySelectorAll('#c_channels .cp').forEach((cp)=>{
      if (S.editTargets.includes(cp.dataset.cid)) cp.classList.add('on');   // 预选已有渠道
      cp.addEventListener('click',()=>cp.classList.toggle('on'));
    });
    if (g.scheduled_at) {                       // 预填定时(UTC→本地 datetime-local)
      const el = document.getElementById('c_sched');
      if (el) el.value = isoToLocalInput(g.scheduled_at);
    }
    if (g.qa) document.getElementById('c_meta').textContent = `形式 ${g.format||''} · QA ${Math.round((g.qa.overall||0)*100)}`;
    S.composerMedia = (g.media||[]).map((p,i)=>({path:p, url:(g.media_urls&&g.media_urls[i])||''}));
    renderComposerMedia();
    document.getElementById('c_files').addEventListener('change', onComposerFiles);
  };
  document.getElementById('composer').classList.remove('hidden');
  if (S.channels.length) build(chans);
  else api.get(`/api/workspaces/${S.ws.id}/channels`).then((cs)=>{ S.channels=cs; build(cs.filter(c=>c.status==='connected')); });
}
const closeComposer = () => {
  document.getElementById('composer').classList.add('hidden');
  window._activePollRun = null;                         // 停掉正在跑的轮询(run 本身继续,可从历史恢复)
};

/* ===== 打开一条 post:按状态分流(已发→只读预览,草稿→编辑,失败→预览+重试)===== */
function openPostDetail(p) {
  const st = p.status;
  if (st === 'posted' || st === 'publishing') return openPreview(p);   // 已上线/在途 → 只读预览+数据
  if (st === 'failed') return openPreview(p, { failed: true });        // 失败 → 预览+错误+重试
  return openComposer(p);                                              // draft / scheduled → 可编辑
}

/* 只读预览面板(复用 #composer 抽屉外壳,不给编辑控件)——已发布/在途/失败帖用 */
function openPreview(p, opts = {}) {
  S.editingId = null;
  const drawer = document.getElementById('composer');
  const head = drawer.querySelector('.drawer-head h2');
  if (head) head.textContent = opts.failed ? '发布失败 · 详情' : '帖子预览 · 数据表现';
  const targets = p.targets || [];
  const badges = [
    `<span class="badge ${p.status}">${p.status}</span>`,
    p.synced_from ? `<span class="badge" title="从账号同步的真实已发帖">${icon('sync','ico-sm')} 已同步</span>` : '',
    targets.some((t) => t.sandbox) ? `<span class="badge" title="模拟渠道,未真实发布到平台">${icon('flask','ico-sm')} 模拟</span>` : '',
  ].filter(Boolean).join(' ');
  const media = (p.media_urls || []).length
    ? `<div class="row" style="flex-wrap:wrap;gap:8px">${p.media_urls.map((u) => `<img src="${esc(u)}" class="preview-media"/>`).join('')}</div>`
    : (p.visual_url ? `<div class="poster-preview"><img src="${esc(p.visual_url)}"/></div>` : '');
  const errBanner = opts.failed
    ? `<div class="preview-err"><b>${icon('warning')} 发布失败</b>
         <div class="page-sub">${esc((p.meta && (p.meta.error || p.meta.recovered)) || '部分渠道未发布成功,可重试(已发渠道不会重复发)')}</div>
         ${targets.filter((t) => t.error).map((t) => `<div style="margin-top:4px">· <b>${esc(t.platform)}</b>:${esc(friendlyPublishErr(t.error))}
           <details style="margin:2px 0 0 12px"><summary class="page-sub" style="cursor:pointer">原始错误</summary><code style="font-size:11px;word-break:break-all">${esc(t.error)}</code></details></div>`).join('')}
         <div class="page-sub" style="margin-top:6px">「编辑后重发」只投递失败渠道;已发成功的渠道保留原帖不重复发。</div>
         <button class="btn" style="margin-top:6px" data-action="go-connections">去 Connections 检查连接</button></div>`
    : '';
  const links = targets.filter((t) => safeHref(t.permalink))
    .map((t) => `<a class="tag" href="${esc(safeHref(t.permalink))}" target="_blank" rel="noopener noreferrer">🔗 在 ${esc(t.platform)} 查看</a>`).join(' ')
    || '<span class="page-sub">暂无平台链接(模拟/未回填)</span>';
  const when = p.posted_at ? localStamp(p.posted_at) : (p.scheduled_at ? '排期 ' + localStamp(p.scheduled_at) : '—');
  const withMetrics = targets.filter((t) => t.metrics && Object.keys(t.metrics).length);
  const metricsHtml = withMetrics.length
    ? withMetrics.map((t) => { const m = t.metrics; return `<div class="perf-row"><b>${esc(t.platform)}</b> 👁 ${m.impressions ?? '—'} · 📣 ${m.reach ?? '—'} · ❤ ${m.likes ?? '—'} · 💬 ${m.comments ?? '—'}</div>`; }).join('')
    : '<div class="page-sub">互动数据(曝光 / 触达 / 点赞)待接入平台 Insights;当前多为模拟或尚未采集。</div>';
  document.getElementById('composerBody').innerHTML = `
    <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">${badges}</div>
    ${errBanner}
    <div class="field"><label>成品文案</label><div class="preview-text">${esc(p.text || '')}</div></div>
    ${media ? `<div class="field"><label>配图</label>${media}</div>` : ''}
    <div class="field"><label>数据表现</label>
      <div class="perf-box">
        <div class="perf-row">发布时间:${esc(when)} · 渠道:${targets.length}</div>
        <div class="perf-row">${links}</div>
        ${metricsHtml}
      </div></div>
    <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:6px">
      ${opts.failed ? `<button class="btn primary" data-action="retry-post" data-id="${p.id}">重试发布</button>
      <button class="btn" data-action="edit-post" data-id="${p.id}">编辑后重发</button>` : ''}
      <button class="btn" data-action="copy-as-draft" data-id="${p.id}">📄 复制为草稿</button>
      ${(!p.synced_from && p.status !== 'publishing') ? `<button class="btn danger" data-action="del-post" data-id="${p.id}">删除</button>` : ''}
    </div>`;
  drawer.classList.remove('hidden');
}

function renderComposerMedia() {
  const el = document.getElementById('c_media'); if (!el) return;
  el.innerHTML = (S.composerMedia||[]).map((m,i)=>`<div style="position:relative;display:inline-block">
    <img src="${m.url}" style="height:64px;border:2px solid var(--line);border-radius:8px;display:block"/>
    <span data-action="rm-media" data-i="${i}" style="position:absolute;top:-8px;right:-8px;background:#ef4444;color:#fff;border-radius:50%;width:20px;height:20px;line-height:18px;text-align:center;cursor:pointer;font-weight:800;border:2px solid #14121a">×</span>
    <span style="position:absolute;bottom:2px;left:2px;background:#14121a;color:#fff;font-size:10px;padding:0 4px;border-radius:4px">${i+1}</span></div>`).join('');
}
async function onComposerFiles(e) {
  let ok = 0, failed = 0;
  for (const f of e.target.files) {
    const fd = new FormData(); fd.append('file', f);
    try {
      const res = await fetch(`/api/workspaces/${S.ws.id}/assets/upload`, {method:'POST', body:fd});
      if (res.status === 401) { location.href = '/login'; return; }
      const r = await res.json().catch(() => ({}));
      if (res.ok && r.path) { S.composerMedia.push({path:r.path, url:r.url}); ok++; }
      else { failed++; toast(`${f.name}: ${r.detail || r.error || '上传失败'}`); }
    } catch (err) { failed++; toast(`${f.name}: 网络错误`); }
  }
  e.target.value = ''; renderComposerMedia();
  if (ok) toast(failed ? `已上传 ${ok} 张,${failed} 张失败` : `已上传 ${ok} 张图片`);
}

function composerCollect() {
  const body = document.getElementById('composerBody');
  const tags = document.getElementById('c_tags').value.split(/\s+/).filter(Boolean);
  const channel_ids = [...document.querySelectorAll('#c_channels .cp.on')].map((c)=>c.dataset.cid);
  const schedRaw = document.getElementById('c_sched').value;
  const scheduled_at = schedRaw ? new Date(schedRaw).toISOString() : '';
  return {
    title: document.getElementById('c_topic').value || document.getElementById('c_hook').value.slice(0,40),
    content: { hook: document.getElementById('c_hook').value, body: document.getElementById('c_body').value,
               cta: document.getElementById('c_cta').value, hashtags: tags, segments: [] },
    format: body.dataset.format || 'text', channel_ids, scheduled_at,
    visual_svg: body.dataset.visual || '', video_srt: body.dataset.video || '',
    media: (S.composerMedia||[]).map((m)=>m.path),
  };
}

async function composerGenerate() {
  const topic = document.getElementById('c_topic').value.trim(); if (!topic) return toast('先填选题');
  const on = [...document.querySelectorAll('#c_channels .cp.on')];
  const platform = on.length ? on[0].dataset.platform : 'x';
  document.getElementById('c_meta').textContent = '生成中…';
  const r = await api.post(`/api/workspaces/${S.ws.id}/generate`, { topic, platform, optimize: document.getElementById('c_opt').checked });
  document.getElementById('c_hook').value = r.content.hook || '';
  document.getElementById('c_body').value = r.content.body || '';
  document.getElementById('c_cta').value = r.content.cta || '';
  document.getElementById('c_tags').value = (r.content.hashtags||[]).join(' ');
  const body = document.getElementById('composerBody');
  body.dataset.format = r.format || 'text'; body.dataset.visual = r.visual_svg || ''; body.dataset.video = r.video_srt || '';
  document.getElementById('c_poster').innerHTML = r.visual_url ? `<div class="poster-preview"><img src="${r.visual_url}"/></div>` : '';
  document.getElementById('c_meta').textContent = `形式 ${r.format} · QA ${Math.round((r.qa.overall||0)*100)} ${r.qa.revised?'· 已重写':''}`;
  toast('已生成');
}

async function savePost(mode) {
  if (S._savingPost) return;                  // 防双击:新建帖时两次 POST 会建两条并各自真发一遍
  const data = composerCollect();
  if ((mode === 'schedule') && !data.scheduled_at) return toast('请选择定时时间');
  if (mode === 'draft') data.scheduled_at = '';
  if (mode === 'publish' && !data.channel_ids.length) return toast('请先选择渠道');
  S._savingPost = true;
  const btns = document.querySelectorAll('#composerBody [data-action="save-draft"],'
    + '#composerBody [data-action="schedule"],#composerBody [data-action="publish"]');
  btns.forEach((b) => (b.disabled = true));
  try {
    // 编辑已有帖子 → PATCH(避免重复创建);否则 POST 新建
    const post = S.editingId
      ? await api.patch(`/api/posts/${S.editingId}`, data)
      : await api.post(`/api/workspaces/${S.ws.id}/posts`, data);
    if (mode === 'publish') {
      if (!(await riskPreflight(post.id))) { S.editingId = null; return; }
      const pub = await api.post(`/api/posts/${post.id}/publish`, {});
      toast(pub.status === 'posted' ? '已发布 ✅' : '发布失败:' + (pub.targets?.[0]?.error||''));
    } else { toast(mode === 'schedule' ? '已排期' : (S.editingId ? '已更新' : '已存草稿')); }
    S.editingId = null;
    closeComposer(); go(S.view);
  } catch (e) {
    btns.forEach((b) => (b.disabled = false));   // 失败留在原地可重试
  } finally {
    S._savingPost = false;
  }
}

/* ================= 连接渠道模态 ================= */
const closeModal = () => document.getElementById('modal').classList.add('hidden');

/* 账号健康画像:刷新(LLM 叙述)并展示评分明细 + 建议动作 */
async function chProfile(chId) {
  document.getElementById('modalBox').innerHTML = '<h3>渠道健康画像</h3><p class="loading">刷新中…(含 LLM 叙述,稍候)</p>';
  document.getElementById('modal').classList.remove('hidden');
  let p; try { p = await api.post(`/api/channels/${chId}/profile/refresh`, {}); }
  catch (e) { document.getElementById('modalBox').innerHTML = '<h3>渠道健康画像</h3><p style="color:#dc2626">刷新失败</p><div class="row"><div class="spacer"></div><button class="btn" data-action="close-modal">关闭</button></div>'; return; }
  const [color] = HEALTH_META[p.health_grade] || ['#94a3b8']; const label = GRADE_LABEL[p.health_grade] || p.health_grade;
  const s = p.signals || {}; const mech = p.mechanism || {}; const [sc, sl] = SRC_BADGE[p.data_source] || ['#94a3b8', ''];
  // 平台原生因子明细(不是通用扣分)
  const bd = (p.score_breakdown || []).map((b) => `<tr><td style="white-space:nowrap;color:#7c3aed">${esc(b.factor)}</td><td class="page-sub">${esc(b.why)}</td></tr>`).join('');
  const acts = (p.recommended_actions || []).map((a) => `<li>${esc(a)}</li>`).join('');
  document.getElementById('modalBox').innerHTML = `
    <h3>渠道健康 <span class="badge" style="border-color:${color};color:${color}">● ${esc(mech.term||'健康')} · ${label}</span>
      ${sl?`<span class="badge" style="border-color:${sc};color:${sc}">${sl}</span>`:''}</h3>
    <div class="page-sub" style="margin:2px 0 8px">机制:${esc(mech.en||mech.term||'')} · 引擎:信号=脚本 · 判定=平台原生 · 叙述=${esc(p.engine?.assessment||'rule')} · ${esc((p.checked_at||'').slice(0,16))}</div>
    <div style="background:#f5f3ff;border:2px solid #7c3aed;border-radius:10px;padding:10px;margin-bottom:8px">
      <b>${esc(mech.term||'状态')}:</b> ${esc(p.native_headline||'—')}　<span class="page-sub">${esc(p.standing||'')}</span>
      ${p.data_source==='dashboard'&&p.dashboard_hint?`<div style="color:#ca8a04;font-size:12px;margin-top:4px">该平台原生状态无 API,请到后台查看:${esc(p.dashboard_hint)}</div>`:''}</div>
    <div style="background:#faf9f6;border:2px solid var(--line);border-radius:10px;padding:10px;margin-bottom:8px">${esc(p.assessment||'')}</div>
    <div class="page-sub">数据:粉丝 ${s.followers ?? '—'} · 内容 ${s.media_count ?? '—'} · 可观测年龄 ${s.account_age_days ?? '—'}天 · 30天发 ${s.posts_30d ?? 0}(周频 ${s.cadence_per_week ?? 0})</div>
    ${bd ? `<div style="font-weight:800;margin-top:8px;font-size:13px">平台原生因子</div><table style="width:100%;margin-top:4px;border-collapse:collapse;font-size:12px">${bd}</table>` : ''}
    ${acts ? `<div style="font-weight:800;margin-top:8px">建议动作</div><ul style="margin:4px 0 0;padding-left:18px;font-size:13px">${acts}</ul>` : ''}
    <div class="row" style="margin-top:14px"><div class="spacer"></div><button class="btn" data-action="close-modal">关闭</button></div>`;
  if (S.view === 'connections') viewConnections(); else if (S.view === 'accounts') accRender();
}

/* 链接广告账户(instagram/facebook → meta_ads;x → x_ads) */
async function chAdsLink(chId, platform) {
  const prov = platform === 'x' ? 'x_ads' : 'meta_ads';
  const hint = prov === 'meta_ads' ? 'act_<广告账户ID>(Instagram+Facebook 共用一个 Meta 广告账户)' : 'X Ads 账户 ID';
  document.getElementById('modalBox').innerHTML = `
    <h3>链接广告账户 · ${esc(prov)}</h3>
    <div class="page-sub" style="margin-bottom:8px">把该社媒账号接到它的广告账户,打通 AIGC → 投放(GTM)。真实报表需对应广告 API 权限,未就绪会诚实显示 pending。</div>
    <div class="field"><label>广告账户 ID</label><input id="adAcctId" placeholder="${esc(hint)}"/></div>
    <div id="adLinkMsg" class="page-sub"></div>
    <div class="row" style="margin-top:14px"><div class="spacer"></div>
      <button class="btn" data-action="close-modal">取消</button>
      <button class="btn-primary" data-action="submit-ads-link" data-id="${esc(chId)}" data-platform="${esc(platform)}">链接</button></div>`;
  document.getElementById('modal').classList.remove('hidden');
}
async function submitAdsLink(chId) {
  const v = (document.getElementById('adAcctId') || {}).value || '';
  const msg = document.getElementById('adLinkMsg');
  if (!v.trim()) { if (msg) msg.textContent = '请填写广告账户 ID'; return; }
  if (msg) msg.textContent = '链接中…';
  try {
    const st = await api.post(`/api/channels/${chId}/ads/link`, { ad_account_id: v.trim() });
    toast(st.status === 'connected' ? '广告账户已链接 ✅' : '已链接(状态:' + (st.status || '') + ')');
    if (msg) msg.innerHTML = st.status === 'connected' ? '✅ 已连接' : '⏳ ' + esc(st.reason || st.status || '');
    closeModal();
    if (S.view === 'connections') viewConnections(); else if (S.view === 'accounts') accRender();
  } catch (e) { if (msg) msg.textContent = '链接失败:' + (e.message || ''); }
}

function openConnectModal(platform) {
  const p = S.platMap[platform];
  const fields = (p.connect_fields || []).map((f) => `
    <div class="field"><label>${esc(f.label)}</label>
      <input id="cf_${f.key}" type="${f.type==='password'?'password':'text'}" placeholder="${esc(f.placeholder||'')}"/></div>`).join('');
  const hint = p.real
    ? `<div class="page-sub" style="margin-bottom:8px">${icon('check','ico-sm')} 真实平台:填入下方凭据后会真实调用 ${esc(p.label)} API 校验并发布。</div>`
    : `<div class="page-sub" style="margin-bottom:8px">该平台需 OAuth,当前用模拟连接打通全链路(可换真实连接器)。</div>`;
  document.getElementById('modalBox').innerHTML = `
    <h3>连接 ${esc(p.label)} ${p.real?'<span class="pill-real">真实可发</span>':'<span class="pill-mock">模拟</span>'}</h3>
    ${hint}
    <div class="field"><label>显示名</label><input id="cf_display" placeholder="${esc(p.label)} 账号" value="${esc(p.label)}"/></div>
    ${fields}
    <div class="row" style="margin-top:14px"><div class="spacer"></div>
      <button class="btn" data-action="close-modal">取消</button>
      <button class="btn-primary" data-action="submit-connect" data-platform="${platform}">连接</button></div>`;
  document.getElementById('modal').classList.remove('hidden');
}
async function submitConnect(platform) {
  const p = S.platMap[platform];
  const cfg = {};
  (p.connect_fields || []).forEach((f) => { cfg[f.key] = (document.getElementById('cf_'+f.key)||{}).value || ''; });
  const display = document.getElementById('cf_display').value || p.label;
  const ch = await api.post(`/api/workspaces/${S.ws.id}/channels/connect`,
    { platform, display_name: display, config: cfg });
  closeModal();
  if (ch.status === 'connected') toast('已连接 ' + p.label);
  else toast('连接失败:' + (ch.config?.error || '凭据无效'));
  viewConnections();
}

/* ================= Create Post · Canvas + Chatbot（左对话 / 右每渠道多模态卡片）===== */
const FMT_LABEL = { text: '文字', image_text: '图文', short_video: '短视频' };

const CV_MAX_PARALLEL = 3;   // 后端 MAX_RUNNING_COMPOSE=3

async function openCanvas() {
  const [channels, caps] = await Promise.all([
    api.get(`/api/workspaces/${S.ws.id}/channels`), api.get('/api/capabilities').catch(() => [])]);
  S.channels = channels;
  S.caps = caps;
  S.cv = { status: 'idle', started: false, cards: {}, setup: {}, focus: null,
           view: { x: 20, y: 20, scale: 1 }, analysisShown: false };
  document.getElementById('canvas').classList.add('show');
  document.getElementById('canvas').classList.remove('show-board');
  document.getElementById('cvBoardToggle').style.display = 'none';
  const inp = document.getElementById('cvInput');
  inp.value = '';
  inp.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); cvSend(); } };
  cvWorldEl().innerHTML = '';
  document.getElementById('cvSetup').style.display = '';   // 重开时恢复设置区
  document.getElementById('cvSuggest').innerHTML = '';
  cvApplyView();
  cvBindViewport();
  cvRenderFocus();
  document.getElementById('cvBoardHead').innerHTML = `<div class="page-sub">选好渠道、贴上素材,点“发送”开始 · 画布可拖拽平移、滚轮缩放、拖卡片标题移动</div>`;
  cvRenderSetup();
  const conn = channels.filter((c) => c.status === 'connected').length;
  cvLog([]);
  cvBot(`嗨,我是你的内容策略助手。贴一段素材(文字 / 链接 / 图片URL 混合都行),选好目标渠道,我会为每个渠道<b>并发</b>跑一遍 <b>剖析→平台调研→策略→生成</b>,在右侧无限画布上产出每渠道一张多模态卡片。\n生成后可拖动卡片排布,或点某张卡“◎ 针对这条优化”让我只改它。${conn ? '' : '\n\n⚠️ 你还没有已连接的渠道,先去 Connections 连接。'}`);
}

function cvClose() {
  document.getElementById('canvas').classList.remove('show');
  if (S.cv) S.cv.status = 'closed';
}

/* ---- 无限画布:平移 / 缩放 / 拖拽 ---- */
function cvWorldEl() { return document.getElementById('cvWorld'); }
function cvApplyView() {
  const v = S.cv.view; const w = cvWorldEl();
  if (w) w.style.transform = `translate(${v.x}px,${v.y}px) scale(${v.scale})`;
  const lbl = document.getElementById('cvZoomLabel'); if (lbl) lbl.textContent = Math.round(v.scale * 100) + '%';
}
function cvZoomAt(cx, cy, factor) {
  const v = S.cv.view; const s2 = Math.max(0.3, Math.min(2, v.scale * factor));
  const wx = (cx - v.x) / v.scale, wy = (cy - v.y) / v.scale;
  v.scale = s2; v.x = cx - wx * s2; v.y = cy - wy * s2; cvApplyView();
}
function cvZoom(factor) { const vp = document.getElementById('cvViewport'); const r = vp.getBoundingClientRect(); cvZoomAt(r.width / 2, r.height / 2, factor); }
function cvZoomFit() { S.cv.view = { x: 20, y: 20, scale: 1 }; cvApplyView(); }
function cvBindViewport() {
  const vp = document.getElementById('cvViewport');
  vp.onmousedown = (e) => {
    if (e.target.closest('.cv-card')) return;    // 卡片拖拽自己处理
    const sx = e.clientX, sy = e.clientY, ox = S.cv.view.x, oy = S.cv.view.y;
    vp.classList.add('panning'); e.preventDefault();
    const mv = (ev) => { S.cv.view.x = ox + (ev.clientX - sx); S.cv.view.y = oy + (ev.clientY - sy); cvApplyView(); };
    const up = () => { vp.classList.remove('panning'); window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
  };
  vp.onwheel = (e) => { e.preventDefault(); const r = vp.getBoundingClientRect(); cvZoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12); };
}
function cvNextPos() {
  const n = Object.values(S.cv.cards).filter((c) => c.pos).length;
  const col = n % 3, row = Math.floor(n / 3);
  return { x: 30 + col * 400, y: 70 + row * 540 + col * 26 };
}

function cvRenderSetup() {
  const conn = (S.channels || []).filter((c) => c.status === 'connected');
  const chips = conn.map((c) => {
    const lang = (c.language && c.language !== 'auto') ? ` <b style="font-size:10px;opacity:.7">${c.language.toUpperCase()}</b>` : '';
    return `<div class="cp" data-action="cv-chan" data-cid="${c.id}" data-platform="${c.platform}">${pf(c.platform)} ${esc(c.handle || c.platform)}${lang}</div>`;
  }).join('') || '<span class="page-sub">无已连接渠道</span>';
  const capChips = (S.caps || []).filter((c) => c.kind === 'image' && c.enabled && c.available)
    .map((c) => `<span class="tag" title="${esc(c.note || '')}">🎨 ${esc(c.label)}</span>`).join('');
  const nSel = conn.length;
  document.getElementById('cvSetup').innerHTML = `
    <details class="cv-setup-fold" open>
      <summary>${icon('gear')} 渠道 · 形式 · 语言 · 生图 <span class="page-sub" style="font-weight:600;font-size:11px">(${nSel} 个已连接渠道)</span></summary>
      <div class="cv-setup-inner">
        <div class="cv-field"><label>目标渠道(可多选)</label>
          <div class="channel-pick" style="display:flex;flex-wrap:wrap;gap:6px">${chips}</div></div>
        <div class="row" style="flex-wrap:wrap;gap:10px">
          <label style="font-size:12px;font-weight:800">形式
            <select id="cvFmt" style="border:2px solid var(--line);border-radius:8px;padding:4px">
              <option value="auto">自动</option><option value="text">文字</option>
              <option value="image_text">图文</option><option value="video">短视频</option></select></label>
          <label style="font-size:12px;font-weight:800">语言
            <select id="cvLang" style="border:2px solid var(--line);border-radius:8px;padding:4px">
              <option value="auto">自动</option><option value="en">EN</option><option value="zh">中文</option></select></label>
        </div>
        ${capChips ? `<div class="page-sub" style="font-size:11px">生图:${capChips} <a class="tag" data-action="cv-goto-settings" style="cursor:pointer">${icon('gear','ico-sm')}</a></div>` : ''}
        <div class="row" style="gap:8px;margin-top:2px">
          <button class="btn" data-action="ref-open" title="从素材库检索并挑选素材,注入到本次生成作参考">📎 引用素材库</button>
          <button class="btn" data-action="cv-recommend" title="按你已贴的素材,让 agent 推荐相关竞品参考">💡 智能推荐</button>
        </div>
      </div>
    </details>`;
}

const CV_QUICK = ['更口语化', '更专业有说服力', '加数据/案例支撑', '换个角度', '再短一点', '更强的钩子'];
function cvRenderSuggest() {
  const box = document.getElementById('cvSuggest'); if (!box) return;
  if (!S.cv.started) { box.innerHTML = ''; return; }
  const cmds = CV_QUICK.map((t) => `<span class="qc" data-action="cv-qc" data-text="${esc(t)}">${esc(t)}</span>`).join('');
  // 未加入本次看板的已连接渠道 → 一键加渠道
  const inBoard = new Set(Object.keys(S.cv.cards || {}));
  const add = (S.channels || []).filter((c) => c.status === 'connected' && !inBoard.has(c.id))
    .map((c) => `<span class="qc add" data-action="cv-addchan" data-cid="${c.id}">+ ${esc(S.platMap[c.platform] ? S.platMap[c.platform].label : c.platform)}</span>`).join('');
  box.innerHTML = cmds + add;
}

function cvLog(msgs) { document.getElementById('cvLog').innerHTML = ''; (msgs || []).forEach((m) => cvMsg(m.role, m.text)); }
function cvMsg(role, html) {
  const log = document.getElementById('cvLog');
  const d = document.createElement('div');
  d.className = 'cv-msg ' + role;
  d.innerHTML = html;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  return d;
}
const cvBot = (h) => cvMsg('bot', h);
const cvUser = (h) => cvMsg('user', esc(h));
const cvSys = (h) => cvMsg('sys', h);

function cvSelectedChannels() {
  return [...document.querySelectorAll('#cvSetup .cp.on')].map((c) => ({ id: c.dataset.cid, platform: c.dataset.platform }));
}

function cvBusy() { return Object.values(S.cv.cards || {}).some((c) => c.status === 'gen'); }
function cvSyncSendState() {
  const btn = document.getElementById('cvSend'); if (!btn) return;
  const busy = cvBusy();
  btn.disabled = busy;
  btn.textContent = busy ? '生成中…' : '发送';
}

async function cvSend() {
  const input = document.getElementById('cvInput');
  const text = input.value.trim();
  // 生成中禁止再发:否则并发 >3 触发 429,会把已生成的好稿冲成红色错误卡
  if (S.cv.started && cvBusy()) return toast('有渠道正在生成,请等它完成再调整');
  if (!S.cv.started) {
    const chans = cvSelectedChannels();
    if (!text) return toast('先贴一段素材');
    if (!chans.length) return toast('先选至少一个渠道');
    input.value = '';
    cvUser(text);
    S.cv.setup = {
      source: text, notes: '',
      target_format: (document.getElementById('cvFmt') || {}).value || 'auto',
      language: (document.getElementById('cvLang') || {}).value || 'auto',
    };
    S.cv.started = true;
    document.querySelector('#cvSetup .cv-setup-fold')?.removeAttribute('open');  // 折起而非隐藏,生成后仍可展开加渠道
    document.getElementById('cvBoardToggle').style.display = '';
    input.placeholder = '说怎么改;或点某张卡“◎ 针对这条优化”只改它';
    cvBot(`开始为 <b>${chans.length}</b> 个渠道<b>并发</b>生成(最多 ${CV_MAX_PARALLEL} 路并行)…`);
    await cvGenerate(chans.map((c) => c.id), '');
  } else if (S.cv.focus && S.cv.cards[S.cv.focus]) {
    if (!text) return;
    input.value = '';
    cvUser(text);
    const c = S.cv.cards[S.cv.focus];
    cvBot(`按“${esc(text)}”优化 ${esc(c.handle || c.platform)} 这一条。`);
    await cvRunOne(S.cv.focus, text);
  } else {
    if (!text) return;
    input.value = '';
    cvUser(text);
    await cvHandleAdjust(text);
  }
}

/* 并发池:每渠道一个独立单渠道 run,最多 CV_MAX_PARALLEL 路同时 */
async function cvGenerate(channelIds, notes) {
  let idx = 0;
  const worker = async () => {
    while (idx < channelIds.length) { const cid = channelIds[idx++]; await cvRunOne(cid, notes); }
  };
  await Promise.all(Array.from({ length: Math.min(CV_MAX_PARALLEL, channelIds.length) }, worker));
}

const CV_STEPS = { queued: '排队', analyze: '① 剖析素材', research: '② 平台调研', strategy: '③ 收敛策略', generate: '④ 生成+QA', done: '完成' };

async function cvRunOne(cid, notes) {
  const ch = (S.channels || []).find((c) => c.id === cid) || {};
  let card = S.cv.cards[cid];
  if (!card) card = S.cv.cards[cid] = { channel_id: cid, platform: ch.platform, handle: ch.display_name || ch.platform };
  if (!card.pos) card.pos = cvNextPos();
  card.status = 'gen'; card.progress = { step: 'queued', pct: 0 }; card.errMsg = '';
  cvUpdateCard(cid);
  try {
    const { run_id } = await api.post(`/api/workspaces/${S.ws.id}/compose/start`, {
      source: S.cv.setup.source,
      notes: [S.cv.setup.notes, notes].filter(Boolean).join('；'),
      channel_ids: [cid], target_format: S.cv.setup.target_format,
      language: S.cv.setup.language, loop: true });
    card.runId = run_id;
    await cvPollOne(cid, run_id);
  } catch (e) {
    card.status = 'err';
    card.errMsg = (e && e.status === 429) ? '并发已满(最多3),稍后重试' : '启动失败';
    cvUpdateCard(cid);
  }
}

async function cvPollOne(cid, runId) {
  for (let i = 0; i < 400; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    if (!S.cv || S.cv.status === 'closed') return;
    const card = S.cv.cards[cid];
    if (!card || card.runId !== runId) return;         // 已被更新的 run 取代
    let r;
    try { r = await api.get(`/api/compose-runs/${runId}`); } catch (e) { continue; }
    if (r.status === 'running') { card.progress = r.progress || {}; cvUpdateCard(cid); continue; }
    if (r.status === 'failed') { card.status = 'err'; card.errMsg = r.error || '生成失败'; cvUpdateCard(cid); return; }
    const d = (r.drafts || []).find((x) => x.channel_id === cid) || (r.drafts || [])[0];
    if (d) { _enrichDraftUrls(d); card.status = 'done'; card.draft = d; }
    else { card.status = 'err'; card.errMsg = '无产出'; }
    cvUpdateCard(cid);
    if (d) cvPersistDraft(cid);      // 生成即落成 draft post → 立刻出现在日历/列表(未确认草稿)
    if (d && !S.cv.analysisShown && r.analysis && r.analysis.subject) {
      S.cv.analysisShown = true;
      cvBot(`🔍 素材剖析:${esc(r.analysis.subject)}${r.analysis.pain_points && r.analysis.pain_points.length ? '<br>痛点:' + esc(String(r.analysis.pain_points[0]).slice(0, 44)) : ''}`);
    }
    if (d) cvSys(`✅ ${esc(card.platform)} 完成 · QA ${Math.round(((d.qa && d.qa.overall) || 0) * 100)}${d.fallback ? ' · ⚠️模板' : ''}`);
    return;
  }
  const card = S.cv.cards[cid]; if (card) { card.status = 'err'; card.errMsg = '超时'; cvUpdateCard(cid); }
}

function _enrichDraftUrls(d) { d.visual_url = d.ext_image || d.visual_url || ''; return d; }

/* 生成完成即把草稿落成 draft post（每卡一渠道一 post；重生成走 PATCH 去重，不新建）*/
function _cvDraftPayload(c) {
  const d = c.draft || {}; const ct = d.content || {};
  return {
    title: (ct.hook || '').slice(0, 40) || '组合稿',
    content: { hook: ct.hook || '', body: ct.body || '', cta: ct.cta || '',
               hashtags: ct.hashtags || [], segments: ct.segments || [] },
    format: d.format || 'text', channel_ids: [c.channel_id],
    visual_svg: (d.visual_url && !d.ext_image) ? (d.visual_svg || '') : '',
    media: d.ext_image ? [d.ext_image] : [] };
}
/* 稳定幂等键:重开/刷新画布会丢掉 c.postId(S.cv.cards 每次重置),
   靠 (渠道 + 素材指纹) 让服务端对同素材同渠道的自动草稿去重,不新建重复帖。
   指纹只取素材(每次生成的正文都不同,不能用正文/run_id 做键)。*/
function _cvHash32(s) {
  let h = 2166136261 >>> 0;               // FNV-1a
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return (h >>> 0).toString(36);
}
function cvDedupeKey(cid) {
  const src = (S.cv && S.cv.setup && S.cv.setup.source) || '';
  return `cv:${cid}:${_cvHash32(src)}`;
}
async function cvPersistDraft(cid) {
  const c = S.cv && S.cv.cards[cid]; if (!c || !c.draft) return;
  // 只对「仍是未确认草稿」的卡自动落盘:已排期/已发/发布中的卡绝不自动改写
  //（否则重生成会 PATCH scheduled_at:'' 把已排期帖静默降级为 draft、或覆盖已发布正文)。
  if (c.postStatus && c.postStatus !== 'draft') return;
  const run = (async () => {
    try {
      if (c.postId) {
        await api.patch(`/api/posts/${c.postId}`, { ..._cvDraftPayload(c), scheduled_at: '', qa: c.draft.qa || {} });
      } else {
        const p = await api.post(`/api/workspaces/${S.ws.id}/posts`,
          { ..._cvDraftPayload(c), scheduled_at: '', run_id: c.runId || '',
            qa: c.draft.qa || {}, dedupe_key: cvDedupeKey(cid) });
        c.postId = p.id;
      }
      c.postStatus = 'draft';
    } catch (e) { /* 落盘失败不打断画布体验（用户仍可手动存草稿） */ }
  })();
  c.persisting = run;                 // 在途落盘句柄:cvPublish 会先 await 它,避免抢建重复帖
  await run;
  if (c.persisting === run) c.persisting = null;
}

function cvBoardHead(n) {
  document.getElementById('cvBoardHead').innerHTML = n
    ? `<div class="page-title" style="font-size:19px">内容画布</div><span class="page-sub">${n} 个渠道 · 拖拽排布 · 编辑后授权发布</span>`
    : `<div class="page-sub">选好渠道、贴上素材,点“发送”开始 · 画布可拖拽平移、滚轮缩放</div>`;
}

/* 整板重建(结构性变化);轮询/单卡更新走 cvUpdateCard,保留其它卡的编辑与位置 */
function cvRenderBoard() {
  const world = cvWorldEl();
  const cards = Object.values(S.cv.cards || {});
  cvBoardHead(cards.length);
  world.innerHTML = cards.map(cvCard).join('');
  cards.forEach((c) => cvBindCard(c.channel_id));
  cvRenderSuggest();
  cvSyncSendState();
}

function cvUpdateCard(cid) {
  const c = S.cv.cards[cid]; if (!c) return;
  const world = cvWorldEl(); if (!world) return;
  const existing = world.querySelector(`.cv-card[data-cid="${cid}"]`);
  const tmp = document.createElement('div'); tmp.innerHTML = cvCard(c);
  const node = tmp.firstElementChild;
  if (existing) existing.replaceWith(node); else world.appendChild(node);
  cvBindCard(cid);
  cvBoardHead(Object.keys(S.cv.cards).length);
  cvRenderSuggest();
}

function cvBindCard(cid) {
  const card = document.querySelector(`#cvWorld .cv-card[data-cid="${cid}"]`);
  const c = S.cv.cards[cid]; if (!card || !c) return;
  // 正文字数计数
  if (c.draft) {
    const meta = S.platMap[c.platform] || {}; const limit = meta.max_chars || 2200;
    const cnt = card.querySelector('.cv-count');
    if (cnt) {
      const upd = () => {
        const n = (card.querySelector('.cd-hook').value + card.querySelector('.cd-body').value + card.querySelector('.cd-cta').value).length;
        cnt.textContent = `${n} / ${limit} 字`; cnt.classList.toggle('over', n > limit);
      };
      ['.cd-hook', '.cd-body', '.cd-cta'].forEach((s) => { const el = card.querySelector(s); if (el) el.oninput = upd; });
      upd();
    }
  }
  // 点卡片(非编辑/按钮区)= 切换引用到这条
  card.onclick = (e) => {
    if (e.target.closest('button,input,textarea,a,select,.x')) return;
    if (card.dataset.dragged) { delete card.dataset.dragged; return; }  // 刚拖过不算点击
    if (c.status === 'done') cvFocus(cid);
  };
  // 拖拽(标题作把手)
  const head = card.querySelector('.cv-card-head');
  if (head) head.onmousedown = (e) => {
    if (e.target.closest('button,input,textarea,a')) return;
    e.preventDefault(); e.stopPropagation();
    const sx = e.clientX, sy = e.clientY; const p0 = Object.assign({ x: 0, y: 0 }, c.pos);
    let moved = false;
    card.classList.add('dragging');
    const mv = (ev) => { moved = true; const s = S.cv.view.scale; c.pos = { x: p0.x + (ev.clientX - sx) / s, y: p0.y + (ev.clientY - sy) / s }; card.style.left = c.pos.x + 'px'; card.style.top = c.pos.y + 'px'; };
    const up = () => { card.classList.remove('dragging'); if (moved) card.dataset.dragged = '1'; window.removeEventListener('mousemove', mv); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', mv); window.addEventListener('mouseup', up);
  };
}

function cvCardStyle(c) { const p = c.pos || { x: 30, y: 70 }; return `style="left:${p.x}px;top:${p.y}px"`; }
function cvCardHead(c, extra) {
  return `<div class="cv-card-head"><h4>${pf(c.platform, 18)} ${esc(c.handle || c.platform)}</h4>${extra || ''}</div>`;
}

function cvCard(c) {
  const foc = S.cv.focus === c.channel_id ? ' focused' : '';
  if (c.status === 'gen') {
    const step = (c.progress && CV_STEPS[c.progress.step]) || '排队中';
    const pct = Math.round(((c.progress && c.progress.pct) || 0) * 100);
    return `<div class="cv-card gen${foc}" data-cid="${c.channel_id}" ${cvCardStyle(c)}>${cvCardHead(c, '<span class="cv-badge">生成中</span>')}
      <div class="cv-card-body"><div class="cv-skel">${icon('target','ico-sm')} ${esc(step)} <b>${pct}%</b><div class="bar"></div><div class="bar" style="width:80%"></div><div class="bar" style="width:60%"></div></div></div></div>`;
  }
  if (c.status === 'err') {
    return `<div class="cv-card err${foc}" data-cid="${c.channel_id}" ${cvCardStyle(c)}>${cvCardHead(c, '<span class="cv-badge warn">失败</span>')}
      <div class="cv-card-body"><div class="page-sub">${esc(c.errMsg || '这个渠道没生成成功。')}</div></div>
      <div class="cv-card-actions"><button class="btn" data-action="cv-regen" data-cid="${c.channel_id}">重新生成</button></div></div>`;
  }
  const d = c.draft || {}; const ct = d.content || {}; const q = d.qa || {}; const p = d.plan || {};
  const drv = (d.drivers || {}).copy || '';
  const drvBadge = d.fallback ? `<span class="cv-badge warn" title="真驱动失败,模板兜底">${icon('warning','ico-sm')} 模板</span>`
    : (drv ? `<span class="cv-badge live" title="真驱动生成">${icon('robot','ico-sm')} ${esc(drv)}</span>` : '');
  const langBadge = d.language ? `<span class="cv-badge">${esc(String(d.language).toUpperCase())}</span>` : '';
  const isVideo = d.format === 'short_video' || d.format === 'video';
  let media = '';
  if (d.visual_url && !isVideo) {
    media = `<div class="cv-card-media cv-poster"><img src="${esc(d.visual_url)}" alt=""/><div class="cap">${esc((ct.hook || '').slice(0, 60))}</div></div>`;
  } else if (isVideo) {
    media = `<div class="cv-vid">${d.visual_url ? `<img src="${esc(d.visual_url)}" alt=""/>` : ''}<span class="play">${icon('play')}</span><span class="dur">SRT</span></div>
      ${d.video_url ? `<a class="tag" href="${esc(d.video_url)}" target="_blank">🎬 分镜字幕 SRT</a>` : ''}`;
  } else if (d.visual_url) {
    media = `<div class="cv-card-media"><img src="${esc(d.visual_url)}" alt=""/></div>`;
  }
  const planLine = p.angle ? `<div class="page-sub" style="font-size:12px">📋 ${esc(p.angle)}${p.reason ? ' —— ' + esc(String(p.reason).slice(0, 60)) : ''}</div>` : '';
  const stBadge = c.postStatus === 'posted' ? `<span class="cv-badge live" title="已发布到平台">${icon('check','ico-sm')} 已发布</span>`
    : c.postStatus === 'scheduled' ? `<span class="cv-badge" title="已排期">${icon('calendar','ico-sm')} 已排期</span>`
    : c.postId ? '<span class="cv-badge" title="已自动存为草稿,可在日历/列表找到">📥 草稿已存</span>' : '';
  const badges = `<span class="cv-badge">${esc(FMT_LABEL[d.format] || d.format || '')}</span>${langBadge}${drvBadge}<span class="cv-badge ${q.passed ? 'ok' : ''}">QA ${Math.round((q.overall || 0) * 100)}</span>${stBadge}`;
  return `<div class="cv-card${foc}" data-cid="${c.channel_id}" ${cvCardStyle(c)}>
    ${cvCardHead(c, badges)}
    <div class="cv-card-body">
      ${planLine}
      ${media}
      <div class="cv-field"><label>Hook</label><input class="cd-hook" value="${esc(ct.hook || '')}"/></div>
      <div class="cv-field"><label>正文</label><textarea class="cd-body" style="min-height:96px">${esc(ct.body || '')}</textarea></div>
      <div class="cv-field"><label>CTA</label><input class="cd-cta" value="${esc(ct.cta || '')}"/></div>
      <div class="cv-field"><label>Hashtags(空格分隔)</label><input class="cd-tags" value="${esc((ct.hashtags || []).join(' '))}"/></div>
      <div class="cv-count"></div>
      <div class="cv-field"><label>定时(留空=立即)</label><input type="datetime-local" class="cd-sched" style="width:auto"/></div>
    </div>
    <div class="cv-card-actions">
      <button class="btn cv-focusbtn" data-action="cv-focus" data-cid="${c.channel_id}" title="让左侧对话只优化这一条">◎ 针对这条优化</button>
      <button class="btn" data-action="cv-regen" data-cid="${c.channel_id}" title="按当前对话指令重生成此渠道">↻</button>
      <div class="spacer" style="flex:1"></div>
      <button class="btn" data-action="cv-save" data-cid="${c.channel_id}">存草稿</button>
      <button class="btn" data-action="cv-schedule" data-cid="${c.channel_id}">排期</button>
      <button class="btn-primary" data-action="cv-publish" data-cid="${c.channel_id}">发布</button>
    </div></div>`;
}

/* ---- 卡片聚焦到对话:后续消息只优化这一条 ---- */
function cvFocus(cid) {
  if (!S.cv.cards[cid]) return;
  S.cv.focus = cid;
  cvRenderFocus();
  document.querySelectorAll('#cvWorld .cv-card').forEach((el) => el.classList.toggle('focused', el.dataset.cid === cid));
  const inp = document.getElementById('cvInput');
  const c = S.cv.cards[cid];
  inp.placeholder = `只优化 ${c.handle || c.platform} 这一条,例如“开头再抓人一点”`;
  inp.focus();
}
function cvUnfocus() {
  S.cv.focus = null; cvRenderFocus();
  document.querySelectorAll('#cvWorld .cv-card.focused').forEach((el) => el.classList.remove('focused'));
  document.getElementById('cvInput').placeholder = '说怎么改;或点某张卡“◎ 针对这条优化”只改它';
}
function cvRenderFocus() {
  const box = document.getElementById('cvFocus'); if (!box) return;
  const done = Object.values(S.cv.cards || {}).filter((c) => c.status === 'done');
  if (!S.cv.focus || !S.cv.cards[S.cv.focus]) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  const c = S.cv.cards[S.cv.focus];
  box.classList.remove('hidden');
  // 其它已完成的卡 → 一键切换引用
  const others = done.filter((o) => o.channel_id !== S.cv.focus)
    .map((o) => `<span class="qc" data-action="cv-switch-ref" data-cid="${o.channel_id}" title="改为引用 ${esc(o.platform)}">切到 ${esc(S.platMap[o.platform] ? S.platMap[o.platform].label : o.platform)}</span>`).join('');
  box.innerHTML = `<span>◎ 引用 ${pf(c.platform)} <b>${esc(c.handle || c.platform)}</b></span>${others ? ' · ' + others : ''}<span class="x" data-action="cv-unfocus" title="取消引用">${icon('x')}</span>`;
}

/* 聊天指令 → 动作:命名渠道→只重写该渠道;“加/add X”→加渠道;否则全体并发带指令重写 */
const CV_ALIAS = { ig: 'instagram', fb: 'facebook', x: 'x', twitter: 'x', tt: 'tiktok',
  yt: 'youtube', 'in': 'linkedin', linkedin: 'linkedin', 小红书: 'xiaohongshu', xhs: 'xiaohongshu',
  推特: 'x', 脸书: 'facebook', 油管: 'youtube' };
// 纯 ASCII 别名按"词边界"匹配,挡住 'in' 命中 'inspiring'、'x' 命中 'exciting' 这类误触;
// 含中文的别名(小红书/推特…)无词边界,直接子串匹配。
function _aliasHit(low, a) {
  if (!a) return false;
  if (/^[a-z0-9]+$/.test(a)) return new RegExp('(^|[^a-z0-9])' + a + '($|[^a-z0-9])', 'i').test(low);
  return low.includes(a);
}
function cvMatchPlatform(text) {
  const low = text.toLowerCase();
  for (const [a, p] of Object.entries(CV_ALIAS)) if (_aliasHit(low, a)) return p;
  return null;
}
async function cvHandleAdjust(text) {
  const conn = (S.channels || []).filter((c) => c.status === 'connected');
  const low = text.toLowerCase();
  const alias = cvMatchPlatform(text);
  const addMatch = /(?:加|添加|add)\s*(?:一个|个|a)?\s*([a-z一-龥]+)/i.exec(text);
  if (addMatch || (alias && !Object.values(S.cv.cards).some((c) => c.platform === alias))) {
    const key = addMatch ? addMatch[1].toLowerCase() : '';
    const cand = conn.find((c) => c.platform === alias || c.platform.toLowerCase().includes(key)
      || (S.platMap[c.platform] && (S.platMap[c.platform].label || '').toLowerCase().includes(key)));
    if (cand && !S.cv.cards[cand.id] && /加|添加|add/.test(text)) {
      cvBot(`好,加上 ${esc(cand.display_name || cand.platform)},按同一素材生成。`);
      await cvRunOne(cand.id, text);
      return;
    }
  }
  const named = Object.values(S.cv.cards).find((c) => {
    const meta = S.platMap[c.platform] || {};
    return c.platform === alias || _aliasHit(low, c.platform.toLowerCase())
      || (meta.label && _aliasHit(low, String(meta.label).toLowerCase()))
      // handle 才用子串,但要求 ≥3 字符,避免 'x' 之类过短句柄乱命中
      || (c.handle && String(c.handle).length >= 3 && low.includes(String(c.handle).toLowerCase()));
  });
  if (named) {
    cvBot(`收到,按“${esc(text)}”只重写 ${esc(named.handle || named.platform)}。`);
    await cvRunOne(named.channel_id, text);
    return;
  }
  const ids = Object.keys(S.cv.cards);
  cvBot(`好,按“${esc(text)}”把 ${ids.length} 个渠道都并发重写一版。`);
  await cvGenerate(ids, text);
}

async function cvAddChannel(cid) {
  const ch = (S.channels || []).find((c) => c.id === cid); if (!ch || S.cv.cards[cid]) return;
  cvBot(`加上 ${esc(ch.display_name || ch.platform)},按同一素材生成。`);
  await cvRunOne(cid, '');
}
async function cvRegenChannel(cid, instruction) { if (S.cv.cards[cid]) await cvRunOne(cid, instruction || ''); }

async function cvPublish(cid, mode) {
  const card = document.querySelector(`.cv-card[data-cid="${cid}"]`);
  const c = S.cv.cards[cid]; if (!c || !c.draft || !card) return;
  if (card.dataset.busy) return;
  const content = {
    hook: card.querySelector('.cd-hook').value, body: card.querySelector('.cd-body').value,
    cta: card.querySelector('.cd-cta').value,
    hashtags: card.querySelector('.cd-tags').value.split(/\s+/).filter(Boolean), segments: [] };
  const schedRaw = card.querySelector('.cd-sched').value;
  const scheduled_at = (mode === 'schedule' && schedRaw) ? new Date(schedRaw).toISOString() : '';
  if (mode === 'schedule' && !scheduled_at) return toast('请选择定时时间');
  card.dataset.busy = '1';
  card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  try {
    if (c.persisting) { try { await c.persisting; } catch (e) { /* ignore */ } }  // 等自动落盘完成,拿到 c.postId,避免抢建重复帖
    const d = c.draft;
    // 复用生成时已落盘的草稿(c.postId):PATCH 覆盖编辑值,避免重复建帖;否则兜底新建。
    const base = { title: content.hook.slice(0, 40) || '组合稿', content, format: d.format,
      channel_ids: [cid], scheduled_at, visual_svg: (d.visual_url && !d.ext_image) ? d.visual_svg : '',
      media: d.ext_image ? [d.ext_image] : [] };
    const post = c.postId
      ? await api.patch(`/api/posts/${c.postId}`, base)
      : await api.post(`/api/workspaces/${S.ws.id}/posts`,
          { ...base, qa: d.qa || {}, run_id: c.runId || '', dedupe_key: cvDedupeKey(cid) });
    c.postId = post.id;
    if (mode === 'publish') {
      if (!(await riskPreflight(post.id))) { cvSys('⛔ 风控预检未通过,已取消发布'); return; }
      const pub = await api.post(`/api/posts/${post.id}/publish`, {});
      const t = pub.targets && pub.targets[0];
      const sb = t && t.sandbox;   // 模拟渠道:未真实发布到平台
      cvSys(pub.status === 'posted'
        ? (sb ? `🧪 ${esc(c.platform)} 模拟发布(未真实上线,连接真实账号后生效)`
              : `✅ 已发布到 ${esc(c.platform)}${t && safeHref(t.permalink) ? ` · <a href="${esc(safeHref(t.permalink))}" target="_blank" rel="noopener noreferrer">查看</a>` : ''}`)
        : `❌ ${esc(c.platform)} 发布失败:${esc((t && t.error) || '')}`);
      toast(pub.status === 'posted' ? (sb ? '🧪 模拟发布' : '已发布 ✅') : '发布失败');
      if (pub.status === 'posted') { c.postStatus = 'posted'; card.style.opacity = 0.6; }  // 已发=终态,卡片保持锁定
      else { delete card.dataset.busy; card.querySelectorAll('button').forEach((b) => { b.disabled = false; }); }  // 发布失败可重试
    } else {
      // 存草稿/排期不是终态:清 busy、恢复按钮,用户之后还能改或直接发布(修复"存草稿后再也点不了发布")
      c.postStatus = (mode === 'schedule') ? 'scheduled' : 'draft';
      cvSys(mode === 'schedule' ? `🗓️ ${esc(c.platform)} 已排期` : `📝 ${esc(c.platform)} 已存草稿`);
      toast(mode === 'schedule' ? '已排期' : '已存草稿');
      delete card.dataset.busy;
      card.querySelectorAll('button').forEach((b) => { b.disabled = false; });
    }
  } catch (e) {
    delete card.dataset.busy;
    card.querySelectorAll('button').forEach((b) => { b.disabled = false; });
  }
}

/* ================= Create Post · 智能组合(旧抽屉,保留兼容) ============ */
async function openCompose() {
  const [channels, caps] = await Promise.all([
    api.get(`/api/workspaces/${S.ws.id}/channels`), api.get('/api/capabilities')]);
  S.channels = channels;
  const conn = channels.filter((c) => c.status === 'connected');
  window._composeDrafts = null;
  document.getElementById('composer').classList.remove('hidden');
  const h = document.querySelector('#composer .drawer-head h2'); if (h) h.textContent = 'Create post · 智能组合';
  const langBadge = (c) => (c.language && c.language !== 'auto')
    ? ` <b style="font-size:10px;opacity:.75">${c.language.toUpperCase()}</b>` : '';
  const chips = conn.map((c) => `<div class="cp" data-cid="${c.id}" data-platform="${c.platform}">${pf(c.platform)} ${esc(c.handle || c.platform)}${langBadge(c)}</div>`).join('')
    || '<span class="page-sub">无已连接渠道,先去 Connections 连接。</span>';
  const capChips = caps.filter((c) => c.enabled && c.available)
    .map((c) => `<span class="tag" title="${esc(c.note)}">${{text:'✍️',image:'🎨',video:'🎬'}[c.kind]||''} ${esc(c.label)}</span>`).join('')
    || '<span class="page-sub">无可用能力</span>';
  document.getElementById('composerBody').innerHTML = `
    <div class="page-sub">素材 all-in-one:文字、网页链接、图片链接、视频链接,<b>混着贴</b>即可 —— AI 自动识别、规划并为每个渠道生成能带来曝光转化的预发稿,你审阅后授权发布。</div>
    <div class="row" style="margin:8px 0"><button class="btn" data-action="manual-compose">✍️ 自己写 + 配图(不用 AI 改写,原文直发)</button></div>
    <div class="field" style="margin-top:8px"><label>素材(多模态,可混合)</label>
      <textarea id="cmp_source" style="min-height:100px" placeholder="粘贴任意组合:一段想法 + 文章URL + 图片URL + 视频URL…"></textarea></div>
    <div class="field"><label>主题 / 角度(可选,给 AI 的指令)</label><input id="cmp_notes" placeholder="例:突出省时间、真实案例"/></div>
    <div class="field"><label>目标渠道(可多选)</label><div class="channel-pick" id="cmp_channels">${chips}</div></div>
    <div class="row" style="flex-wrap:wrap;gap:10px">
      <label style="font-size:13px;font-weight:700">目标内容形式
        <select id="cmp_fmt" style="border:2px solid var(--line);border-radius:8px;padding:5px">
          <option value="auto">自动(AI 规划)</option><option value="text">纯文本</option>
          <option value="image_text">图文</option><option value="video">短视频</option></select></label>
      <label style="font-size:13px;font-weight:700">输出语言
        <select id="cmp_lang" style="border:2px solid var(--line);border-radius:8px;padding:5px">
          <option value="auto">自动(按渠道配置)</option>
          <option value="en">English</option><option value="zh">中文</option></select></label>
      <label style="font-size:13px;font-weight:700"><input type="checkbox" id="cmp_loop" checked/> Post Loop(剖析→research→策略→迭代)</label></div>
    <div class="field"><label>能力池(在 Settings 配置)</label>
      <div>${capChips} <a class="tag" data-action="goto-settings" style="cursor:pointer">${icon('gear','ico-sm')} 配置</a></div></div>
    <div class="row"><span class="page-sub">驱动:${esc(DRIVER_LABELS[S.settings.provider] || S.settings.provider)}</span>
      <div class="spacer"></div>
      <button class="btn-primary" data-action="compose-generate">${icon('sparkles')} 规划并为每个渠道生成</button></div>
    <div id="cmp_history"></div>
    <div id="cmp_out"></div>`;
  document.querySelectorAll('#cmp_channels .cp').forEach((cp) => cp.addEventListener('click', () => cp.classList.toggle('on')));
  loadComposeHistory();
}

async function loadComposeHistory() {
  const box = document.getElementById('cmp_history'); if (!box) return;
  let runs = [];
  try { runs = await api.get(`/api/workspaces/${S.ws.id}/compose-runs?limit=8`); } catch (e) { return; }
  if (!runs.length) { box.innerHTML = ''; return; }
  const mark = (r) => r.status === 'running' ? '⏳ ' : (r.status === 'failed' ? '❌ ' : (r.fallback ? icon('warning','ico-sm') + ' ' : ''));
  box.innerHTML = `<div class="page-sub" style="margin:10px 0 4px">${icon('clock','ico-sm')} 历史生成(点击恢复;${icon('warning','ico-sm')}=模板兜底 ⏳=进行中):</div>
    <div class="row" style="flex-wrap:wrap;gap:6px">` + runs.map((r) =>
      `<span class="tag" data-action="restore-run" data-id="${r.id}" style="cursor:pointer"
        title="${esc(r.material_preview || '')}">${mark(r)}${localStamp(r.created_at)} · ${r.channels}渠道</span>`).join('') + `</div>`;
}

async function restoreComposeRun(id) {
  const out = document.getElementById('cmp_out');
  out.innerHTML = `<div class="empty">恢复中…</div>`;
  try {
    const r = await api.get(`/api/compose-runs/${id}`);
    const rq = r.request || {};
    if (rq.source) document.getElementById('cmp_source').value = rq.source;
    if (rq.notes) document.getElementById('cmp_notes').value = rq.notes;
    if (rq.target_format) document.getElementById('cmp_fmt').value = rq.target_format;
    if (rq.language && document.getElementById('cmp_lang')) document.getElementById('cmp_lang').value = rq.language;
    const ids = new Set(rq.channel_ids || []);          // 还原渠道选择
    document.querySelectorAll('#cmp_channels .cp').forEach((cp) =>
      cp.classList.toggle('on', ids.has(cp.dataset.cid)));
    if (r.status === 'running') { await pollComposeRun(id, out); return; }  // 恢复进行中的 run
    if (r.status === 'failed') { out.innerHTML = `<div class="empty">❌ 该次生成失败:${esc(r.error || '')}</div>`; return; }
    renderComposeResult(r);
    toast('已恢复上次生成结果');
  } catch (e) { out.innerHTML = ''; }
}
const LOOP_STEPS = { queued: '排队', analyze: '① 剖析素材', research: '② 平台 research',
                     strategy: '③ 收敛策略', generate: '④ 生成+QA迭代', done: icon('check','ico-sm') + ' 完成' };

async function composeGenerate() {
  const source = document.getElementById('cmp_source').value.trim();
  const notes = document.getElementById('cmp_notes').value.trim();
  const channel_ids = [...document.querySelectorAll('#cmp_channels .cp.on')].map((c) => c.dataset.cid);
  if (!source && !notes) return toast('请填素材或主题');
  if (!channel_ids.length) return toast('请至少选一个渠道');
  const btn = document.querySelector('[data-action="compose-generate"]');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Loop 运行中…'; }
  const out = document.getElementById('cmp_out');
  out.innerHTML = `<div class="empty">启动 Post Creating Loop…</div>`;
  let runId;
  try {
    const s = await api.post(`/api/workspaces/${S.ws.id}/compose/start`, {
      source, notes, channel_ids,
      target_format: document.getElementById('cmp_fmt').value,
      language: (document.getElementById('cmp_lang') || {}).value || 'auto',
      loop: document.getElementById('cmp_loop').checked, max_iters: 2, optimize: true });
    runId = s.run_id;
    await pollComposeRun(runId, out);
  } catch (e) {
    out.innerHTML = `<div class="empty">启动失败,请重试</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = icon('sparkles') + ' 规划并为每个渠道生成'; }
  }
}

async function pollComposeRun(runId, out) {
  const order = ['analyze', 'research', 'strategy', 'generate'];
  window._activePollRun = runId;                        // 只允许一个活跃轮询
  for (let i = 0; i < 400; i++) {                       // 上限 ~20 分钟
    await new Promise((res) => setTimeout(res, 3000));
    // 抽屉已关 / 已切换到别的 run → 停止本轮询,不碰 UI
    if (window._activePollRun !== runId || !document.body.contains(out)) return;
    let r;
    try { r = await api.get(`/api/compose-runs/${runId}`); }
    catch (e) { continue; }                             // 网络抖动:继续轮询
    if (window._activePollRun !== runId || !document.body.contains(out)) return;
    if (r.status === 'done') { renderComposeResult(r); loadComposeHistory(); return; }
    if (r.status === 'failed') {
      out.innerHTML = `<div class="empty">❌ 生成失败:${esc(r.error || '未知错误')}</div>`; return;
    }
    const p = r.progress || {};
    const idx = order.indexOf(p.step);
    const steps = order.map((s, j) => {
      const cls = j < idx ? 'posted' : (j === idx ? 'scheduled' : '');
      const mark = j < idx ? '✓ ' : (j === idx ? icon('play','ico-sm') + ' ' : '');
      return `<span class="badge ${cls}">${mark}${LOOP_STEPS[s]}</span>`;
    }).join(' ');
    out.innerHTML = `<div class="card" style="margin-top:12px">
      <h4 style="margin:0 0 8px">Post Creating Loop 运行中</h4>
      <div class="row" style="flex-wrap:wrap;gap:6px">${steps}</div>
      <div class="page-sub" style="margin-top:8px">${esc(p.detail || '')} · ${Math.round((p.pct || 0) * 100)}%</div>
      <div style="height:8px;background:var(--line);border-radius:4px;margin-top:6px">
        <div style="height:8px;width:${Math.round((p.pct || 0) * 100)}%;background:#7c5cff;border-radius:4px;transition:width .5s"></div></div>
    </div>`;
  }
  out.innerHTML = `<div class="empty">超时:可在「历史生成」里查看该 run 的最终结果</div>`;
}

function renderComposeResult(r) {
  window._composeDrafts = r.drafts || [];
  const assets = r.material_assets || {};
  const assetNote = [
    assets.pages?.length ? `🔗 抓取网页×${assets.pages.length}` : '',
    assets.images?.length ? `${icon('image','ico-sm')} 配图×${assets.images.length}` : '',
    assets.videos?.length ? `🎬 视频素材×${assets.videos.length}` : '',
  ].filter(Boolean).join(' · ');
  const fallbackBanner = r.fallback
    ? `<div class="card" style="margin:10px 0;border-color:#ef4444;background:#fef2f2">
         <b>${icon('warning')} 本次全部为「模板兜底」内容</b>
         <div class="page-sub" style="margin-top:4px">选择的真驱动不可用(如 claude 未登录 / codex 限额),
         引擎已用离线模板兜底 —— 内容质量有限,建议修复驱动后重新生成。
         阶段溯源见每张卡片的驱动标记。</div></div>` : '';
  const ana = r.analysis || {};
  const anaLine = ana.subject
    ? `<div class="page-sub" style="margin-bottom:8px">🔍 素材剖析:${esc(ana.subject)}${ana.audience ? ' · 受众:' + esc(ana.audience) : ''}</div>` : '';
  const cards = (r.drafts || []).map((d, i) => {
    const c = d.content, q = d.qa || {}, p = d.plan || {}, rs = d.research || {};
    const iterStr = (d.iterations || []).map((x) => Math.round(x * 100)).join(' → ');
    const drvCopy = (d.drivers || {}).copy || '';
    const drvBadge = d.fallback
      ? `<span class="badge" style="background:#fee2e2;color:#b91c1c" title="真驱动失败,内容为离线模板生成">${icon('warning','ico-sm')} 模板兜底</span>`
      : (drvCopy ? `<span class="badge posted" title="文案由真驱动生成">${icon('robot','ico-sm')} ${esc(drvCopy)}</span>` : '');
    const langBadge = d.language ? `<span class="badge">${esc(String(d.language).toUpperCase())}</span>` : '';
    const researchLine = (rs.angles && rs.angles.length)
      ? `<div class="page-sub" style="margin:4px 0">🔬 research:${esc(rs.angles[0].angle || '')}${rs.angles[0].why ? ' —— ' + esc(rs.angles[0].why) : ''}${rs.algorithm_notes ? '<br/>' + icon('chart','ico-sm') + ' 算法:' + esc(rs.algorithm_notes) : ''}</div>` : '';
    return `<div class="card" style="margin-bottom:12px" data-idx="${i}">
      <div class="row" style="flex-wrap:wrap"><h4 style="margin:0">${pf(d.platform, 18)} ${esc(d.handle || d.platform)}</h4>
        <span class="badge">${esc(d.format)}</span>${langBadge}${drvBadge}
        <span class="badge ${q.passed ? 'posted' : 'scheduled'}">QA ${iterStr || Math.round((q.overall || 0) * 100)}</span>
        <span class="page-sub">${d.char_count} 字${(d.iterations||[]).length > 1 ? ' · 迭代'+((d.iterations.length)-1)+'次' : ''}</span></div>
      ${p.angle ? `<div class="page-sub" style="margin:6px 0">📋 策略:${esc(p.angle)}${p.reason ? ' —— ' + esc(p.reason) : ''}</div>` : ''}
      ${researchLine}
      <div class="field"><label>Hook</label><input class="dh" value="${esc(c.hook)}"/></div>
      <div class="field"><label>正文</label><textarea class="db" style="min-height:88px">${esc(c.body)}</textarea></div>
      <div class="field"><label>CTA</label><input class="dc" value="${esc(c.cta)}"/></div>
      <div class="field"><label>Hashtags(空格分隔)</label><input class="dt" value="${esc((c.hashtags || []).join(' '))}"/></div>
      ${d.visual_url ? `<div class="poster-preview" style="max-width:170px;margin-bottom:8px"><img src="${d.visual_url}"/></div>` : ''}
      ${d.video_url ? `<a class="tag" href="${d.video_url}" target="_blank">🎬 分镜字幕 SRT</a>` : ''}
      <div class="row"><input type="datetime-local" class="ds" style="border:2px solid var(--line);border-radius:8px;padding:6px"/>
        <div class="spacer"></div>
        <button class="btn" data-action="draft-save" data-idx="${i}">存草稿</button>
        <button class="btn" data-action="draft-schedule" data-idx="${i}">排期</button>
        <button class="btn-primary" data-action="draft-publish" data-idx="${i}">发布</button></div></div>`;
  }).join('');
  document.getElementById('cmp_out').innerHTML =
    `${fallbackBanner}
     <h3 style="margin:14px 0 8px">预发稿 · 每渠道一版(可编辑,授权后发布)</h3>
     ${anaLine}
     ${assetNote ? `<div class="page-sub" style="margin-bottom:8px">${assetNote}</div>` : ''}
     ${cards || '<div class="empty">无结果</div>'}`;
}
async function publishDraft(idx, mode) {
  const d = window._composeDrafts[idx]; if (!d) return;
  const card = document.querySelector(`.card[data-idx="${idx}"]`);
  if (card.dataset.busy) return;                        // 防重复点击 → 重复建帖
  const content = {
    hook: card.querySelector('.dh').value, body: card.querySelector('.db').value,
    cta: card.querySelector('.dc').value,
    hashtags: card.querySelector('.dt').value.split(/\s+/).filter(Boolean), segments: [] };
  const schedRaw = card.querySelector('.ds').value;
  const scheduled_at = (mode === 'schedule' && schedRaw) ? new Date(schedRaw).toISOString() : '';
  if (mode === 'schedule' && !scheduled_at) return toast('请选择定时时间');
  card.dataset.busy = '1';
  card.querySelectorAll('button').forEach((b) => { b.disabled = true; });
  try {
    // 若上次「建帖成功但发布失败」,重试时复用已建的 post,避免重复建帖
    const post = card.dataset.postId
      ? await api.patch(`/api/posts/${card.dataset.postId}`, { content, scheduled_at })
      : await api.post(`/api/workspaces/${S.ws.id}/posts`, {
          title: content.hook.slice(0, 40) || '组合稿', content, format: d.format,
          channel_ids: [d.channel_id], scheduled_at, visual_svg: d.visual_svg || '', qa: d.qa || {} });
    card.dataset.postId = post.id;
    if (mode === 'publish') {
      if (!(await riskPreflight(post.id))) return;
      const pub = await api.post(`/api/posts/${post.id}/publish`, {});
      const t = pub.targets?.[0];
      toast(pub.status === 'posted' ? (t?.sandbox ? '🧪 模拟发布(未真实上线)' : '已发布 ✅') : ('发布失败:' + (t?.error || '')));
    } else toast(mode === 'schedule' ? '已排期' : '已存草稿');
    card.style.opacity = 0.55;
  } catch (e) {
    delete card.dataset.busy;                           // 失败允许重试
    card.querySelectorAll('button').forEach((b) => { b.disabled = false; });
  }
}

/* ================= OAuth 一键授权连接(TryPost 式:整页跳转到平台授权页) ============ */
function oauthConnect(platform) {
  const p = S.platMap[platform];
  if (platform === 'mastodon') { openInstanceModal(platform); return; }  // 实例型:先问实例
  toast('前往 ' + p.label + ' 授权…');
  // 必须整页跳转(OAuth 无法在 fetch 里完成);服务器 /connect 会 302 到平台授权页
  window.location.href = `/connect/${platform}?ws=${encodeURIComponent(S.ws.id)}`;
}
function openInstanceModal(platform) {
  const p = S.platMap[platform];
  document.getElementById('modalBox').innerHTML = `
    <h3>连接 ${esc(p.label)}</h3>
    <div class="page-sub" style="margin-bottom:10px">输入你的实例地址,应用会在该实例自助注册后直接跳授权页(零配置)。</div>
    <div class="field"><label>实例地址</label><input id="mi_instance" placeholder="https://mastodon.social" value="https://mastodon.social"/></div>
    <div class="row" style="margin-top:12px"><div class="spacer"></div>
      <button class="btn" data-action="close-modal">取消</button>
      <button class="btn-primary" data-action="submit-instance" data-platform="${platform}">前往授权</button></div>`;
  document.getElementById('modal').classList.remove('hidden');
}
function submitInstance(platform) {
  const inst = document.getElementById('mi_instance').value.trim();
  if (!inst.startsWith('http')) return toast('请填有效实例地址');
  closeModal();
  window.location.href = `/connect/${platform}?ws=${encodeURIComponent(S.ws.id)}&instance=${encodeURIComponent(inst)}`;
}

/* ================= 全局事件 ================= */
async function globalActions(e) {
  const el = e.target.closest('[data-action]'); if (!el) return;
  const a = el.dataset.action;
  const stop = () => e.preventDefault();
  if (a === 'compose') { stop(); openCanvas(); }
  else if (a === 'cv-close') cvClose();
  else if (a === 'cv-send') cvSend();
  else if (a === 'cv-toggle-board') document.getElementById('canvas').classList.toggle('show-board');
  else if (a === 'cv-chan') { el.classList.toggle('on'); }
  else if (a === 'cv-qc') { const t = el.dataset.text; document.getElementById('cvInput').value = t; cvSend(); }
  else if (a === 'cv-addchan') cvAddChannel(el.dataset.cid);
  else if (a === 'cv-focus') cvFocus(el.dataset.cid);
  else if (a === 'cv-switch-ref') cvFocus(el.dataset.cid);
  else if (a === 'cv-unfocus') cvUnfocus();
  else if (a === 'cv-zoom-in') cvZoom(1.15);
  else if (a === 'cv-zoom-out') cvZoom(1 / 1.15);
  else if (a === 'cv-zoom-fit') cvZoomFit();
  else if (a === 'cv-regen') cvRegenChannel(el.dataset.cid);
  else if (a === 'cv-save') cvPublish(el.dataset.cid, 'draft');
  else if (a === 'cv-schedule') cvPublish(el.dataset.cid, 'schedule');
  else if (a === 'cv-publish') cvPublish(el.dataset.cid, 'publish');
  else if (a === 'cv-goto-settings') { cvClose(); document.querySelector('.nav-item[data-view="settings"]').click(); }
  else if (a === 'logout') { stop(); try { await api.post('/api/logout', {}); } catch (e) {} location.href = '/login'; }
  else if (a === 'manual-compose') { S.channels = await api.get(`/api/workspaces/${S.ws.id}/channels`); openComposer({}); }
  else if (a === 'compose-generate') composeGenerate();
  else if (a === 'restore-run') restoreComposeRun(el.dataset.id);
  else if (a === 'draft-save') publishDraft(+el.dataset.idx, 'draft');
  else if (a === 'draft-schedule') publishDraft(+el.dataset.idx, 'schedule');
  else if (a === 'draft-publish') publishDraft(+el.dataset.idx, 'publish');
  else if (a === 'goto-settings') { closeComposer(); document.querySelector('.nav-item[data-view="settings"]').click(); }
  else if (a === 'close-composer') closeComposer();
  else if (a === 'generate') doGenerate();
  else if (a === 'use-gen') { const g = window._lastGen?.r; if (g) { g.topic = window._lastGen.topic; openComposer(g); } }
  else if (a === 'c-generate') composerGenerate();
  else if (a === 'save-draft') savePost('draft');
  else if (a === 'schedule') savePost('schedule');
  else if (a === 'publish') savePost('publish');
  else if (a === 'rm-media') { S.composerMedia.splice(+el.dataset.i, 1); renderComposerMedia(); }
  else if (a === 'open-connect') openConnectModal(el.dataset.platform);
  else if (a === 'submit-connect') submitConnect(el.dataset.platform);
  else if (a === 'oauth-connect') oauthConnect(el.dataset.platform);
  else if (a === 'submit-instance') submitInstance(el.dataset.platform);
  else if (a === 'save-app') saveApp(el.dataset.platform);
  else if (a === 'close-modal') closeModal();
  else if (a === 'disconnect') { await api.post(`/api/channels/${el.dataset.id}/disconnect`, {}); toast('已断开'); viewConnections(); }
  else if (a === 'ch-profile') { stop(); await chProfile(el.dataset.id); }
  else if (a === 'ch-ads-link') { stop(); await chAdsLink(el.dataset.id, el.dataset.platform); }
  else if (a === 'submit-ads-link') { stop(); await submitAdsLink(el.dataset.id); }
  else if (a === 'acc-sync') { stop();
    const btn = el; const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '同步中…';
    try { const r = await api.post(`/api/workspaces/${S.ws.id}/channel-sync`, {});
      const c = r.content || {}; toast(`已同步:内容拉取 ${c.fetched||0}·导入 ${c.imported||0} · 画像 ${r.profiles?.refreshed||0}`); }
    catch (e) { toast('同步失败,请重试'); }
    finally { btn.disabled = false; btn.innerHTML = old; }
    await accRender(); }
  else if (a === 'go-accounts') { stop(); go('accounts'); }
  else if (a === 'test-driver') { const st = document.getElementById('driverStatus'); if (st) st.textContent = '测试中…(真实 CLI 驱动约 30-60s,请稍候)';
    try { const r = await api.post('/api/settings/test', {}); if (st) st.textContent = `✅ ${r.driver} · ${r.seconds}s · “${(r.hook||'').slice(0,30)}”`; }
    catch(err){ if (st) st.textContent = '测试失败'; } }
  else if (a === 'publish-now') { if (!(await riskPreflight(el.dataset.id))) return;
    const p = await api.post(`/api/posts/${el.dataset.id}/publish`, {}); toast(p.status==='posted'?'已发布 ✅':'发布失败'); go(S.view); }
  else if (a === 'del-post') { if (confirm('删除这条?')) {
      const r = await api.del(`/api/posts/${el.dataset.id}`);
      if (r && r.deleted_local === false) {
        // 远端删除临时失败 → 本地已保留;让用户选择是否强制只删本地
        if (confirm('⚠️ 平台删除失败(可能仅临时),已保留本地记录。\n\n点「确定」= 仅删除本地记录(平台上的帖可能仍在);\n点「取消」= 保留,稍后重试删除。')) {
          await api.del(`/api/posts/${el.dataset.id}?force=1`); toast('已删除本地记录');
        } else { toast('已保留本地记录,可稍后重试删除'); }
      }
      closeComposer();   // 若从只读预览抽屉删的,关掉抽屉,否则残留已删帖 + 后续按钮 404
      go(S.view);
    } }
  else if (a === 'sync-posts') syncPosts();
  else if (a === 'asset-src') { const s = el.dataset.src; S.assetFilter = { ...S.assetFilter, source: s };
    if (s !== 'external') { S.assetFilter.brand = 'all'; S.assetFilter.channel = 'all'; } viewAssets(); }
  else if (a === 'asset-form') { S.assetFilter = { ...S.assetFilter, form: el.dataset.form }; viewAssets(); }
  else if (a === 'asset-brand') { S.assetFilter = { ...S.assetFilter, brand: el.dataset.brand }; viewAssets(); }
  else if (a === 'asset-chan') { S.assetFilter = { ...S.assetFilter, channel: el.dataset.chan }; viewAssets(); }
  else if (a === 'asset-upload') { document.getElementById('assetUpload')?.click(); }
  else if (a === 'asset-del') { if (confirm('从素材库删除这条?')) { await api.del(`/api/workspaces/${S.ws.id}/assets/${el.dataset.id}`); viewAssets(); } }
  else if (a === 'ref-open') { openRefPicker('cvInput'); }
  else if (a === 'ref-facet') { S.refPick[el.dataset.dim] = el.dataset.val; if (el.dataset.dim === 'source') { S.refPick.form = 'all'; S.refPick.channel = 'all'; } refPickRender(); }
  else if (a === 'ref-toggle') { const id = el.dataset.id; S.refPick.sel.has(id) ? S.refPick.sel.delete(id) : S.refPick.sel.add(id); refPickRender(); }
  else if (a === 'ref-confirm') { refConfirm(); }
  else if (a === 'cv-recommend') { stop(); cvRecommend(); }
  else if (a === 'cv-ref-insert') { cvRefInsert(el.dataset.i); }
  else if (a === 'open-post') { const p = await api.get(`/api/posts/${el.dataset.id}`); openPostDetail(p); }
  else if (a === 'edit-post') { const p = await api.get(`/api/posts/${el.dataset.id}`); openComposer(p); }
  else if (a === 'go-connections') { closeComposer(); go('connections'); }
  else if (a === 'retry-post') { const pub = await api.post(`/api/posts/${el.dataset.id}/publish`, {});
    toast(pub.status === 'posted' ? '已发布 ✅' : '仍有渠道失败:' + (pub.targets?.find(t=>t.error)?.error || '')); closeComposer(); go(S.view); }
  else if (a === 'copy-as-draft') { const p = await api.get(`/api/posts/${el.dataset.id}`);
    const np = await api.post(`/api/workspaces/${S.ws.id}/posts`, {
      title: (p.title || '') + ' (副本)', content: p.content, format: p.format,
      channel_ids: (p.targets || []).filter(t => p.status !== 'failed' || t.status !== 'posted').map(t => t.channel_id), scheduled_at: '',
      visual_svg: p.visual_svg || '', media: p.media || [], qa: p.qa || {} });
    toast('已复制为草稿'); closeComposer(); openComposer(await api.get(`/api/posts/${np.id}`)); }
  else if (a === 'del-topic') { await api.del(`/api/topics/${el.dataset.id}`); viewTopics(); }
  else if (a === 'add-topic') { const title = document.getElementById('t_title').value.trim(); if (!title) return;
    await api.post(`/api/workspaces/${S.ws.id}/topics`, { title, pillar: document.getElementById('t_pillar').value }); viewTopics(); }
  else if (a === 'topic-to-studio') { document.querySelector('.nav-item[data-view="studio"]').click();
    setTimeout(()=>{ const i=document.getElementById('g_topic'); if(i) i.value = el.dataset.title; }, 60); }
  else if (a === 'tick') { const r = await api.post(`/api/workspaces/${S.ws.id}/tick`, {}); toast(`发布了 ${r.published.length} 条到期帖`); viewCalendar(); }
  else if (a === 'tick-sched') { const r = await api.post(`/api/workspaces/${S.ws.id}/tick`, {}); toast(`发布了 ${r.published.length} 条到期帖`); viewScheduled(); }
  else if (a === 'unschedule') { await api.patch(`/api/posts/${el.dataset.id}`, { scheduled_at: '' }); toast('已取消排期(转为草稿)'); viewScheduled(); }
  else if (a === 'retry-sched') { const pub = await api.post(`/api/posts/${el.dataset.id}/publish`, {}); toast(pub.status === 'posted' ? '已发布 ✅' : '仍有渠道失败'); viewScheduled(); }
  else if (a === 'cal-prev') { S.weekStart.setDate(S.weekStart.getDate()-7); viewCalendar(); }
  else if (a === 'cal-next') { S.weekStart.setDate(S.weekStart.getDate()+7); viewCalendar(); }
  else if (a === 'cal-today') { S.weekStart = mondayOf(new Date()); viewCalendar(); }
  else if (a === 'cal-fstatus') { S.calFilter = { ...(S.calFilter||{channel:'all'}), status: el.dataset.v }; renderCalendar(); }
  else if (a === 'cal-fchan') { S.calFilter = { ...(S.calFilter||{status:'all'}), channel: el.dataset.v }; renderCalendar(); }
  else if (a === 'cal-view') { S.calView = el.dataset.v; renderCalendar(); }
}

boot();
