/* 企业微信会话存档 — 管理前端（零依赖原生 JS） */
const API = '/api';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

/* ---------------- 全局错误捕获（显示在页面顶部，便于排查） ---------------- */
function showErrBar(msg) {
  const bar = $('#errBar');
  if (!bar) return;
  bar.style.display = 'block';
  bar.textContent = '⚠ 页面错误：' + msg;
}
window.addEventListener('error', (e) => {
  showErrBar((e.message || '未知错误') + (e.filename ? ` @ ${e.filename}:${e.lineno}` : ''));
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason;
  showErrBar((r && (r.message || r.detail || String(r))) || '未处理的 Promise 异常');
});

/* 前端版本戳：用于确认浏览器实际加载的是哪版 app.js（排查缓存/旧部署） */
const APP_JS_VERSION = '2026-08-18-5';
function markJsVersion() {
  const el = $('#jsVer');
  if (el) el.textContent = 'JS:' + APP_JS_VERSION + (typeof loadRooms === 'function' ? '' : ' ⚠缺loadRooms');
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', markJsVersion);
else markJsVersion();

/* ---------------- 基础工具 ---------------- */
const LS_TOKEN = 'wa_token';
const LS_USER = 'wa_user';

async function req(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const token = localStorage.getItem(LS_TOKEN);
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) {
    sessionExpired();
    throw new Error('登录已过期，请重新登录');
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) { /* 非 JSON 响应 */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- 登录认证与权限 ---------------- */
let AUTH_USER = null;

function loadAuth() {
  try { AUTH_USER = JSON.parse(localStorage.getItem(LS_USER) || 'null'); } catch (e) { AUTH_USER = null; }
  return AUTH_USER;
}

function saveAuth(user, token) {
  AUTH_USER = user;
  localStorage.setItem(LS_TOKEN, token || '');
  localStorage.setItem(LS_USER, JSON.stringify(user || null));
}

/** 是否拥有某权限码，如 hasPerm('records:delete')；超管恒为 true */
function hasPerm(code) {
  if (!AUTH_USER) return false;
  if (AUTH_USER.is_super) return true;
  return (AUTH_USER.perms || []).includes(code);
}

function showLogin() {
  const ov = $('#loginOverlay');
  if (!ov) return;
  ov.classList.add('show');
  $('#loginErr').textContent = '';
  setTimeout(() => { $('#loginUser').focus(); }, 60);
}
function hideLogin() { const ov = $('#loginOverlay'); if (ov) ov.classList.remove('show'); }

async function doLogin() {
  const u = $('#loginUser').value.trim();
  const p = $('#loginPass').value;
  const err = $('#loginErr');
  err.textContent = '';
  if (!u || !p) { err.textContent = '请输入用户名和密码'; return; }
  try {
    const res = await fetch(API + '/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u, password: p }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.detail || '登录失败');
    saveAuth(j.user, j.token);
    hideLogin();
    applyAuthUI();
    refreshModePill();
    const active = document.querySelector('.tab.active');
    const loader = MAIN_LOADERS[active && active.dataset.view];
    (loader || loadDashboard)();
  } catch (e) { err.textContent = e.message; }
}

function logout() {
  saveAuth(null, '');
  applyAuthUI();
  showLogin();
}

function sessionExpired() {
  saveAuth(null, '');
  applyAuthUI();
  showLogin();
}

/** 顶栏用户信息 + 页签/子页签可见性 + 按钮权限门 */
function applyAuthUI() {
  const logged = !!AUTH_USER;
  const up = $('#userPill');
  if (up) {
    up.style.display = logged ? '' : 'none';
    if (logged) up.textContent = '👤 ' + (AUTH_USER.display_name || AUTH_USER.username) + (AUTH_USER.is_super ? '（超管）' : '');
  }
  $('#btnLogout').style.display = logged ? '' : 'none';
  $('#btnChangePwd').style.display = logged ? '' : 'none';
  gateTabs();
  gateSubtabs();
  applyPermGates();
}

const CFG_VIEW_MODULES = ['templates', 'models', 'wecom', 'delivery', 'settings', 'extract', 'system'];
const ADMIN_MODULES = ['users', 'roles', 'permissions'];
const hasAnyView = (mods) => mods.some((m) => hasPerm(m + ':view'));

function gateTabs() {
  const showMap = {
    dashboard: hasPerm('dashboard:view'),
    rooms: hasPerm('rooms:view'),
    risks: hasPerm('risks:view'),
    data: hasAnyView(['records', 'attachments', 'messages']),
    config: hasAnyView(CFG_VIEW_MODULES),
    admin: hasAnyView(ADMIN_MODULES),
  };
  $$('.tab').forEach((t) => { t.style.display = showMap[t.dataset.view] ? '' : 'none'; });
  // 若当前激活页签被隐藏，跳到第一个可见页签
  const active = document.querySelector('.tab.active');
  if (active && active.style.display === 'none') {
    const first = $$('.tab').find((x) => x.style.display !== 'none');
    if (first) first.click();
  }
}

const SUBTAB_PERMS = {
  'risk-events': 'risks:view', 'risk-routing': 'risks:view', 'risk-config': 'risks:config',
  'risk-delivery': 'risks:view', 'risk-ocr-vision': 'risks:view',
  'data-records': 'records:view', 'data-attachments': 'attachments:view', 'data-messages': 'messages:view',
  'cfg-templates': 'templates:view', 'cfg-models': 'models:view', 'cfg-extract-compare': 'extract:view',
  'cfg-system': 'system:view', 'cfg-wecom': 'wecom:view', 'cfg-settings': 'settings:view',
  'admin-users': 'users:view', 'admin-roles': 'roles:view', 'admin-perms': 'permissions:view',
  'admin-license': 'users:view',
};

function gateSubtabs() {
  $$('.subtab').forEach((b) => {
    const need = SUBTAB_PERMS[b.dataset.sub];
    b.style.display = (!need || hasPerm(need)) ? '' : 'none';
  });
  // 当前激活子页签被隐藏 → 切到第一个可见子页签
  $$('.view').forEach((v) => {
    const active = v.querySelector('.subtab.active');
    if (active && active.style.display === 'none') {
      const first = Array.from(v.querySelectorAll('.subtab')).find((x) => x.style.display !== 'none');
      if (first) first.click();
    }
  });
}

/** 按钮级权限：扫描 [data-perm] 与内置 ID 映射，无权限则隐藏 */
const ID_PERM_MAP = {
  btnSync: 'system:operate', btnRun: 'system:operate', btnTestModels: 'models:operate',
  btnPause: 'system:operate', btnResume: 'system:operate', btnReloadCollector: 'system:operate',
  smtpSave: 'delivery:edit', smtpTest: 'delivery:operate',
  appSave: 'delivery:edit', appTest: 'delivery:operate', whTest: 'delivery:operate',
  recExport: 'records:export',
};
function applyPermGates() {
  $$('[data-perm]').forEach((el) => { el.style.display = hasPerm(el.dataset.perm) ? '' : 'none'; });
  Object.entries(ID_PERM_MAP).forEach(([id, code]) => {
    const el = document.getElementById(id);
    if (el) el.style.display = hasPerm(code) ? '' : 'none';
  });
}

async function refreshModePill() {
  try {
    const cfg = await req('/system/config');
    $('#modePill').textContent = '采集模式：' + (cfg.collector_mode === 'mock' ? 'mock（演示）' : 'archive（会话存档）');
  } catch (e) { $('#modePill').textContent = '采集模式 —'; }
}

/* ---------------- 群名称解析（rows 里只带 room_id，前端按缓存 map 反查名称） ---------------- */

let toastTimer = null;
function toast(msg, type = '') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = 'toast ' + type), 2800);
}

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const tag = (s) => `<span class="tag tag-${s || 'pending'}">${
  ({ done: '完成', pending: '待处理', processing: '处理中', failed: '失败', skipped: '跳过' }[s] || s || '-')
}</span>`;

const fmtTime = (t) => (t ? String(t).replace('T', ' ').slice(0, 19) : '-');
const fmtSize = (n) => {
  if (!n) return '-';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
};
const qs = (o) =>
  Object.entries(o)
    .filter(([, v]) => v !== '' && v !== null && v !== undefined)
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');

/* ---------------- 群名称解析（rows 里只带 room_id，前端按缓存 map 反查名称） ---------------- */
let _roomNameMap = null;
let _roomNameLoading = null;
async function getRoomNameMap() {
  if (_roomNameMap) return _roomNameMap;
  if (_roomNameLoading) return _roomNameLoading;
  _roomNameLoading = req('/rooms')
    .then((rooms) => { _roomNameMap = new Map(rooms.map((r) => [r.room_id, r.name || r.room_id])); return _roomNameMap; })
    .catch(() => { _roomNameMap = new Map(); return _roomNameMap; })
    .finally(() => { _roomNameLoading = null; });
  return _roomNameLoading;
}
function roomName(rid) {
  if (!rid) return '(单聊)';
  if (_roomNameMap && _roomNameMap.has(rid)) return _roomNameMap.get(rid);
  return rid;
}

/* ---------------- 外部联系人姓名解析（wo/wm 开头），mirror roomName ----------------
   后端 /api/wecom/external-contacts 按 id 批量解析（externalcontact/get + 缓存），
   前端懒加载：先按原始 id 展示，解析到位后二次渲染替换。 */
let _contactMap = null;
let _contactLoading = null;
function contactName(uid) {
  if (!uid) return uid;
  if (_contactMap && _contactMap.has(uid)) return _contactMap.get(uid);
  return uid;
}
async function ensureContacts(ids) {
  const ext = [...new Set((ids || []).filter((id) => id && (id.startsWith('wo') || id.startsWith('wm'))))];
  const miss = ext.filter((id) => !(_contactMap && _contactMap.has(id)));
  if (!miss.length) return;
  if (_contactLoading) return _contactLoading;
  const params = encodeURIComponent(miss.join(','));
  _contactLoading = req('/wecom/external-contacts?ids=' + params)
    .then((d) => {
      const names = (d && d.names) || {};
      if (!_contactMap) _contactMap = new Map();
      Object.entries(names).forEach(([k, v]) => _contactMap.set(k, v));
    })
    .catch(() => { /* 解析失败不影响展示，保留原始 id */ })
    .finally(() => { _contactLoading = null; });
  return _contactLoading;
}

/* ---------------- 风险相关枚举与路由聚合（前端，依据后端规则数据推导） ---------------- */
const SEV_LABEL = { low: '低', medium: '中', high: '高', critical: '严重' };
// 严重度兜底映射：规则未显式指定通知层时，按严重度决定推给哪些管理层
const SEV_LAYERS = { low: ['L1'], medium: ['L1', 'L2'], high: ['L2', 'L3'], critical: ['L1', 'L2', 'L3'] };

function layersOf(rule) {
  return (rule.alert_layers && rule.alert_layers.length) ? rule.alert_layers : (SEV_LAYERS[rule.severity] || ['L1']);
}
function ruleAppliesToRoom(rule, roomId) {
  const s = rule.scope_rooms || [];
  return s.length === 0 || s.includes(roomId);
}
const sevTag = (s) => `<span class="tag tag-${
  s === 'critical' ? 'failed' : s === 'high' ? 'warn' : s === 'medium' ? 'processing' : 'skipped'
}">${SEV_LABEL[s] || s}</span>`;
const alertTag = (s) => ({
  sent: '<span class="tag tag-done">已送达</span>',
  partial: '<span class="tag tag-warn">部分送达</span>',
  failed: '<span class="tag tag-failed">失败</span>',
  unsent: '<span class="tag tag-skipped">未发</span>',
}[s] || s);
const layerTags = (arr) => (arr && arr.length)
  ? arr.slice().sort().map((l) => `<span class="lvl-tag">${esc(l)}</span>`).join(' ')
  : '<span class="muted">无</span>';

/* ---------------- 主标签切换（以"群"为中心的业务视图） ---------------- */
const MAIN_LOADERS = {
  dashboard: loadDashboard,
  rooms: loadRooms,
  risks: () => loadRisks(1),
  data: () => initRecords(),
  config: () => {
    const v = document.getElementById('view-config');
    const active = v && v.querySelector('.subtab.active');
    const loader = SUB_LOADERS[active && active.dataset.sub];
    (loader || loadTemplates)();
  },
  admin: () => {
    const v = document.getElementById('view-admin');
    const active = v && v.querySelector('.subtab.active');
    const loader = SUB_LOADERS[active && active.dataset.sub];
    (loader || loadUsers)();
  },
};

function resetSubtabs(viewEl) {
  const firstSub = viewEl.querySelector('.subtab');
  const firstView = viewEl.querySelector('.subview');
  viewEl.querySelectorAll('.subtab').forEach((x) => x.classList.remove('active'));
  viewEl.querySelectorAll('.subview').forEach((x) => x.classList.remove('active'));
  if (firstSub) firstSub.classList.add('active');
  if (firstView) firstView.classList.add('active');
}

$$('.tab').forEach((t) => {
  t.onclick = () => {
    $$('.tab').forEach((x) => x.classList.remove('active'));
    $$('.view').forEach((x) => x.classList.remove('active'));
    t.classList.add('active');
    const v = $('#view-' + t.dataset.view);
    v.classList.add('active');
    resetSubtabs(v);
    const loader = MAIN_LOADERS[t.dataset.view];
    loader && loader();
    gateSubtabs();
    applyPermGates();
  };
});

/* ---------------- 子标签切换（同一主视图内的子面板） ---------------- */
const SUB_LOADERS = {
  'risk-events': () => loadRisks(1),
  'risk-routing': loadRouting,
  'risk-config': loadRiskConfig,
  'risk-delivery': loadDeliveryLogs,
  'risk-ocr-vision': loadOcrVisionConfig,
  'data-records': () => initRecords(),
  'data-attachments': () => loadAttachments(1),
  'data-messages': () => loadMessages(1),
  'cfg-templates': loadTemplates,
  'cfg-models': loadModels,
  'cfg-extract-compare': loadExtractCompare,
  'cfg-system': loadSystem,
  'cfg-wecom': loadWeComConfig,
  'cfg-settings': loadSysSettings,
  'admin-users': loadUsers,
  'admin-roles': loadRoles,
  'admin-perms': loadPermCatalog,
  'admin-license': loadLicense,
};
function bindSubtabs() {
  $$('.view').forEach((v) => {
    const subs = v.querySelectorAll('.subtab');
    subs.forEach((b) => {
      b.onclick = () => {
        subs.forEach((x) => x.classList.remove('active'));
        v.querySelectorAll('.subview').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
        const sv = v.querySelector('#sub-' + b.dataset.sub);
        if (sv) sv.classList.add('active');
        const loader = SUB_LOADERS[b.dataset.sub];
        loader && loader();
      };
    });
  });
}

/* ---------------- 群卡片渲染（总览与群与监控共用） ---------------- */
function renderRoomCards(el, rooms, rules, byRoom) {
  if (!el) return;
  if (!rooms || !rooms.length) {
    el.innerHTML = '<div class="empty">暂无群数据，点右上角「立即同步」拉取群聊</div>';
    return;
  }
  el.innerHTML = rooms.map((r) => {
    const applicable = rules.filter((rl) => ruleAppliesToRoom(rl, r.room_id));
    const lset = new Set();
    applicable.forEach((rl) => layersOf(rl).forEach((l) => lset.add(l)));
    const riskCnt = (byRoom && byRoom[r.room_id]) || 0;
    return `<div class="room-card" onclick="openRoom('${esc(r.room_id)}')">
      <div class="room-head">
        <span class="room-name">${esc(r.name || r.room_id)}</span>
        <label class="switch" data-perm="rooms:edit" title="采集开关" onclick="event.stopPropagation()">
          <input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="toggleRoom('${esc(r.room_id)}', this.checked)">
          <span class="slider"></span>
        </label>
      </div>
      <div class="room-id">${esc(r.room_id)}</div>
      <div class="room-routing"><span class="rr-label">风险预警走向</span><div class="rr-layers">${layerTags([...lset])}</div></div>
      ${riskCnt ? `<div class="room-risk">⚠ 已触发 ${riskCnt} 条风险</div>` : ''}
    </div>`;
  }).join('');
}

/* ================ 概览 ================ */

// —— 风险图表（纯 SVG，零依赖）——
const SEV_COLOR = { high: 'var(--err)', critical: 'var(--err)', medium: 'var(--warn)', low: 'var(--skip)', unknown: 'var(--skip)' };

function svgTrend(daily) {
  const data = [...(daily || [])].reverse();
  const W = 640, H = 200, padL = 30, padR = 12, padT = 12, padB = 26;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const max = Math.max(1, ...data.map((d) => d.count || 0));
  const n = data.length || 1;
  const bw = innerW / n;
  let s = `<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${padT + innerH}" stroke="var(--line)"/>`
        + `<line x1="${padL}" y1="${padT + innerH}" x2="${W - padR}" y2="${padT + innerH}" stroke="var(--line)"/>`;
  data.forEach((d, i) => {
    const c = d.count || 0;
    const h = (c / max) * innerH;
    const w = bw * 0.66;
    const x = padL + i * bw + (bw - w) / 2;
    const y = padT + innerH - h;
    s += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="2" fill="var(--accent)"><title>${esc(d.date)}：${c}</title></rect>`;
    if (c > 0) s += `<text x="${(x + w / 2).toFixed(1)}" y="${(y - 3).toFixed(1)}" text-anchor="middle" font-size="9" fill="var(--muted)">${c}</text>`;
    if (i % 2 === 0 || i === n - 1) {
      s += `<text x="${(x + w / 2).toFixed(1)}" y="${(H - 8).toFixed(1)}" text-anchor="middle" font-size="9" fill="var(--muted)">${esc(String(d.date).slice(5))}</text>`;
    }
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block">${s}</svg>`;
}

function svgHBar(obj, colorMap, labelMap) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return '<div class="empty" style="padding:16px">暂无数据</div>';
  const max = Math.max(1, ...entries.map(([, v]) => v));
  const labelW = 96, barW = 300, valW = 30, rowH = 22, padT = 2;
  const H = padT * 2 + entries.length * rowH;
  let s = '';
  entries.forEach(([k, v], i) => {
    const y = padT + i * rowH;
    const w = Math.max(2, (v / max) * barW);
    const color = (colorMap && (colorMap[k] || colorMap._)) || 'var(--accent)';
    const raw = (labelMap && labelMap[k] != null) ? labelMap[k] : k;
    const label = raw.length > 12 ? raw.slice(0, 12) + '…' : raw;
    s += `<text x="0" y="${y + rowH / 2 + 4}" font-size="11" fill="var(--text)">${esc(label)}</text>`;
    s += `<rect x="${labelW}" y="${y + 3}" width="${w.toFixed(1)}" height="${rowH - 9}" rx="3" fill="${color}"/>`;
    s += `<text x="${labelW + w + 6}" y="${y + rowH / 2 + 4}" font-size="11" fill="var(--muted)">${v}</text>`;
  });
  return `<svg viewBox="0 0 ${labelW + barW + valW} ${H}" width="100%" style="display:block">${s}</svg>`;
}

function renderRiskCharts(stats) {
  if (!stats) return;
  const trend = document.getElementById('riskTrendChart');
  const sev = document.getElementById('sevChart');
  const cat = document.getElementById('catChart');
  if (trend) trend.innerHTML = svgTrend(stats.daily);
  if (sev) sev.innerHTML = svgHBar(stats.by_severity, SEV_COLOR, SEV_LABEL);
  if (cat) {
    const top = Object.entries(stats.by_category || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
    cat.innerHTML = svgHBar(Object.fromEntries(top));
  }
}

async function loadDashboard() {
  let s;
  try { s = await req('/system/stats'); } catch (e) { return toast('加载统计失败：' + e.message, 'err'); }

  const T = s.totals;
  const cards = [
    { l: '消息', n: T.messages, nav: ['data', 'data-messages'] },
    { l: '附件', n: T.attachments, nav: ['data', 'data-attachments'] },
    { l: 'OCR 结果', n: T.ocr_results, nav: ['data', 'data-attachments'] },
    { l: '结构化记录', n: T.records, nav: ['data', 'data-records'] },
    { l: '风险事件', n: T.risk_events || 0, nav: ['risks', 'risk-events'], risk: {} },
    { l: '群数', n: T.rooms, nav: ['rooms'] },
  ];
  $('#statCards').innerHTML = cards.map(({ l, n, nav, risk }) => {
    const attr = `data-nav="${nav.join(',')}"` + (risk ? ` data-risk="${encodeURIComponent(JSON.stringify(risk))}"` : '');
    return `<div class="card stat-card" ${attr}><div class="num">${n}</div><div class="lbl">${l}</div></div>`;
  }).join('');
  $('#statCards').querySelectorAll('.stat-card').forEach((c) => {
    c.onclick = () => {
      const [v, sub] = c.dataset.nav.split(',');
      goView(v, sub);
      if (c.dataset.risk) applyRiskFilter(JSON.parse(decodeURIComponent(c.dataset.risk)));
    };
  });

  const bar = (title, obj) => {
    const total = Object.values(obj).reduce((a, b) => a + b, 0) || 1;
    const order = ['done', 'processing', 'pending', 'failed', 'skipped'];
    const segs = order.filter((k) => obj[k]).map(
      (k) => `<span class="seg-${k}" style="width:${(obj[k] / total * 100).toFixed(1)}%" title="${k}:${obj[k]}"></span>`).join('');
    const detail = order.filter((k) => obj[k]).map((k) => `${k} ${obj[k]}`).join(' · ') || '暂无数据';
    return `<div class="bar-row"><div class="bar-head"><span>${title}</span><span>${detail}</span></div>
            <div class="bar">${segs}</div></div>`;
  };
  $('#stageBars').innerHTML =
    bar('下载', s.attachment_download) + bar('OCR', s.attachment_ocr) + bar('结构化抽取', s.attachment_extract) +
    `<div class="legend">
      <span><i class="seg-done"></i>完成</span><span><i class="seg-processing"></i>处理中</span>
      <span><i class="seg-pending"></i>待处理</span><span><i class="seg-failed"></i>失败</span>
      <span><i class="seg-skipped"></i>跳过</span></div>`;

  const dist = (title, obj) => {
    const items = Object.entries(obj);
    return `<h4>${title}</h4>` + (items.length
      ? items.map(([k, v]) => `<div class="dist-item"><span>${esc(k)}</span><b>${v}</b></div>`).join('')
      : '<div class="dist-item"><span>暂无</span></div>');
  };
  $('#distBox').innerHTML = dist('按模板分布', s.record_by_template) + dist('按消息类型', s.message_by_type);

  // 群一览卡片
  const rooms = await req('/rooms').catch(() => []);
  const rules = await req('/risks/rules').catch(() => []);
  const rstats = await req('/risks/stats').catch(() => ({ by_room: {} }));
  renderRoomCards($('#roomCards'), rooms, rules, rstats.by_room || {});
  renderRiskCharts(rstats);
}

/* ================ 群与监控 ================ */
let roomFilter = 'all';
async function loadRooms() {
  const [rooms, rules, stats] = await Promise.all([
    req('/rooms').catch(() => []),
    req('/risks/rules').catch(() => []),
    req('/risks/stats').catch(() => ({ risk_events: 0, pending: 0, by_room: {} })),
  ]);
  const cards = [
    { l: '监控群数', n: rooms.length, f: 'all' },
    { l: '采集中', n: rooms.filter((r) => r.enabled).length, f: 'enabled' },
    { l: '风险事件', n: stats.total || 0, f: 'risk-all' },
    { l: '待处置', n: stats.pending || 0, f: 'risk-pending' },
  ];
  $('#roomStatCards').innerHTML = cards.map(({ l, n, f }) =>
    `<div class="card stat-card ${roomFilter === f ? 'active' : ''}" data-f="${f}"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join('');
  $('#roomStatCards').querySelectorAll('.stat-card').forEach((c) => {
    c.onclick = () => onRoomStatClick(c.dataset.f);
  });
  const filtered = roomFilter === 'enabled' ? rooms.filter((r) => r.enabled) : rooms;
  renderRoomCards($('#roomList'), filtered, rules, stats.by_room || {});
}

function onRoomStatClick(f) {
  if (f === 'risk-all') { goView('risks', 'risk-events'); applyRiskFilter({}); return; }
  if (f === 'risk-pending') { goView('risks', 'risk-events'); applyRiskFilter({ status: 'pending' }); return; }
  roomFilter = f;
  loadRooms();
}

window.openRoom = async function (roomId) {
  const [rooms, evs, rules] = await Promise.all([
    req('/rooms').catch(() => []),
    req('/risks/events?room_id=' + encodeURIComponent(roomId) + '&page=1&page_size=8').catch(() => ({ items: [] })),
    req('/risks/rules').catch(() => []),
  ]);
  const room = rooms.find((r) => r.room_id === roomId) || { room_id: roomId };
  await ensureContacts([room.owner]);
  const applicable = rules.filter((rl) => ruleAppliesToRoom(rl, roomId));
  const lset = new Set();
  applicable.forEach((rl) => layersOf(rl).forEach((l) => lset.add(l)));
  const mini = (items, fn) => items.length
    ? items.map(fn).join('')
    : '<div class="dist-item"><span>无</span></div>';

  openDrawer('群：' + esc(room.name || roomId), `
    <div class="kv">
      <span class="k">群ID</span><span class="v">${esc(room.room_id)}</span>
      <span class="k">备注名</span><span class="v">${esc(room.name || '-')}</span>
      <span class="k">群主</span><span class="v">${esc(contactName(room.owner) || '-')}</span>
      <span class="k">成员数</span><span class="v">${(room.members && room.members.length) || room.member_count || 0}</span>
      <span class="k">采集</span><span class="v">${room.enabled ? '<span class="tag tag-done">开</span>' : '<span class="tag tag-skipped">关</span>'}</span>
      <span class="k">消息/附件</span><span class="v">${room.msg_count || 0} / ${room.attachment_count || 0}</span>
      <span class="k">最近活动</span><span class="v">${fmtTime(room.last_msg_at)}</span>
    </div>
    <h4>本群风险预警走向（命中规则后推给）</h4>
    <div class="rr-layers">${layerTags([...lset])}</div>
    <h4>本群适用的风险规则（${applicable.length}）</h4>
    ${applicable.length ? mini(applicable, (rl) => `<div class="dist-item"><span>${esc(rl.name)} · ${SEV_LABEL[rl.severity] || rl.severity}</span><span class="muted">${esc(rl.category)}</span></div>`) : '<div class="dist-item"><span>无（仅全群规则生效）</span></div>'}
    <h4>风险事件</h4>${mini(evs.items, (r) => `<div class="dist-item"><span>${sevTag(r.severity)} ${esc(r.category)}</span><span class="muted">${esc((r.snippet || '').slice(0, 30))}</span></div>`)}
    <div class="row-btns">
      <button class="btn btn-sm" id="drToggleRoom">${room.enabled ? '关闭采集' : '开启采集'}</button>
      <button class="btn btn-sm" id="drSyncWeCom">从企微同步群信息</button>
      <button class="btn btn-sm btn-warn" id="drDelRoom">删除群及存档</button>
    </div>
  `);
  const tr = $('#drToggleRoom');
  if (tr) tr.onclick = async () => { await toggleRoom(room.room_id, !room.enabled); closeDrawer(); };
  const sw = $('#drSyncWeCom');
  if (sw) sw.onclick = async () => { await syncRoomFromWeCom(room.room_id); };
  const dr = $('#drDelRoom');
  if (dr) dr.onclick = async () => { await delRoom(room.room_id, false); };
};

/* ---------------- 群采集开关 ---------------- */
window.toggleRoom = async function (roomId, enabled) {
  try {
    await req('/rooms/' + encodeURIComponent(roomId), { method: 'PATCH', body: JSON.stringify({ enabled }) });
    toast(enabled ? '已开启采集' : '已关闭采集', 'ok');
    if ($('#view-rooms').classList.contains('active')) loadRooms();
    if ($('#view-dashboard').classList.contains('active')) loadDashboard();
  } catch (e) { toast('操作失败：' + e.message, 'err'); }
};

window.syncRoomFromWeCom = async function (roomId) {
  try {
    const r = await req('/wecom/groupchat/' + encodeURIComponent(roomId));
    toast(`已同步群信息：${r.name || roomId} · 成员 ${r.member_count}`, 'ok');
    openRoom(roomId);
    if ($('#view-rooms').classList.contains('active')) loadRooms();
  } catch (e) { toast('同步失败：' + e.message, 'err'); }
};

window.batchToggleRooms = async function (enabled) {
  if (!confirm(enabled ? '确认开启所有群的采集？' : '确认关闭所有群的采集？已入库的历史数据保留。')) return;
  try {
    const r = await req('/rooms/batch-toggle', { method: 'POST', body: JSON.stringify({ enabled }) });
    toast(`已${enabled ? '开启' : '关闭'} ${r.updated} 个群的采集`, 'ok');
    if ($('#view-rooms').classList.contains('active')) loadRooms();
    if ($('#view-dashboard').classList.contains('active')) loadDashboard();
  } catch (e) { toast('操作失败：' + e.message, 'err'); }
};

/* ================ 路由全景（直观展示 群/规则 → 管理层） ================ */
async function loadRouting() {
  const box = $('#routingBox');
  if (!box) return;
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const [rules, layers, logs] = await Promise.all([
      req('/risks/rules'), req('/risks/layers'),
      req('/risks/logs?page_size=1').catch(() => ({ total: 0, by_status: {}, by_channel: {} })),
    ]);
    const ruleRows = rules.length ? rules.map((rl) => `<tr>
        <td>${esc(rl.name)}</td>
        <td><span class="tag tag-skipped">${esc(rl.category)}</span></td>
        <td>${sevTag(rl.severity)}</td>
        <td>${(rl.scope_rooms && rl.scope_rooms.length) ? rl.scope_rooms.length + ' 个群' : '全部群'}</td>
        <td>${layerTags(layersOf(rl))}</td>
      </tr>`).join('') : '<tr><td colspan="5" class="empty">暂无规则</td></tr>';

    // 全链条最后一环：投递可达性概览
    const sent = (logs.by_status && logs.by_status.sent) || 0;
    const failed = (logs.by_status && logs.by_status.failed) || 0;
    const targetCnt = layers.reduce((n, l) => n + (l.targets || []).length, 0);
    const summary = `<div class="chain-summary">
        <div class="cs-item clickable" data-route="delivery" data-dl-status="" title="点击查看全部投递回执"><span class="cs-num">${logs.total || 0}</span><span class="cs-lbl">累计投递</span></div>
        <div class="cs-item clickable ok" data-route="delivery" data-dl-status="sent" title="点击查看送达回执"><span class="cs-num">${sent}</span><span class="cs-lbl">送达</span></div>
        <div class="cs-item clickable bad" data-route="delivery" data-dl-status="failed" title="点击查看失败回执"><span class="cs-num">${failed}</span><span class="cs-lbl">失败</span></div>
        <div class="cs-item clickable" data-route="layers" title="点击查看管理层与投递目标"><span class="cs-num">${layers.length}</span><span class="cs-lbl">管理层</span></div>
        <div class="cs-item clickable" data-route="targets" title="点击查看投递目标"><span class="cs-num">${targetCnt}</span><span class="cs-lbl">投递目标</span></div>
      </div>`;

    const layerCards = layers.length ? layers.map((l) => {
      const coverRules = rules.filter((rl) => layersOf(rl).includes(l.id));
      const roomSet = new Set(); let allGroups = false;
      coverRules.forEach((rl) => { const s = rl.scope_rooms || []; if (s.length === 0) allGroups = true; s.forEach((x) => roomSet.add(x)); });
      const coverTxt = allGroups ? '<span class="tag tag-done">全部群</span>'
        : (roomSet.size ? [...roomSet].map((x) => `<span class="tag tag-skipped">${esc(x)}</span>`).join(' ') : '<span class="muted">无（仅严重度兜底可能覆盖）</span>');
      const targets = (l.targets || []).map((t) => `<div class="target-row mini">
          <span class="ch ch-${esc(t.channel)}">${esc(CH_LABEL[t.channel] || t.channel)}</span>
          <span class="tg" title="${esc(t.target || '')}">${esc(t.label || t.target || (t.channel === 'system' ? '自动送达' : '(无目标)'))}</span>
          ${targetBadge(t)}
        </div>`).join('') || '<div class="desc">该层暂无投递目标</div>';
      return `<div class="layer-card">
        <h4>${esc(l.name)} <span class="lvl">${esc(l.id)}</span></h4>
        <div class="desc">会收到来自：${coverTxt}</div>
        <div class="desc">覆盖规则 ${coverRules.length} 条</div>
        <div class="targets-block">${targets}</div>
      </div>`;
    }).join('') : '<div class="empty">暂无管理层</div>';

    box.innerHTML = summary + `<div class="grid-2">
      <div><h4>规则 → 通知管理层</h4>
        <div class="table-wrap"><table><thead><tr><th>规则</th><th>分类</th><th>严重度</th><th>作用群</th><th>通知层</th></tr></thead>
        <tbody>${ruleRows}</tbody></table></div>
      </div>
      <div><h4>管理层 → 投递目标（通道与状态）</h4>
        <div class="layer-list">${layerCards}</div>
      </div>
    </div>`;
    const summaryEl = box.querySelector('.chain-summary');
    if (summaryEl) {
      summaryEl.querySelectorAll('[data-route]').forEach((el) => {
        el.onclick = () => {
          if (el.dataset.route === 'delivery') {
            applyDeliveryFilter({ status: el.dataset.dlStatus });
          } else {
            goView('risks', 'risk-config');
          }
        };
      });
    }
  } catch (e) { box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

/* ================ 送达回执（全链条最后一环） ================ */
let dlPage = 1;
async function loadDeliveryLogs(page) {
  dlPage = page || 1;
  const wrap = $('#deliveryTable tbody');
  const sumBox = $('#deliverySummary');
  if (!wrap) return;
  wrap.innerHTML = '<tr><td colspan="7" class="empty">加载中…</td></tr>';
  try {
    await getRoomNameMap();
    const status = $('#dlStatus').value;
    const channel = $('#dlChannel').value;
    const d = await req('/risks/logs?' + qs({ page: dlPage, page_size: 30, status, channel }));
    if (sumBox) {
      const sent = (d.by_status && d.by_status.sent) || 0;
      const failed = (d.by_status && d.by_status.failed) || 0;
      const total = d.total || 0;
      sumBox.innerHTML = [
        { status: '', cls: '', num: total, lbl: '条回执' },
        { status: 'sent', cls: 'ok', num: sent, lbl: '送达' },
        { status: 'failed', cls: 'bad', num: failed, lbl: '失败' },
      ].map(({ status, cls, num, lbl }) =>
        `<span class="cs-item clickable ${cls}" data-dl-status="${status}" title="点击查看${status === 'sent' ? '送达' : status === 'failed' ? '失败' : '全部'}回执"><b>${num}</b> ${lbl}</span>`
      ).join('');
      sumBox.querySelectorAll('[data-dl-status]').forEach((el) => {
        el.onclick = () => applyDeliveryFilter({ status: el.dataset.dlStatus });
      });
    }
    $('#dlTotal').textContent = '共 ' + (d.total || 0) + ' 条';
    if (!d.items.length) { wrap.innerHTML = '<tr><td colspan="7" class="empty">暂无投递回执</td></tr>'; $('#deliveryPager').innerHTML = ''; return; }
    wrap.innerHTML = d.items.map((x) => {
      const tgt = x.target || (x.channel === 'system' ? '自动送达' : '-');
      const tgtShort = tgt.length > 30 ? tgt.slice(0, 30) + '…' : tgt;
      return `<tr>
        <td>${fmtTime(x.sent_at)}</td>
        <td>${sevTag(x.severity)} ${esc(x.category || '-')}<br><span class="muted">${esc(roomName(x.room_id))}${x.snippet ? ' · ' + esc(x.snippet) : ''}</span></td>
        <td>${esc(x.layer_id || '系统内')}</td>
        <td><span class="ch ch-${esc(x.channel)}">${esc(CH_LABEL[x.channel] || x.channel)}</span></td>
        <td title="${esc(tgt)}">${esc(tgtShort)}</td>
        <td>${x.status === 'sent' ? '<span class="badge badge-on">✅ 送达</span>' : '<span class="badge badge-warn">❌ 失败</span>'}</td>
        <td class="muted" title="${esc(x.detail || '')}">${esc((x.detail || '').slice(0, 40))}</td>
      </tr>`;
    }).join('');
    const total = d.total || 0, ps = d.page_size || 30, cur = d.page || 1;
    const pages = Math.max(1, Math.ceil(total / ps));
    let p = '';
    if (cur > 1) p += `<button class="btn btn-sm" onclick="loadDeliveryLogs(${cur - 1})">上一页</button>`;
    p += `<span class="muted">第 ${cur}/${pages} 页</span>`;
    if (cur < pages) p += `<button class="btn btn-sm" onclick="loadDeliveryLogs(${cur + 1})">下一页</button>`;
    $('#deliveryPager').innerHTML = p;
  } catch (e) {
    wrap.innerHTML = `<tr><td colspan="7" class="empty">加载失败：${esc(e.message)}</td></tr>`;
  }
}

/* ================ 结构化数据 ================ */
let recPage = 1;
async function initRecords() {
  const sel = $('#recTemplate');
  if (!sel.dataset.loaded) {
    const tpls = await req('/templates').catch(() => []);
    sel.innerHTML = '<option value="">全部模板</option>' +
      tpls.map((t) => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('');
    sel.dataset.loaded = '1';
  }
  loadRecords(1);
}

const STYLE_LABEL = { table: '表格', card: '卡片', list: '列表', mixed: '混合' };
async function loadRecords(page = 1) {
  recPage = page;
  const tplName = $('#recTemplate').value;
  const flatten = $('#recFlatten').checked;
  const wrap = $('#recTableWrap');

  if (flatten && !tplName) {
    wrap.innerHTML = '<div class="empty">宽表视图需要先选择一个模板</div>';
    $('#recPager').innerHTML = '';
    return;
  }
  wrap.innerHTML = '<div class="empty">加载中…</div>';

  try {
    await getRoomNameMap();
    if (flatten) {
      const d = await req('/records/flatten?' + qs({ template_name: tplName, page, page_size: 30 }));
      if (!d.rows.length) { wrap.innerHTML = '<div class="empty">暂无数据</div>'; $('#recPager').innerHTML = ''; return; }
      wrap.innerHTML = `<table><thead><tr>
          <th>业务时间</th><th>群名称</th>${d.columns.map((c) => `<th>${esc(c.label)}</th>`).join('')}<th>抽取置信度</th><th>操作</th>
        </tr></thead><tbody>${
          d.rows.map((r) => `<tr>
            <td>${fmtTime(r.__biz_time)}</td><td>${esc(roomName(r.__room_id))}</td>
            ${d.columns.map((c) => `<td class="wrap">${esc(r[c.key] ?? '')}</td>`).join('')}
            <td>${r.__confidence != null ? (r.__confidence * 100).toFixed(0) + '%' : '-'}</td>
            <td><button class="btn btn-sm" onclick="showRecord('${r.__id}')">详情</button> <button class="btn btn-sm btn-warn" data-perm="records:delete" onclick="delRecord('${r.__id}')">删除</button></td>
          </tr>`).join('')}</tbody></table>`;
      renderPager('#recPager', d.total, page, 30, loadRecords);
    } else {
      const d = await req('/records?' + qs({
        template_name: tplName, page, page_size: 20,
        status: $('#recStatus').value, keyword: $('#recKeyword').value,
      }));
      if (!d.items.length) { wrap.innerHTML = '<div class="empty">暂无数据</div>'; $('#recPager').innerHTML = ''; return; }
      wrap.innerHTML = `<table><thead><tr>
          <th>业务时间</th><th>群名称</th><th>模板</th><th>状态</th><th>抽取字段</th><th>抽取置信度</th><th>复核</th><th>操作</th>
        </tr></thead><tbody>${
          d.items.map((r) => `<tr>
            <td>${fmtTime(r.biz_time)}</td><td>${esc(roomName(r.room_id))}</td><td>${esc(r.template_name || '-')} ${r.extract_method === 'vision' ? '<span class="tag tag-vision" style="margin-left:4px">视觉</span>' : ''}</td>
            <td>${tag(r.status)}</td>
            <td class="wrap">${esc(JSON.stringify(r.fields_json || {}).slice(0, 160))}</td>
            <td>${r.confidence != null ? (r.confidence * 100).toFixed(0) + '%' : '-'}</td>
            <td>${r.reviewed ? '✓' : ''}</td>
            <td><button class="btn btn-sm" onclick="showRecord('${r.id}')">详情</button> <button class="btn btn-sm btn-warn" data-perm="records:delete" onclick="delRecord('${r.id}')">删除</button></td>
          </tr>`).join('')}</tbody></table>`;
      renderPager('#recPager', d.total, page, 20, loadRecords);
    }
  } catch (e) { wrap.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

window.showRecord = async function (id) {
  const r = await req('/records/' + id).catch((e) => { toast(e.message, 'err'); return null; });
  if (!r) return;
  const fields = r.fields_json || {};
  // 以模板字段结构为准：定义过的字段即使未抽取到也展示为空白行，避免整行缺失
  const schema = (r.fields_schema || []).filter((f) => f && f.key);
  const schemaKeys = new Set(schema.map((f) => f.key));
  const labelOf = {};
  const typeOf = {};
  schema.forEach((f) => { labelOf[f.key] = f.label || f.key; typeOf[f.key] = f.type || 'string'; });
  // 模板未定义、但实际抽到的额外字段也保留展示
  const extraKeys = Object.keys(fields).filter((k) => !schemaKeys.has(k));
  const allKeys = schema.map((f) => f.key).concat(extraKeys);
  const isArr = (v) => Array.isArray(v);
  const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);

  // 标量字段的可编辑输入框
  function scalarInput(k) {
    const v = fields[k];
    const missing = v === undefined || v === null;
    const val = missing ? '' : (isObj(v) || isArr(v) ? JSON.stringify(v) : String(v));
    return `<input class="fv" data-fk="${esc(k)}" value="${esc(val)}"${missing ? ' placeholder="（未抽取）"' : ''}>`;
  }
  // 数组/对象字段的「展示 + 编辑」块
  function blockInput(k) {
    const v = fields[k];
    if (v === undefined || v === null) return `<textarea class="fv-array" data-fk="${esc(k)}" placeholder="（未抽取）"></textarea>`;
    const json = typeof v === 'string' ? v : JSON.stringify(v, null);
    return `<details class="array-edit"><summary>查看/编辑 JSON</summary><textarea class="fv-array" data-fk="${esc(k)}">${esc(json)}</textarea></details>`;
  }
  function renderArrayTable(arr) {
    if (!Array.isArray(arr) || !arr.length) return '<div class="muted">（空）</div>';
    if (typeof arr[0] !== 'object' || arr[0] === null) {
      return '<ul class="arr-list">' + arr.map((x) => `<li>${esc(String(x))}</li>`).join('') + '</ul>';
    }
    const keys = Array.from(new Set(arr.flatMap((it) => Object.keys(it || {}))));
    return `<table class="sub-table"><thead><tr>${keys.map((kk) => `<th>${esc(kk)}</th>`).join('')}</tr></thead><tbody>${
      arr.map((it) => `<tr>${keys.map((kk) => `<td>${esc(it && it[kk] != null ? it[kk] : '')}</td>`).join('')}</tr>`).join('')
    }</tbody></table>`;
  }
  function structDisplay(k) {
    const v = fields[k];
    if (isArr(v)) return renderArrayTable(v);
    if (isObj(v)) return '<pre class="code">' + esc(JSON.stringify(v, null, 2)) + '</pre>';
    return '';
  }

  // 按模板展示样式组装字段
  const style = (r.display_style || 'card');
  const scalarKeys = allKeys.filter((k) => typeOf[k] !== 'array' && typeOf[k] !== 'object');
  const structKeys = allKeys.filter((k) => typeOf[k] === 'array' || typeOf[k] === 'object');

  let bodyHtml = '';
  if (style === 'table') {
    const rows = allKeys.map((k) => {
      const t = typeOf[k] || 'string';
      if (t === 'array' || t === 'object') {
        return `<tr><td class="fl">${esc(labelOf[k] || k)}</td><td class="fv-cell">${structDisplay(k)}${blockInput(k)}</td></tr>`;
      }
      return `<tr><td class="fl">${esc(labelOf[k] || k)}</td><td>${scalarInput(k)}</td></tr>`;
    }).join('');
    bodyHtml = `<table class="field-table"><tbody>${rows}</tbody></table>`;
  } else if (style === 'list') {
    const rows = allKeys.map((k) => {
      const t = typeOf[k] || 'string';
      if (t === 'array' || t === 'object') {
        return `<div class="list-row"><div class="lr-label">${esc(labelOf[k] || k)}</div>${structDisplay(k)}${blockInput(k)}</div>`;
      }
      return `<div class="list-row"><div class="lr-label">${esc(labelOf[k] || k)}</div>${scalarInput(k)}</div>`;
    }).join('');
    bodyHtml = `<div class="field-list">${rows}</div>`;
  } else if (style === 'mixed') {
    const header = scalarKeys.map((k) => `<div class="card-cell"><div class="cc-label">${esc(labelOf[k] || k)}</div>${scalarInput(k)}</div>`).join('');
    let struct = '';
    structKeys.forEach((k) => { struct += `<h4 class="struct-title">${esc(labelOf[k] || k)}</h4>${structDisplay(k)}${blockInput(k)}`; });
    bodyHtml = `<div class="card-grid">${header}</div>${struct}`;
  } else { // card 默认
    const cards = scalarKeys.map((k) => `<div class="card-cell"><div class="cc-label">${esc(labelOf[k] || k)}</div>${scalarInput(k)}</div>`).join('');
    let struct = '';
    structKeys.forEach((k) => { struct += `<div class="struct-block"><div class="sb-label">${esc(labelOf[k] || k)}</div>${structDisplay(k)}${blockInput(k)}</div>`; });
    bodyHtml = `<div class="card-grid">${cards}</div>${struct}`;
  }

  openDrawer('结构化记录详情', `
    <div class="kv">
      <span class="k">模板</span><span class="v">${esc(r.template_name || '-')}</span>
      <span class="k">样式</span><span class="v"><span class="tag tag-style">${STYLE_LABEL[style] || esc(style)}</span></span>
      <span class="k">场景</span><span class="v">${r.scenario ? `<span class="tag tag-scenario">${esc(r.scenario)}</span>` : '<span class="muted">-</span>'}</span>
      <span class="k">抽取方式</span><span class="v">${r.extract_method === 'vision' ? '<span class="tag tag-vision">视觉直抽</span>' : '<span class="tag tag-ocr">OCR抽取</span>'}</span>
      <span class="k">状态</span><span class="v">${tag(r.status)}</span>
      <span class="k">抽取置信度</span><span class="v">${r.confidence != null ? (r.confidence * 100).toFixed(0) + '%' : '-'}</span>
      <span class="k">模型</span><span class="v">${esc(r.model || '-')}</span>
      <span class="k">耗时</span><span class="v">${r.duration_ms} ms</span>
      <span class="k">业务时间</span><span class="v">${fmtTime(r.biz_time)}</span>
      ${r.error ? `<span class="k">错误</span><span class="v" style="color:#f87171">${esc(r.error)}</span>` : ''}
    </div>
    ${r.extract_warnings && r.extract_warnings.length ? `
    <div class="warn-box">
      <div class="warn-title">⚠ 抽取校验提示（建议人工复核）</div>
      <ul>${r.extract_warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>
    </div>` : ''}
    <h4>字段（可直接修改后保存，视为已复核）</h4>
    ${bodyHtml || '<div class="empty">无字段</div>'}
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" id="drSaveRec" data-perm="records:edit">保存修正</button>
      ${r.attachment_id ? `<button class="btn btn-sm" onclick="showAttachment('${r.attachment_id}')">查看来源附件</button>` : ''}
      <button class="btn btn-sm btn-warn" data-perm="records:delete" onclick="delRecord('${r.id}')">删除</button>
    </div>
    <h4>原始 JSON</h4><pre class="code">${esc(JSON.stringify(fields, null, 2))}</pre>`);

  $('#drSaveRec').onclick = async () => {
    const patch = {};
    const origKeys = new Set(Object.keys(fields));
    $$('#drawerBody [data-fk]').forEach((inp) => {
      const fk = inp.dataset.fk;
      const raw = inp.value;
      // 未抽取且用户未填写的字段不写入，避免凭空注入大量 null
      if (raw === '' && !origKeys.has(fk)) return;
      let v = raw;
      try { if (/^[[{]/.test(v.trim())) v = JSON.parse(v); } catch (e) { /* 保留字符串原样 */ }
      patch[fk] = v === '' ? null : v;
    });
    try {
      await req('/records/' + id, { method: 'PATCH', body: JSON.stringify({ fields_json: patch, reviewed: true }) });
      toast('已保存并标记为已复核', 'ok');
      closeDrawer(); loadRecords(recPage);
    } catch (e) { toast('保存失败：' + e.message, 'err'); }
  };
};

$('#recSearch').onclick = () => loadRecords(1);
$('#recFlatten').onchange = () => loadRecords(1);
$('#recKeyword').onkeydown = (e) => { if (e.key === 'Enter') loadRecords(1); };
$('#recExport').onclick = () => {
  const t = $('#recTemplate').value;
  if (!t) return toast('请先选择要导出的模板', 'err');
  window.open(API + '/records/export?' + qs({ template_name: t }), '_blank');
};

/* ================ 附件 ================ */
async function loadAttachments(page = 1) {
  const tbody = $('#attTable').querySelector('tbody');
  tbody.innerHTML = '<tr><td colspan="9" class="empty">加载中…</td></tr>';
  try {
    await getRoomNameMap();
    const d = await req('/attachments?' + qs({
      page, page_size: 20, keyword: $('#attKeyword').value,
      ocr_status: $('#attOcr').value, extract_status: $('#attExtract').value,
    }));
    tbody.innerHTML = d.items.length ? d.items.map((a) => `<tr>
        <td class="wrap">${esc(a.file_name || '(无名)')}</td>
        <td>${esc(roomName(a.room_id))}</td>
        <td>${esc(a.media_type)}</td><td>${fmtSize(a.file_size)}</td>
        <td>${tag(a.download_status)}</td><td>${tag(a.ocr_status)}</td><td>${tag(a.extract_status)}</td>
        <td>${fmtTime(a.created_at)}</td>
        <td>
          <button class="btn btn-sm" onclick="showAttachment('${a.id}')">详情</button>
          <button class="btn btn-sm" data-perm="attachments:operate" onclick="retryAtt('${a.id}')">重跑</button>
        </td></tr>`).join('')
      : '<tr><td colspan="9" class="empty">暂无附件</td></tr>';
    renderPager('#attPager', d.total, page, 20, loadAttachments);
  } catch (e) { tbody.innerHTML = `<tr><td colspan="9" class="empty">加载失败：${esc(e.message)}</td></tr>`; }
}

window.retryAtt = async function (id) {
  try { await req(`/attachments/${id}/retry?stage=all`, { method: 'POST' }); toast('已加入重跑队列', 'ok'); }
  catch (e) { toast(e.message, 'err'); }
};

window.showAttachment = async function (id) {
  const a = await req('/attachments/' + id).catch((e) => { toast(e.message, 'err'); return null; });
  if (!a) return;
  const ocr = await req(`/attachments/${id}/ocr`).catch(() => null);
  const isImg = /\.(jpg|jpeg|png|bmp|webp|gif|tiff)$/i.test(a.file_name || a.file_ext || '');

  openDrawer('附件详情', `
    <div class="kv">
      <span class="k">文件名</span><span class="v">${esc(a.file_name || '-')}</span>
      <span class="k">类型/大小</span><span class="v">${esc(a.media_type)} · ${fmtSize(a.file_size)}</span>
      <span class="k">下载</span><span class="v">${tag(a.download_status)} ${a.download_error ? esc(a.download_error) : ''}</span>
      <span class="k">OCR</span><span class="v">${tag(a.ocr_status)}</span>
      <span class="k">抽取</span><span class="v">${tag(a.extract_status)}</span>
      <span class="k">本地路径</span><span class="v">${esc(a.local_path || '-')}</span>
    </div>
    <div class="row-btns">
      ${a.local_path ? `<a class="btn btn-sm" href="${API}/attachments/${id}/file" target="_blank">下载原文件</a>` : ''}
      <button class="btn btn-sm" onclick="retryAtt('${id}')">全部重跑</button>
      <button class="btn btn-sm" onclick="retryStage('${id}','ocr')">重跑 OCR</button>
      <button class="btn btn-sm" onclick="retryStage('${id}','extract')">重跑抽取</button>
    </div>
    ${isImg && a.local_path ? `<h4>原图</h4><img src="${API}/attachments/${id}/file">` : ''}
    <h4>OCR 文本 ${ocr ? `（${ocr.text_length} 字 · ${ocr.duration_ms}ms · OCR置信度 ${
      ocr.avg_confidence != null ? (ocr.avg_confidence * 100).toFixed(1) + '%' : '-'}）` : ''}</h4>
    <div class="ocr-text">${ocr ? esc(ocr.text_content || '(空)') : '尚无 OCR 结果'}</div>`);
};

window.retryStage = async function (id, stage) {
  try { await req(`/attachments/${id}/retry?stage=${stage}`, { method: 'POST' }); toast(`已重置 ${stage}`, 'ok'); }
  catch (e) { toast(e.message, 'err'); }
};

$('#attSearch').onclick = () => loadAttachments(1);
$('#attKeyword').onkeydown = (e) => { if (e.key === 'Enter') loadAttachments(1); };
$('#attResetFailed').onclick = async () => {
  if (!confirm('把所有失败的附件重置为待处理？')) return;
  try { const r = await req('/system/pipeline/reset-failed', { method: 'POST' }); toast(r.message, 'ok'); loadAttachments(1); }
  catch (e) { toast(e.message, 'err'); }
};

/* ================ 消息 ================ */
async function loadMessages(page = 1) {
  const tbody = $('#msgTable').querySelector('tbody');
  tbody.innerHTML = '<tr><td colspan="8" class="empty">加载中…</td></tr>';
  try {
    await getRoomNameMap();
    const d = await req('/messages?' + qs({
      page, page_size: 20, keyword: $('#msgKeyword').value,
      msg_type: $('#msgType').value,
      has_attachment: $('#msgHasAtt').checked ? 'true' : '',
    }));
    tbody.innerHTML = d.items.length ? d.items.map((m) => `<tr data-id="${esc(m.id)}">
        <td>${m.seq}</td><td>${fmtTime(m.msg_time)}</td>
        <td>${esc(m.from_name || contactName(m.from_id))}</td><td>${esc(roomName(m.room_id))}</td><td>${esc(m.msg_type)}</td>
        <td class="wrap">${esc((m.content_text || '').slice(0, 120))}</td>
        <td>${m.attachment_count ? `<button class="btn btn-sm" onclick="showMessage('${m.id}')">${m.attachment_count} 个</button>` : '-'}</td>
        <td class="sel-col"><input type="checkbox" class="msg-row-cb" value="${esc(m.id)}"></td>
      </tr>`).join('')
      : '<tr><td colspan="8" class="empty">暂无消息</td></tr>';
    renderPager('#msgPager', d.total, page, 20, loadMessages);
    updateMsgSelectAllState();
    // 外部联系人姓名懒解析：解析到位后二次渲染替换原始 id
    if (d.items.length) {
      const fromIds = d.items.map((m) => m.from_id);
      ensureContacts(fromIds).then(() => {
        tbody.querySelectorAll('tr').forEach((tr, i) => {
          const m = d.items[i];
          if (m && _contactMap && _contactMap.has(m.from_id)) {
            const td = tr.children[2];
            if (td) td.textContent = m.from_name || _contactMap.get(m.from_id);
          }
        });
      });
    }
  } catch (e) { tbody.innerHTML = `<tr><td colspan="8" class="empty">加载失败：${esc(e.message)}</td></tr>`; }
}

function updateMsgSelectAllState() {
  const cbs = [...document.querySelectorAll('.msg-row-cb')];
  const checked = cbs.filter((cb) => cb.checked);
  const all = cbs.length > 0 && checked.length === cbs.length;
  const sa = $('#msgSelectAll');
  if (sa) { sa.checked = all; sa.indeterminate = checked.length > 0 && !all; }
  const btn = $('#msgBatchDel');
  if (btn) btn.disabled = checked.length === 0;
}

window.deleteSelectedMessages = async () => {
  const ids = [...document.querySelectorAll('.msg-row-cb:checked')].map((cb) => cb.value).filter(Boolean);
  if (!ids.length) return;
  if (!confirm(`确认删除选中的 ${ids.length} 条消息？其附件一并删除，风险事件保留（解除关联），不可恢复！`)) return;
  let ok = 0, fail = 0;
  for (const id of ids) {
    try { await req('/messages/' + id, { method: 'DELETE' }); ok++; }
    catch (e) { fail++; console.error('删除消息失败', id, e); }
  }
  toast(`删除完成：成功 ${ok} 条${fail ? '，失败 ' + fail + ' 条' : ''}`, fail ? 'warn' : 'ok');
  loadMessages(1);
};

window.showMessage = async function (id) {
  const m = await req('/messages/' + id).catch((e) => { toast(e.message, 'err'); return null; });
  if (!m) return;
  await ensureContacts([m.from_id]);
  openDrawer('消息详情', `
    <div class="kv">
      <span class="k">seq / msgid</span><span class="v">${m.seq} / ${esc(m.msgid)}</span>
      <span class="k">时间</span><span class="v">${fmtTime(m.msg_time)}</span>
      <span class="k">发送人</span><span class="v">${esc(m.from_name || contactName(m.from_id))}</span>
      <span class="k">群</span><span class="v">${esc(m.room_id || '(单聊)')}</span>
      <span class="k">类型</span><span class="v">${esc(m.msg_type)}</span>
    </div>
    <h4>正文</h4><div class="ocr-text">${esc(m.content_text || '(无)')}</div>
    <h4>附件（${m.attachments.length}）</h4>
    ${m.attachments.map((a) => `<div class="dist-item">
        <span>${esc(a.file_name || a.media_type)} · ${fmtSize(a.file_size)}</span>
        <span>${tag(a.ocr_status)} <button class="btn btn-sm" onclick="showAttachment('${a.id}')">查看</button></span>
      </div>`).join('') || '<div class="dist-item"><span>无</span></div>'}
    <div class="row-btns"><button class="btn btn-sm btn-warn" data-perm="messages:delete" onclick="delMessage('${m.id}')">删除该消息</button></div>
    <h4>原始 JSON</h4><pre class="code">${esc(JSON.stringify(m.raw_json || {}, null, 2))}</pre>`);
};

$('#msgSearch').onclick = () => loadMessages(1);
$('#msgKeyword').onkeydown = (e) => { if (e.key === 'Enter') loadMessages(1); };
$('#msgBatchDel').onclick = () => deleteSelectedMessages();
$('#msgSelectAll').addEventListener('change', (e) => {
  document.querySelectorAll('.msg-row-cb').forEach((cb) => { cb.checked = e.target.checked; });
  updateMsgSelectAllState();
});
$('#msgTable').querySelector('tbody').addEventListener('change', (e) => {
  if (e.target && e.target.classList.contains('msg-row-cb')) updateMsgSelectAllState();
});

/* ================ 模板 ================ */
let editingTplId = null;

async function loadTemplates() {
  const box = $('#tplList');
  box.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const list = await req('/templates');
    box.innerHTML = list.length ? list.map((t) => `
      <div class="tpl-card ${t.enabled ? '' : 'disabled'}">
        <h4>${esc(t.name)}
          ${t.is_fallback ? '<span class="tag tag-skipped">兜底</span>' : ''}
          <span class="tag tag-processing">P${t.priority}</span></h4>
        <div class="desc">${esc(t.description || '')}</div>
        <div class="kw">${(t.match_keywords || []).map((k) => `<span>${esc(k)}</span>`).join('') || '<span>无关键词</span>'}</div>
        <div class="tpl-fields">${(t.fields_schema || []).map((f) => esc(f.label || f.key)).join('、') || '未定义字段'}</div>
        <div class="tpl-actions">
          <button class="btn btn-sm" data-perm="templates:edit" onclick="editTpl('${t.id}')">编辑</button>
          <button class="btn btn-sm" data-perm="templates:edit" onclick="toggleTpl('${t.id}',${!t.enabled})">${t.enabled ? '停用' : '启用'}</button>
          <button class="btn btn-sm btn-warn" data-perm="templates:delete" onclick="delTpl('${t.id}')">删除</button>
        </div>
      </div>`).join('') : '<div class="empty">暂无模板，点「恢复默认模板」</div>';
    box.innerHTML += `<button class="btn btn-sm" data-perm="templates:edit" onclick="seedTpls()" style="margin-top:10px">恢复默认模板</button>`;
  } catch (e) { box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

function fieldRow(f = {}) {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input class="fk" value="${esc(f.key || '')}" placeholder="invoice_no"></td>
    <td><input class="fl" value="${esc(f.label || '')}" placeholder="发票号码"></td>
    <td><select class="ft">${['string', 'number', 'date', 'boolean', 'array', 'object']
        .map((t) => `<option ${f.type === t ? 'selected' : ''}>${t}</option>`).join('')}</select></td>
    <td><input class="fd" value="${esc(f.desc || '')}" placeholder="可选说明"></td>
    <td><button class="btn btn-sm">×</button></td>`;
  tr.querySelector('button').onclick = () => tr.remove();
  return tr;
}

function openTplModal(t) {
  editingTplId = t ? t.id : null;
  $('#modalTitle').textContent = t ? '编辑模板：' + t.name : '新建模板';
  $('#fName').value = t?.name || '';
  $('#fDesc').value = t?.description || '';
  $('#fPriority').value = t?.priority ?? 10;
  $('#fEnabled').checked = t ? t.enabled : true;
  $('#fFallback').checked = t ? t.is_fallback : false;
  $('#fKeywords').value = (t?.match_keywords || []).join(',');
  $('#fExts').value = (t?.match_file_exts || []).join(',');
  $('#fPrompt').value = t?.prompt_extra || '';
  $('#fTestText').value = '';
  $('#fTestResult').textContent = '';
  const tb = $('#fFields');
  tb.innerHTML = '';
  ((t?.fields_schema || []).length ? t.fields_schema : [{}]).forEach((f) => tb.appendChild(fieldRow(f)));
  $('#modalMask').classList.add('show');
}

function collectTpl() {
  const fields = Array.from($('#fFields').children).map((tr) => ({
    key: tr.querySelector('.fk').value.trim(),
    label: tr.querySelector('.fl').value.trim(),
    type: tr.querySelector('.ft').value,
    desc: tr.querySelector('.fd').value.trim() || null,
  })).filter((f) => f.key);
  const split = (s) => s.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
  return {
    name: $('#fName').value.trim(),
    description: $('#fDesc').value.trim() || null,
    enabled: $('#fEnabled').checked,
    priority: parseInt($('#fPriority').value || '0', 10),
    match_keywords: split($('#fKeywords').value),
    match_file_exts: split($('#fExts').value),
    fields_schema: fields.map((f) => ({ ...f, label: f.label || f.key })),
    prompt_extra: $('#fPrompt').value.trim() || null,
    is_fallback: $('#fFallback').checked,
  };
}

$('#tplNew').onclick = () => openTplModal(null);
$('#fAddField').onclick = () => $('#fFields').appendChild(fieldRow());
$('#modalCancel').onclick = () => $('#modalMask').classList.remove('show');
$('#modalMask').onclick = (e) => { if (e.target === $('#modalMask')) $('#modalMask').classList.remove('show'); };

$('#modalSave').onclick = async () => {
  const body = collectTpl();
  if (!body.name) return toast('请填写模板名称', 'err');
  if (!body.fields_schema.length) return toast('至少定义一个字段', 'err');
  try {
    if (editingTplId) await req('/templates/' + editingTplId, { method: 'PATCH', body: JSON.stringify(body) });
    else await req('/templates', { method: 'POST', body: JSON.stringify(body) });
    toast('已保存', 'ok');
    $('#modalMask').classList.remove('show');
    loadTemplates();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
};

$('#fTryRun').onclick = async () => {
  const text = $('#fTestText').value.trim();
  if (!text) return toast('请先粘贴测试文本', 'err');
  $('#fTestResult').textContent = '抽取中（模型推理，通常十几秒）…';
  try {
    const body = { text, template_id: editingTplId };
    const r = await req('/templates/try-run', { method: 'POST', body: JSON.stringify(body) });
    $('#fTestResult').textContent =
      `模板：${r.template_name}（${r.matched_by === 'auto' ? '自动匹配' : '手动指定'}） 耗时 ${r.duration_ms}ms\n` +
      JSON.stringify(r.fields, null, 2) + (r.error ? '\n错误：' + r.error : '');
  } catch (e) { $('#fTestResult').textContent = '失败：' + e.message; }
};

window.editTpl = async (id) => openTplModal(await req('/templates/' + id));
window.toggleTpl = async (id, enabled) => {
  try { await req('/templates/' + id, { method: 'PATCH', body: JSON.stringify({ enabled }) }); loadTemplates(); }
  catch (e) { toast(e.message, 'err'); }
};
window.delTpl = async (id) => {
  if (!confirm('确认删除该模板？已产生数据的模板会自动改为停用。')) return;
  try { const r = await req('/templates/' + id, { method: 'DELETE' }); toast(r.message, 'ok'); loadTemplates(); }
  catch (e) { toast(e.message, 'err'); }
};
$('#tplSeed').onclick = async () => {
  try { const r = await req('/templates/seed', { method: 'POST' }); toast(r.message, 'ok'); loadTemplates(); }
  catch (e) { toast(e.message, 'err'); }
};

/* ================ 系统 ================ */
async function loadSystem() {
  try {
    const h = await req('/system/health');
    const dot = (ok) => `<span class="tag tag-${ok ? 'done' : 'failed'}">${ok ? '正常' : '异常'}</span>`;
    const conns = (h.llm && h.llm.connections) || [];
    const connHtml = conns.length ? conns.map((c) => {
      const roleTag = (c.roles || []).map((r) => ROLE_LABEL[r] || r).join('、') || '（无）';
      const flags = [c.enabled ? '' : '已停用', c.has_model ? '' : '缺模型', c.has_key ? '' : '缺密钥'].filter(Boolean).join(' · ');
      return `<div class="kv-row" style="padding:3px 0">
        <span class="kv-k">${dot(c.ok)} ${esc(c.name)}</span>
        <span class="kv-v">${esc(c.provider)} · <b>${esc(c.model || '-')}</b>　角色：${esc(roleTag)}${flags ? '　<span class="muted">' + esc(flags) + '</span>' : ''}</span>
      </div>`;
    }).join('') : '<div class="empty">无模型连接</div>';
    $('#healthBox').innerHTML = `<div class="kv">
      <span class="k">数据库</span><span class="v">${dot(h.database.ok)} ${esc(h.database.dialect || h.database.error || '')}</span>
      <span class="k">采集器</span><span class="v">${dot(h.collector.ok)} ${esc(h.collector.mode)} — ${esc(h.collector.detail || '')}</span>
      <span class="k">OCR 引擎</span><span class="v">${dot(h.ocr.ok)} ${esc(h.ocr.detail || h.ocr.engine || '')}</span>
      <span class="k">模型连接</span><span class="v">${dot(h.llm.ok)} 共 ${conns.length} 条（已启用 ${conns.filter((c) => c.enabled).length}）</span>
    </div>
    <div style="margin-top:8px"><b>模型连接明细</b><div id="connList" style="margin-top:4px">${connHtml}</div></div>`;

    const s = h.scheduler;
    const lr = s.last_run || {};
    $('#schedBox').innerHTML = `<div class="kv">
      <span class="k">调度状态</span><span class="v">${s.running ? '<span class="tag tag-done">运行中</span>' : '<span class="tag tag-skipped">未运行</span>'}</span>
      ${(s.jobs || []).map((j) => `<span class="k">${esc(j.name)}</span><span class="v">下次 ${fmtTime(j.next_run)}</span>`).join('')}
      <span class="k">上次同步</span><span class="v">${lr.sync?.at ? fmtTime(lr.sync.at) + (lr.sync.ok ? ' ✓' : ' ✗ ' + esc(lr.sync.error || '')) : '未运行'}</span>
      <span class="k">上次流水线</span><span class="v">${lr.pipeline?.at ? fmtTime(lr.pipeline.at) + (lr.pipeline.ok ? ' ✓' : ' ✗ ' + esc(lr.pipeline.error || '')) : '未运行'}</span>
    </div>`;

    $('#configBox').textContent = JSON.stringify(await req('/system/config'), null, 2);
  } catch (e) { toast('加载系统信息失败：' + e.message, 'err'); }
}

/* ================ 企业微信接口配置 ================ */
async function loadWeComConfig() {
  try {
    const c = await req('/wecom-config');
    $('#wcMode').value = c.mode || 'mock';
    $('#wcFetchLimit').value = c.fetch_limit ?? 500;
    $('#wcCorpId').value = c.corp_id || '';
    $('#wcSecret').value = c.archive_secret || '';
    $('#wcCustomerSecret').value = c.customer_contact_secret || '';
    $('#wcSdkPath').value = c.sdk_path || '';
    $('#wcTimeout').value = c.sdk_timeout ?? 30;
    $('#wcKey').value = c.private_key_content || '';
    $('#wcProxy').value = c.proxy || '';
    $('#wcProxyPass').value = c.proxy_passwd || '';
    $('#wcAgentId').value = c.agent_id || '';
    $('#wcAgentSecret').value = c.agent_secret || '';
    $('#wcApiBase').value = c.api_base_url || 'https://qyapi.weixin.qq.com';
    $('#wcFilterRooms').value = c.filter_room_ids || '';
    $('#wcOnlyGroup').checked = c.only_group_chat !== false;
    $('#wecomSrcHint').textContent = c.source === 'db' ? '（已保存）' : '（当前来自环境变量，保存后生效）';
    $('#wcSavedAt').textContent = c.updated_at ? '上次保存：' + c.updated_at : '';
  } catch (e) { toast('加载企业微信配置失败：' + e.message, 'err'); }
}

// ---------------------------------------------------------------- 系统设置（标准化通知投递）
async function loadSysSettings() {
  try {
    const [smtp, app] = await Promise.all([req('/smtp-config'), req('/wecom-app-config')]);
    $('#smtpHost').value = smtp.host || '';
    $('#smtpPort').value = smtp.port || 465;
    $('#smtpUser').value = smtp.user || '';
    $('#smtpFrom').value = smtp.from || '';
    $('#smtpTls').checked = smtp.tls !== false;
    $('#smtpPass').value = '';
    $('#smtpPass').placeholder = smtp.has_pass ? '已保存，留空=不修改' : '留空=不修改';
    const smtpOk = !!(smtp.host && smtp.user && smtp.has_pass);
    $('#smtpState').className = 'badge ' + (smtpOk ? 'ok' : 'warn');
    $('#smtpState').textContent = smtpOk ? '已配置' : '未配置';
    $('#appCorp').value = app.corp_id || '';
    $('#appAgent').value = app.agent_id || '';
    $('#appSecret').value = '';
    $('#appSecret').placeholder = app.has_secret ? '已保存，留空=不修改' : '留空=不修改';
    const appOk = !!(app.corp_id && app.agent_id && app.has_secret);
    $('#appState').className = 'badge ' + (appOk ? 'ok' : 'warn');
    $('#appState').textContent = appOk ? '已配置' : '未配置';
  } catch (e) { toast('加载系统设置失败：' + e.message, 'err'); }
}

function _smtpBody() {
  return {
    host: $('#smtpHost').value.trim(),
    port: parseInt($('#smtpPort').value, 10) || 465,
    user: $('#smtpUser').value.trim(),
    pass_field: $('#smtpPass').value,
    from_addr: $('#smtpFrom').value.trim(),
    tls: $('#smtpTls').checked,
  };
}
function _validateSmtp(body, forTest) {
  const EMAIL_RE = /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/;
  const EMAIL_WITH_NAME_RE = /^(.+?)\s*<([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)>\s*$/;
  function isFromOk(v) {
    if (!v) return true;
    return EMAIL_RE.test(v) || EMAIL_WITH_NAME_RE.test(v);
  }
  if (!body.host) return '请填写 SMTP 主机';
  if (!body.user) return '请填写发件账号';
  if (forTest && !$('#smtpTestTo').value.trim()) return '请先填写测试收件人';
  if (body.from_addr && !isFromOk(body.from_addr)) return '发件人格式不正确，请填写邮箱或“显示名 <邮箱>”';
  return '';
}
function _appBody() {
  return {
    corp_id: $('#appCorp').value.trim(),
    agent_id: $('#appAgent').value.trim(),
    agent_secret: $('#appSecret').value,
  };
}

async function _saveThenTest(channel, target, saveUrl, saveBody, msgSel, btnSel) {
  $(btnSel).disabled = true;
  try {
    await req(saveUrl, { method: 'PUT', body: JSON.stringify(saveBody) });
  } catch (e) {
    $(msgSel).textContent = '保存失败：' + e.message;
    $(msgSel).style.color = '#dc2626';
    return;
  }
  try {
    const r = await req('/delivery-config/test', { method: 'POST', body: JSON.stringify({ channel, target }) });
    $(msgSel).textContent = (r.ok ? '✅ ' : '❌ ') + (r.detail || '');
    $(msgSel).style.color = r.ok ? '#07c160' : '#dc2626';
    loadSysSettings();
  } catch (e) {
    $(msgSel).textContent = '测试请求失败：' + e.message;
    $(msgSel).style.color = '#dc2626';
  } finally {
    $(btnSel).disabled = false;
  }
}

$('#smtpSave').onclick = async () => {
  const body = _smtpBody();
  const err = _validateSmtp(body, false);
  if (err) { toast(err, 'err'); return; }
  $('#smtpSave').disabled = true;
  try {
    const r = await req('/smtp-config', { method: 'PUT', body: JSON.stringify(body) });
    toast(r.message || '已保存', 'ok');
    $('#smtpPass').value = '';
    loadSysSettings();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
  finally { $('#smtpSave').disabled = false; }
};
$('#smtpTest').onclick = async () => {
  const body = _smtpBody();
  const err = _validateSmtp(body, true);
  if (err) { $('#smtpMsg').textContent = err; $('#smtpMsg').style.color = '#dc2626'; return; }
  await _saveThenTest('email', $('#smtpTestTo').value.trim(), '/smtp-config', body, '#smtpMsg', '#smtpTest');
};
$('#appSave').onclick = async () => {
  $('#appSave').disabled = true;
  try {
    const r = await req('/wecom-app-config', { method: 'PUT', body: JSON.stringify(_appBody()) });
    toast(r.message || '已保存', 'ok');
    $('#appSecret').value = '';
    loadSysSettings();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
  finally { $('#appSave').disabled = false; }
};
$('#appTest').onclick = async () => {
  const to = $('#appTestTo').value.trim();
  if (!to) { $('#appMsg').textContent = '请先填写测试接收人（userid 或 party:部门ID）'; $('#appMsg').style.color = '#dc2626'; return; }
  await _saveThenTest('app', to, '/wecom-app-config', _appBody(), '#appMsg', '#appTest');
};
$('#whTest').onclick = async () => {
  const url = $('#whUrl').value.trim();
  if (!url) { $('#whMsg').textContent = '请填写 Webhook 地址'; $('#whMsg').style.color = '#dc2626'; return; }
  $('#whTest').disabled = true;
  try {
    const r = await req('/delivery-config/test', { method: 'POST', body: JSON.stringify({ channel: 'webhook', target: url }) });
    $('#whMsg').textContent = (r.ok ? '✅ ' : '❌ ') + (r.detail || '');
    $('#whMsg').style.color = r.ok ? '#07c160' : '#dc2626';
  } catch (e) { $('#whMsg').textContent = '测试请求失败：' + e.message; $('#whMsg').style.color = '#dc2626'; }
  finally { $('#whTest').disabled = false; }
};

// 系统设置页密码/Secret 显示切换
document.querySelectorAll('.toggle-eye').forEach((btn) => {
  btn.addEventListener('click', () => {
    const input = $('#' + btn.dataset.eyeFor);
    if (!input) return;
    const isHidden = input.type === 'password';
    input.type = isHidden ? 'text' : 'password';
    btn.textContent = isHidden ? '🙈' : '👁';
  });
});

$('#wcShowKey').onchange = (e) => { $('#wcKey').type = e.target.checked ? 'text' : 'password'; };
$('#wcSave').onclick = async () => {
  const body = {
    mode: $('#wcMode').value,
    corp_id: $('#wcCorpId').value.trim(),
    archive_secret: $('#wcSecret').value,
    customer_contact_secret: $('#wcCustomerSecret').value,
    sdk_path: $('#wcSdkPath').value.trim(),
    private_key_content: $('#wcKey').value,
    private_key_path: '',
    proxy: $('#wcProxy').value.trim(),
    proxy_passwd: $('#wcProxyPass').value,
    sdk_timeout: parseInt($('#wcTimeout').value, 10) || 30,
    fetch_limit: parseInt($('#wcFetchLimit').value, 10) || 500,
    agent_id: $('#wcAgentId').value.trim(),
    agent_secret: $('#wcAgentSecret').value,
    only_group_chat: $('#wcOnlyGroup').checked,
    filter_room_ids: $('#wcFilterRooms').value.trim(),
    api_base_url: $('#wcApiBase').value.trim(),
  };
  $('#wcSave').disabled = true;
  try {
    const r = await req('/wecom-config', { method: 'PUT', body: JSON.stringify(body) });
    toast(r.message || '已保存', 'ok');
    loadWeComConfig();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
  finally { $('#wcSave').disabled = false; }
};

$('#wcVerify').onclick = async () => {
  const corpId = $('#wcCorpId').value.trim();
  const secret = $('#wcAgentSecret').value;
  const customerSecret = $('#wcCustomerSecret').value;
  const base = $('#wcApiBase').value.trim();
  if (!corpId || (!secret && !customerSecret)) {
    $('#wcVerifyMsg').textContent = '请先填写 Corp ID 与（应用/客户联系）Secret';
    $('#wcVerifyMsg').style.color = '#dc2626';
    return;
  }
  $('#wcVerify').disabled = true;
  $('#wcVerifyMsg').textContent = '验证中…';
  $('#wcVerifyMsg').style.color = '#94a3b8';
  try {
    const r = await req('/wecom-config/verify', {
      method: 'POST',
      body: JSON.stringify({ corp_id: corpId, archive_secret: $('#wcSecret').value, agent_secret: secret, customer_contact_secret: customerSecret, api_base_url: base }),
    });
    const parts = [];
    if (r.archive) {
      parts.push(r.archive.ok
        ? `✓ 存档/应用凭证有效（token ${r.archive.token_masked}，有效期 ${r.archive.expires_in}s）`
        : `✗ 存档/应用凭证失败 errcode=${r.archive.errcode}：${r.archive.errmsg}`);
    }
    if (r.customer_contact) {
      parts.push(r.customer_contact.ok
        ? `✓ 客户联系凭证有效（token ${r.customer_contact.token_masked}，有效期 ${r.customer_contact.expires_in}s）`
        : `✗ 客户联系凭证失败 errcode=${r.customer_contact.errcode}：${r.customer_contact.errmsg}`);
    }
    if (!r.archive && !r.customer_contact) {
      if (r.ok) parts.push(`✓ 凭证有效（token ${r.token_masked}，有效期 ${r.expires_in}s）`);
      else parts.push(`✗ 失败 errcode=${r.errcode}：${r.errmsg}`);
    }
    $('#wcVerifyMsg').textContent = parts.join('　|　');
    $('#wcVerifyMsg').style.color = parts.every((p) => p.startsWith('✓')) ? '#16a34a' : '#dc2626';
  } catch (e) {
    $('#wcVerifyMsg').textContent = '验证请求异常：' + e.message;
    $('#wcVerifyMsg').style.color = '#dc2626';
  }
  finally { $('#wcVerify').disabled = false; }
};

$('#wcPermitUsers').onclick = async () => {
  const box = $('#wcPermitBox');
  const msg = $('#wcPermitMsg');
  $('#wcPermitUsers').disabled = true;
  msg.textContent = '拉取中…';
  msg.style.color = '#94a3b8';
  try {
    const r = await req('/wecom/permit-users');
    msg.textContent = `✓ 已拉取 ${r.count} 个已授权存档成员`;
    msg.style.color = '#16a34a';
    box.innerHTML = (r.userlist || []).slice(0, 50).map((u) =>
      `<div class="dist-item"><span>${esc(u.userid || u.open_id || '-')}</span><span class="muted">${esc((u.type == 1 ? '成员' : '企业') + (u.permission == 1 ? '·已授权' : ''))}</span></div>`
    ).join('') || '<div class="dist-item"><span>无</span></div>';
    if (r.count > 50) box.innerHTML += `<div class="dist-item"><span class="muted">…其余 ${r.count - 50} 条已省略</span></div>`;
  } catch (e) {
    msg.textContent = '拉取失败：' + e.message;
    msg.style.color = '#dc2626';
    box.innerHTML = '';
  }
  finally { $('#wcPermitUsers').disabled = false; }
};

$('#wcQuitList').onclick = async () => {
  const box = $('#wcQuitBox');
  const msg = $('#wcQuitMsg');
  $('#wcQuitList').disabled = true;
  msg.textContent = '拉取中…';
  msg.style.color = '#94a3b8';
  try {
    const r = await req('/wecom/quit-list');
    msg.textContent = `✓ 已拉取 ${r.count} 个离职需转接成员`;
    msg.style.color = '#16a34a';
    box.innerHTML = (r.ids || []).slice(0, 50).map((u) =>
      `<div class="dist-item"><span>${esc(u)}</span><span class="muted">离职待转接</span></div>`
    ).join('') || '<div class="dist-item"><span>无离职待转接成员</span></div>';
    if (r.count > 50) box.innerHTML += `<div class="dist-item"><span class="muted">…其余 ${r.count - 50} 条已省略</span></div>`;
  } catch (e) {
    msg.textContent = '拉取失败：' + e.message;
    msg.style.color = '#dc2626';
    box.innerHTML = '';
  }
  finally { $('#wcQuitList').disabled = false; }
};

$('#wcExternalGroup').onclick = async () => {
  const roomid = $('#wcExternalRoomid').value.trim();
  const box = $('#wcExternalBox');
  const msg = $('#wcExternalMsg');
  if (!roomid) { msg.textContent = '请填写外部群 roomid'; msg.style.color = '#dc2626'; return; }
  $('#wcExternalGroup').disabled = true;
  msg.textContent = '拉取中…';
  msg.style.color = '#94a3b8';
  try {
    const r = await req('/wecom/external-groupchat/' + encodeURIComponent(roomid) + '?customer_contact_secret=' + encodeURIComponent($('#wcCustomerSecret').value));
    msg.textContent = `✓ 已拉取外部群「${r.name || r.room_id}」(${r.member_count} 人)`;
    msg.style.color = '#16a34a';
    // 解析成员 / 群主中的外部联系人姓名（wo/wm 开头），再渲染
    await ensureContacts([...(r.members || []).map((m) => m.userid), r.owner]);
    const members = (r.members || []).map((m) =>
      `<div class="dist-item"><span>${esc(contactName(m.userid) || m.userid || '-')}</span><span class="muted">${esc(m.type == 2 ? '外部联系人' : '企业成员')}</span></div>`
    ).join('') || '<div class="dist-item"><span>无成员</span></div>';
    const admins = (r.admins || []).length ? `<div class="dist-item"><span class="muted">群管理员：${esc((r.admins || []).map((a) => contactName(a) || a).join(', '))}</span></div>` : '';
    box.innerHTML = `<div class="dist-item"><span>群主：${esc(contactName(r.owner) || r.owner || '-')}</span></div>` + members + admins;
  } catch (e) {
    msg.textContent = '拉取失败：' + e.message;
    msg.style.color = '#dc2626';
    box.innerHTML = '';
  }
  finally { $('#wcExternalGroup').disabled = false; }
};

$('#wcTransfer').onclick = async () => {
  const handover = $('#wcHandover').value.trim();
  const takeover = $('#wcTakeover').value.trim();
  const msg = $('#wcTransferMsg');
  if (!handover || !takeover) { msg.textContent = '请填写离职成员与接管成员 userid'; msg.style.color = '#dc2626'; return; }
  if (handover === takeover) { msg.textContent = '离职成员与接管成员不能相同'; msg.style.color = '#dc2626'; return; }
  $('#wcTransfer').disabled = true;
  msg.textContent = '转接中…';
  msg.style.color = '#94a3b8';
  try {
    const r = await req('/wecom/transfer', { method: 'POST', body: JSON.stringify({ handover_userid: handover, takeover_userid: takeover }) });
    msg.textContent = `✓ 已转接 ${r.handover_userid} → ${r.takeover_userid}`;
    msg.style.color = '#16a34a';
  } catch (e) {
    msg.textContent = '转接失败：' + e.message;
    msg.style.color = '#dc2626';
  }
  finally { $('#wcTransfer').disabled = false; }
};

$('#wcSingleAgree').onclick = async () => {
  const userid = $('#wcAgreeUserid').value.trim();
  const roomids = $('#wcAgreeRoomids').value.split(',').map((s) => s.trim()).filter(Boolean);
  const box = $('#wcAgreeBox');
  const msg = $('#wcAgreeMsg');
  if (!userid) { msg.textContent = '请填写员工 userid'; msg.style.color = '#dc2626'; return; }
  if (!roomids.length) { msg.textContent = '请填写至少一个会话 roomid'; msg.style.color = '#dc2626'; return; }
  $('#wcSingleAgree').disabled = true;
  msg.textContent = '查询中…';
  msg.style.color = '#94a3b8';
  try {
    const r = await req('/wecom/single-agree', {
      method: 'POST',
      body: JSON.stringify({ userid, roomids }),
    });
    msg.textContent = `✓ 查询到 ${r.count} 个会话`;
    msg.style.color = '#16a34a';
    box.innerHTML = (r.agree_status || []).map((it) =>
      `<div class="dist-item"><span>${esc(it.roomid || '-')}</span><span class="muted">${esc(it.status_text || ('状态' + it.status))}</span></div>`
    ).join('') || '<div class="dist-item"><span>无结果</span></div>';
  } catch (e) {
    msg.textContent = '查询失败：' + e.message;
    msg.style.color = '#dc2626';
    box.innerHTML = '';
  }
  finally { $('#wcSingleAgree').disabled = false; }
};

$('#btnPause').onclick = async () => { try { toast((await req('/system/scheduler/pause', { method: 'POST' })).message, 'ok'); loadSystem(); } catch (e) { toast(e.message, 'err'); } };
$('#btnResume').onclick = async () => { try { toast((await req('/system/scheduler/resume', { method: 'POST' })).message, 'ok'); loadSystem(); } catch (e) { toast(e.message, 'err'); } };
$('#btnReloadCollector').onclick = async () => { try { toast((await req('/system/collector/reload', { method: 'POST' })).message, 'ok'); loadSystem(); } catch (e) { toast(e.message, 'err'); } };

$('#btnTestModels').onclick = async function () {
  this.disabled = true; this.textContent = '测试中…';
  const box = document.getElementById('connList');
  try {
    const cfg = await req('/system/config');
    const conns = (cfg.models || []).filter((m) => m.enabled);
    if (!conns.length) { toast('没有已启用的连接', 'err'); return; }
    for (const m of conns) {
      try {
        const t = await req('/models/' + encodeURIComponent(m.id) + '/test', { method: 'POST' });
        const d = (t && t.data) || {};
        const ok = d.reachable && d.sample_ok;
        if (box) box.insertAdjacentHTML('beforeend', `<div class="kv-row" style="padding:2px 0"><span class="kv-k">${ok ? '✅' : '❌'} ${esc(m.name)}</span><span class="kv-v">${esc((d.reachable ? '连通 ' + (d.latency_ms ?? '?') + 'ms' : '不可达'))}${d.sample_ok ? ' · 样例正常' : (d.model ? ' · 样例失败' : '')}${d.error ? ' · ' + esc(d.error) : ''}</span></div>`);
      } catch (e) {
        if (box) box.insertAdjacentHTML('beforeend', `<div class="kv-row" style="padding:2px 0"><span class="kv-k">❌ ${esc(m.name)}</span><span class="kv-v">测试异常：${esc(e.message)}</span></div>`);
      }
    }
    toast('已测试 ' + conns.length + ' 条已启用连接', 'ok');
  } catch (e) { toast('测试失败：' + e.message, 'err'); }
  finally { this.disabled = false; this.textContent = '测试所有连接'; }
};

/* ================ 顶栏动作 ================ */
$('#btnSync').onclick = async function () {
  this.disabled = true; this.textContent = '同步中…';
  try {
    const r = await req('/system/sync?wait=true', { method: 'POST' });
    const d = r.data || {};
    toast(`同步完成：新增 ${d.saved || 0} 条消息 / ${d.attachments || 0} 个附件`, 'ok');
    loadDashboard();
  } catch (e) { toast('同步失败：' + e.message, 'err'); }
  finally { this.disabled = false; this.textContent = '立即同步'; }
};

$('#btnRun').onclick = async function () {
  this.disabled = true; this.textContent = '已提交';
  try { toast((await req('/system/pipeline/run?wait=false', { method: 'POST' })).message + '（OCR+模型推理较慢，稍后刷新查看）', 'ok'); }
  catch (e) { toast(e.message, 'err'); }
  finally { setTimeout(() => { this.disabled = false; this.textContent = '跑一轮流水线'; }, 1500); }
};

/* 群采集批量开关 */
$('#roomEnableAll').onclick = () => batchToggleRooms(true);
$('#roomDisableAll').onclick = () => batchToggleRooms(false);

/* ================ 抽屉 / 分页 ================ */
function openDrawer(title, html) {
  $('#drawerTitle').textContent = title;
  $('#drawerBody').innerHTML = html;
  $('#drawer').classList.add('show');
  $('#drawerMask').classList.add('show');
}
function closeDrawer() {
  $('#drawer').classList.remove('show');
  $('#drawerMask').classList.remove('show');
}
$('#drawerClose').onclick = closeDrawer;
$('#drawerMask').onclick = closeDrawer;
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  closeDrawer();
  $('#modalMask').classList.remove('show');
});

function renderPager(sel, total, page, size, fn) {
  const pages = Math.max(1, Math.ceil(total / size));
  $(sel).innerHTML = `
    <span>共 ${total} 条 · 第 ${page}/${pages} 页</span>
    <button class="btn btn-sm" ${page <= 1 ? 'disabled' : ''} id="pgPrev">上一页</button>
    <button class="btn btn-sm" ${page >= pages ? 'disabled' : ''} id="pgNext">下一页</button>`;
  const prev = $(sel + ' #pgPrev'); const next = $(sel + ' #pgNext');
  if (prev) prev.onclick = () => fn(page - 1);
  if (next) next.onclick = () => fn(page + 1);
}

/* ================ 风险预警 ================ */
let riskPage = 1;
function applyRiskFilter(opts) {
  $('#riskSev').value = opts.severity || '';
  $('#riskStatus').value = opts.status || '';
  $('#riskAlertStatus').value = opts.alert_status || '';
  $('#riskKeyword').value = opts.keyword || '';
  loadRisks(1);
}

function applyDeliveryFilter(opts) {
  $('#dlStatus').value = opts.status || '';
  $('#dlChannel').value = opts.channel || '';
  goView('risks', 'risk-delivery');
}

/* 切换到指定主视图（及子标签），并触发对应加载器 */
function goView(view, sub) {
  const tab = document.querySelector(`.tab[data-view="${view}"]`);
  if (!tab) return;
  if (!tab.classList.contains('active')) tab.click();
  if (sub) {
    const subBtn = document.querySelector(`#view-${view} .subtab[data-sub="${sub}"]`);
    if (subBtn) {
      if (!subBtn.classList.contains('active')) subBtn.click();
      else { const L = SUB_LOADERS[sub]; L && L(); }
    }
  }
}

async function loadRisks(page = 1) {
  riskPage = page;
  const wrap = $('#riskTable').querySelector('tbody');
  wrap.innerHTML = '<tr><td colspan="10" class="empty">加载中…</td></tr>';
  try {
    await getRoomNameMap();
    const d = await req('/risks/events?' + qs({
      page, page_size: 20, severity: $('#riskSev').value,
      status: $('#riskStatus').value, alert_status: $('#riskAlertStatus').value,
      keyword: $('#riskKeyword').value,
    }));
    if (!d.items.length) {
      wrap.innerHTML = '<tr><td colspan="10" class="empty">暂无风险事件，下一轮扫描会自动研判新消息</td></tr>';
      $('#riskPager').innerHTML = '';
    } else {
      wrap.innerHTML = d.items.map((r) => `<tr>
        <td>${fmtTime(r.created_at)}</td>
        <td>${esc(r.category)}</td>
        <td>${sevTag(r.severity)}</td>
        <td>${esc(roomName(r.room_id))}</td>
        <td>${esc(r.from_id || '-')}</td>
        <td class="wrap">${esc((r.snippet || '').slice(0, 80))}</td>
        <td>${r.detection_method === 'llm' ? '<span class="tag tag-processing">模型研判</span>' : '<span class="tag tag-done">关键词</span>'}</td>
        <td>${alertTag(r.alert_status)}</td>
        <td>${tag(r.status)}</td>
        <td>
          <button class="btn btn-sm" onclick="showRisk('${r.id}')">详情</button>
          <button class="btn btn-sm" onclick="ackRisk('${r.id}')">确认</button>
        </td></tr>`).join('');
      renderPager('#riskPager', d.total, page, 20, loadRisks);
    }
    const s = await req('/risks/stats');
    const cards = [
      { l: '风险事件', n: s.total, f: {} },
      { l: '待处置', n: s.pending, f: { status: 'pending' } },
      { l: '严重', n: s.by_severity.critical || 0, f: { severity: 'critical' } },
      { l: '高', n: s.by_severity.high || 0, f: { severity: 'high' } },
      { l: '中', n: s.by_severity.medium || 0, f: { severity: 'medium' } },
      { l: '预警失败', n: (s.by_alert_status.failed || 0) + (s.by_alert_status.partial || 0), f: { alert_status: 'failed,partial' } },
    ];
    $('#riskStatCards').innerHTML = cards.map(({ l, n, f }) => {
      const attrs = Object.entries(f).map(([k, v]) => `data-${k}="${v}"`).join(' ');
      return `<div class="card stat-card" ${attrs}><div class="num">${n}</div><div class="lbl">${l}</div></div>`;
    }).join('');
    $('#riskStatCards').querySelectorAll('.stat-card').forEach((c) => {
      c.onclick = () => applyRiskFilter({
        severity: c.dataset.severity || '',
        status: c.dataset.status || '',
        alert_status: c.dataset.alert_status || '',
      });
    });
  } catch (e) { wrap.innerHTML = `<tr><td colspan="10" class="empty">加载失败：${esc(e.message)}</td></tr>`; }
}

window.ackRisk = async function (id) {
  try {
    await req('/risks/events/' + id + '/acknowledge', { method: 'POST', body: JSON.stringify({ reviewer: 'web' }) });
    toast('已确认', 'ok'); loadRisks(riskPage);
  } catch (e) { toast(e.message, 'err'); }
};

window.showRisk = async function (id) {
  const r = await req('/risks/events/' + id).catch((e) => { toast(e.message, 'err'); return null; });
  if (!r) return;
  const logs = await req('/risks/events/' + id + '/logs').catch(() => []);
  openDrawer('风险事件详情', `
    <div class="kv">
      <span class="k">分类</span><span class="v">${esc(r.category)}</span>
      <span class="k">严重度</span><span class="v">${sevTag(r.severity)}</span>
      <span class="k">群</span><span class="v">${esc(roomName(r.room_id))}</span>
      <span class="k">发送人</span><span class="v">${esc(r.from_id || '-')}</span>
      <span class="k">引擎</span><span class="v">${r.detection_method === 'llm' ? '模型研判' : '关键词'}${r.matched_keyword ? '（' + esc(r.matched_keyword) + '）' : ''}</span>
      <span class="k">预警状态</span><span class="v">${alertTag(r.alert_status)}</span>
      <span class="k">状态</span><span class="v">${tag(r.status)}</span>
    </div>
    <h4>命中内容</h4><div class="ocr-text">${esc(r.snippet || '(模型语义命中)')}</div>
    ${r.detail ? `<h4>研判</h4><div class="ocr-text">${esc(r.detail)}</div>` : ''}
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" id="rkAck">确认处置</button>
      <button class="btn btn-sm" id="rkResend">重发预警</button>
    </div>
    <h4>投递回执</h4>
    <div class="table-wrap"><table><thead><tr><th>通道</th><th>目标</th><th>状态</th><th>详情</th></tr></thead><tbody>${
      (logs && logs.length) ? logs.map((l) => `<tr><td><span class="ch ch-${esc(l.channel)}">${esc(CH_LABEL[l.channel] || l.channel)}</span></td><td class="wrap">${esc(l.target)}</td><td>${l.status === 'sent' ? '<span class="badge badge-on">✅ 送达</span>' : '<span class="badge badge-warn">❌ 失败</span>'}</td><td class="wrap">${esc(l.detail || '')}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">无</td></tr>'
    }</tbody></table></div>`);
  $('#rkAck').onclick = async () => { try { await req('/risks/events/' + id + '/acknowledge', { method: 'POST', body: JSON.stringify({ reviewer: 'web' }) }); toast('已确认', 'ok'); closeDrawer(); loadRisks(riskPage); } catch (e) { toast(e.message, 'err'); } };
  $('#rkResend').onclick = async () => { try { const rr = await req('/risks/events/' + id + '/resend', { method: 'POST' }); toast(rr.message, 'ok'); closeDrawer(); loadRisks(riskPage); } catch (e) { toast(e.message, 'err'); } };
};

$('#riskSearch').onclick = () => loadRisks(1);
$('#riskKeyword').onkeydown = (e) => { if (e.key === 'Enter') loadRisks(1); };
$('#dlSearch').onclick = () => loadDeliveryLogs(1);
$('#dlStatus').onchange = () => loadDeliveryLogs(1);
$('#dlChannel').onchange = () => loadDeliveryLogs(1);
$('#riskRescan').onclick = async () => {
  if (!confirm('把全部已扫消息重置为待扫描，下一轮风险作业将重扫（已发预警可能重复）？')) return;
  try { const r = await req('/risks/rescan', { method: 'POST', body: JSON.stringify({}) }); toast(r.message, 'ok'); loadRisks(1); }
  catch (e) { toast(e.message, 'err'); }
};

/* ================ 风控配置 ================ */
let editingRuleId = null;
let _layersCache = [];
const CH_LABEL = { webhook: '群机器人', app: '应用消息', email: '邮件', system: '系统内' };
const TARGET_CHANNELS = {
  webhook: { targetLabel: '目标（Webhook 地址）', placeholder: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...', hint: '群机器人 Webhook 须以 https:// 开头，可在企微群「群机器人」设置里获取。' },
  app: { targetLabel: '目标（userid 或 party:部门ID）', placeholder: 'zhangsan 或 party:2', hint: '填 userid（推给个人）或 party:部门ID（推给部门），多个用逗号分隔。' },
  email: { targetLabel: '目标（邮箱地址）', placeholder: 'risk@example.com', hint: '接收邮箱地址，多个用逗号分隔。' },
  system: { targetLabel: '目标（无需填写）', placeholder: '无需填写', hint: '系统内通知自动送达风险事件页（红点），无需填写目标。' },
};
// 投递目标状态徽标：已启用 / 已停用 / 未配置或占位
function targetBadge(t) {
  if (!t.enabled) return '<span class="badge badge-off">已停用</span>';
  if (t.channel === 'system') return '<span class="badge badge-on">● 自动送达</span>';
  const empty = !t.target || !t.target.trim()
    || (t.channel === 'webhook' && !t.target.startsWith('http'))
    || t.target.includes('REPLACE_WITH_REAL');
  if (empty) return '<span class="badge badge-warn">⚠ 未配置/占位</span>';
  return '<span class="badge badge-on">● 已启用</span>';
}
function layerName(layers, id) {
  const l = layers.find((x) => x.id === id);
  return l ? `${l.name}(${l.id})` : (id || '-');
}
async function loadRiskConfig() {
  try {
    const [rules, layers] = await Promise.all([req('/risks/rules'), req('/risks/layers')]);
    _layersCache = layers;
    $('#ruleList').innerHTML = rules.length ? rules.map((t) => `
      <div class="tpl-card ${t.enabled ? '' : 'disabled'}">
        <h4>${esc(t.name)} <span class="tag tag-${t.severity === 'critical' ? 'failed' : t.severity === 'high' ? 'warn' : 'processing'}">${SEV_LABEL[t.severity] || t.severity}</span> <span class="tag tag-skipped">${esc(t.category)}</span></h4>
        <div class="desc">${esc(t.description || '')}</div>
        <div class="kw">${(t.keywords || []).map((k) => `<span>${esc(k)}</span>`).join('') || '<span>无关键词</span>'}</div>
        <div class="desc">路由层：${(t.alert_layers && t.alert_layers.length) ? t.alert_layers.map((x) => layerName(layers, x)).join('、') : '按严重度兜底'} · 作用群：${t.scope_rooms && t.scope_rooms.length ? t.scope_rooms.length + ' 个' : '全群'}</div>
        <div class="tpl-actions">
          <button class="btn btn-sm" onclick="editRule('${t.id}')">编辑</button>
          <button class="btn btn-sm" onclick="toggleRule('${t.id}',${!t.enabled})">${t.enabled ? '停用' : '启用'}</button>
          <button class="btn btn-sm btn-warn" onclick="delRule('${t.id}')">删除</button>
        </div>
      </div>`).join('') : '<div class="empty">暂无规则</div>';

    $('#layerList').innerHTML = layers.length ? layers.map((l) => `
      <div class="layer-card">
        <h4>${esc(l.name)} <span class="lvl">Lv ${l.level}</span> <span class="tag tag-skipped">${esc(l.id)}</span></h4>
        <div class="desc">${esc(l.description || '')}</div>
        ${(l.targets || []).map((t) => `<div class="target-row">
            <span class="ch ch-${esc(t.channel)}">${esc(CH_LABEL[t.channel] || t.channel)}</span>
            <span class="tg" title="${esc(t.target || '')}">${esc(t.label || t.target || (t.channel === 'system' ? '自动送达' : '(无目标)'))}</span>
            ${targetBadge(t)}
            <span class="act">
              <label class="chk"><input type="checkbox" data-tid="${t.id}" ${t.enabled ? 'checked' : ''} onchange="toggleTarget('${t.id}',this.checked)"> 启用</label>
              <button class="btn btn-sm" onclick="editTarget('${esc(l.id)}','${esc(t.id)}')">编辑</button>
              <button class="btn btn-sm" onclick="testLayer('${esc(l.id)}')">测试</button>
              <button class="btn btn-sm btn-warn" onclick="delTarget('${esc(t.id)}')">删除</button>
            </span>
          </div>`).join('') || '<div class="desc">该层暂无投递目标</div>'}
        <div class="row-btns"><button class="btn btn-sm" onclick="addTarget('${l.id}')">+ 添加投递目标</button>
          <button class="btn btn-sm btn-warn" onclick="delLayer('${l.id}')">删除层</button></div>
      </div>`).join('') : '<div class="empty">暂无管理层</div>';

    loadTimeoutConfig();
  } catch (e) { toast('加载风控配置失败：' + e.message, 'err'); }
}

/* 超时回复提醒配置：从 /settings 读取并允许前端覆盖（无需改 .env / 重启） */
async function loadTimeoutConfig() {
  try {
    const s = await req('/settings').catch(() => ({}));
    const t = (s && s.risk_timeout) || {};
    $('#toEnabled').checked = t.enabled !== false;
    $('#toMinutes').value = t.minutes || 30;
    $('#toSeverity').value = t.severity || 'medium';
  } catch (e) { /* 配置缺失不阻断 */ }
}
async function saveTimeoutConfig() {
  const body = {
    risk_timeout: {
      enabled: $('#toEnabled').checked,
      minutes: parseInt($('#toMinutes').value, 10) || 30,
      severity: $('#toSeverity').value,
    },
  };
  try {
    await req('/settings', { method: 'PUT', body: JSON.stringify(body) });
    $('#toMsg').textContent = '已保存';
    toast('超时回复提醒设置已保存', 'ok');
  } catch (e) {
    $('#toMsg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'err');
  }
}

/* OCR 视觉升级配置：KV 存储，前端覆盖（无需改 .env / 重启） */
async function loadOcrVisionConfig() {
  try {
    const s = await req('/settings').catch(() => ({}));
    $('#ovEnabled').checked = s.OCR_VISION_ENABLED === true;
    $('#ovConf').value = (typeof s.OCR_VISION_MIN_CONFIDENCE === 'number') ? s.OCR_VISION_MIN_CONFIDENCE : 0.6;
    const ft = s.OCR_VISION_FORCE_TEMPLATES || [];
    $('#ovForce').value = Array.isArray(ft) ? ft.join(',') : '';
  } catch (e) { /* 配置缺失不阻断 */ }
  // 视觉模型就绪状态（来自 /api/system/health）
  try {
    const h = await req('/system/health').catch(() => null);
    const v = h && h.ocr && h.ocr.ocr_vision;
    if (v) {
      const ready = v.model_configured ? `已就绪（${v.model || ''}）` : '未配置视觉模型';
      const onOff = v.enabled ? '开关已开' : '开关已关';
      $('#ovStatus').textContent = `视觉模型：${ready}；${onOff}`;
    }
  } catch (e) { /* ignore */ }
}
async function saveOcrVisionConfig() {
  const conf = parseFloat($('#ovConf').value);
  const body = {
    OCR_VISION_ENABLED: $('#ovEnabled').checked,
    OCR_VISION_MIN_CONFIDENCE: isNaN(conf) ? 0.6 : Math.min(1, Math.max(0, conf)),
    OCR_VISION_FORCE_TEMPLATES: $('#ovForce').value.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
  };
  try {
    await req('/settings', { method: 'PUT', body: JSON.stringify(body) });
    $('#ovMsg').textContent = '已保存';
    toast('OCR 视觉升级设置已保存', 'ok');
    loadOcrVisionConfig();
  } catch (e) {
    $('#ovMsg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'err');
  }
}
$('#ovSave').onclick = saveOcrVisionConfig;

$('#toSave').onclick = saveTimeoutConfig;
$('#toScan').onclick = async () => {
  try {
    const r = await req('/risks/timeout-scan', { method: 'POST' });
    $('#toMsg').textContent = '扫描完成：' + (r && r.data ? JSON.stringify(r.data) : '');
    toast('已触发一次超时扫描', 'ok');
  } catch (e) {
    $('#toMsg').textContent = '扫描失败：' + e.message;
    toast('扫描失败：' + e.message, 'err');
  }
};

async function openRiskModal(rule) {
  editingRuleId = rule ? rule.id : null;
  $('#riskModalTitle').textContent = rule ? '编辑规则：' + rule.name : '新建风险规则';
  $('#rkName').value = rule?.name || '';
  $('#rkCategory').value = rule?.category || '价格异常';
  $('#rkSeverity').value = rule?.severity || 'medium';
  $('#rkEnabled').checked = rule ? rule.enabled : true;
  $('#rkKeywords').value = (rule?.keywords || []).join(',');
  $('#rkLlm').value = rule?.llm_prompt || '';
  // 初始化选择集合 + 名称映射（供 chips 显示），加载不阻塞弹窗打开
  rkRoomSel = new Set(rule?.scope_rooms || []);
  rkLayerSel = new Set(rule?.alert_layers || []);
  const [rooms, layers] = await Promise.all([
    req('/rooms').catch(() => []),
    req('/risks/layers').catch(() => []),
  ]);
  rkRoomMap = new Map(rooms.map((r) => [r.room_id, r.name || r.room_id]));
  rkLayerMap = new Map(layers.map((l) => [l.id, l.name || l.id]));
  renderScopeChips();
  renderLayerChips();
  $('#riskModalMask').classList.add('show');
}
const splitC = (s) => s.split(/[,，]/).map((x) => x.trim()).filter(Boolean);

// ---- 规则 modal：作用群 / 管理层 以「标签(chip)」展示，选择走独立弹窗 ----
let rkRoomSel = new Set();
let rkLayerSel = new Set();
let rkRoomMap = new Map();
let rkLayerMap = new Map();

function chipsHtml(ids, map) {
  if (!ids.length) return '<span style="font-size:12px;color:#9aa3af">未选择（全部生效 / 按严重度兜底）</span>';
  return ids.map((id) => `<span style="display:inline-flex;align-items:center;gap:5px;background:#eef2ff;color:#3730a3;border:1px solid #c7d2fe;border-radius:14px;padding:2px 9px;margin:3px 5px 3px 0;font-size:12px">${esc(map.get(id) || id)} <b style="cursor:pointer;font-weight:700" onclick="rkRemoveChip('${esc(id)}')">×</b></span>`).join('');
}
function renderScopeChips() { $('#rkScopeChips').innerHTML = chipsHtml(Array.from(rkRoomSel), rkRoomMap); }
function renderLayerChips() { $('#rkLayersChips').innerHTML = chipsHtml(Array.from(rkLayerSel), rkLayerMap); }
window.rkRemoveChip = (id) => { rkRoomSel.delete(id); rkLayerSel.delete(id); renderScopeChips(); renderLayerChips(); };

// ---- 通用选择弹窗（作用群 / 管理层 复用）----
let pickerType = null;
function openPicker(type) {
  pickerType = type;
  const isRoom = type === 'room';
  req(isRoom ? '/rooms' : '/risks/layers').then((data) => {
    const ids = (isRoom ? rkRoomSel : rkLayerSel);
    $('#pickerTitle').textContent = isRoom ? '选择作用群' : '选择通知管理层';
    const list = $('#pickerList');
    const items = () => data.map((d) => {
      const id = isRoom ? d.room_id : d.id;
      const name = isRoom ? (d.name || d.room_id) : (d.name || d.id);
      return { id, name };
    });
    const render = (q) => {
      q = (q || '').trim().toLowerCase();
      const arr = items().filter((it) => !q || it.name.toLowerCase().includes(q) || it.id.toLowerCase().includes(q));
      list.innerHTML = arr.length ? arr.map((it) => `
        <label style="display:flex;align-items:center;gap:8px;padding:8px 6px;border-bottom:1px solid #f2f4f7;font-size:14px;cursor:pointer;color:#1f2937" onmouseover="this.style.background='#f6f8fa'" onmouseout="this.style.background='transparent'">
          <input type="checkbox" data-pid="${esc(it.id)}" ${ids.has(it.id) ? 'checked' : ''}>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><b style="color:#111827">${esc(it.name)}</b> <span style="color:#4b5563;font-size:12px">${esc(it.id)}</span></span>
          <button type="button" class="btn btn-sm btn-warn" style="margin-left:auto" onclick="event.stopPropagation(); ${isRoom ? `delRoom('${esc(it.id)}', true)` : `delLayerInPicker('${esc(it.id)}')`}">删除</button>
        </label>`).join('') : '<div style="padding:24px;text-align:center;color:#9aa3af">无匹配项</div>';
      $('#pickerTotal').textContent = arr.length;
      $('#pickerCount').textContent = list.querySelectorAll('input:checked').length;
    };
    render('');
    $('#pickerSearch').value = '';
    $('#pickerSearch').oninput = (e) => render(e.target.value);
    list.onchange = () => { $('#pickerCount').textContent = list.querySelectorAll('input:checked').length; };
    $('#pickerAll').onclick = () => { list.querySelectorAll('input[data-pid]').forEach((c) => (c.checked = true)); $('#pickerCount').textContent = list.querySelectorAll('input:checked').length; };
    $('#pickerClear').onclick = () => { list.querySelectorAll('input[data-pid]').forEach((c) => (c.checked = false)); $('#pickerCount').textContent = 0; };
    $('#pickerOk').onclick = () => {
      list.querySelectorAll('input[data-pid]').forEach((c) => {
        const id = c.getAttribute('data-pid');
        if (c.checked) ids.add(id); else ids.delete(id);
      });
      if (isRoom) renderScopeChips(); else renderLayerChips();
      closePicker();
    };
    $('#pickerCancel').onclick = () => closePicker();
    $('#pickerClose').onclick = () => closePicker();
    $('#pickerMask').style.display = 'flex';
  }).catch((e) => toast('加载列表失败：' + e.message, 'err'));
}
function closePicker() { $('#pickerMask').style.display = 'none'; }
$('#rkScopePick').onclick = () => openPicker('room');
$('#rkLayersPick').onclick = () => openPicker('layer');
$('#ruleNew').onclick = () => openRiskModal(null);
$('#riskModalCancel').onclick = () => $('#riskModalMask').classList.remove('show');
$('#riskModalMask').onclick = (e) => { if (e.target === $('#riskModalMask')) $('#riskModalMask').classList.remove('show'); };
$('#riskModalSave').onclick = async () => {
  const body = {
    name: $('#rkName').value.trim(), category: $('#rkCategory').value, severity: $('#rkSeverity').value,
    enabled: $('#rkEnabled').checked,
    scope_rooms: Array.from(rkRoomSel),
    keywords: splitC($('#rkKeywords').value),
    alert_layers: Array.from(rkLayerSel),
    llm_prompt: $('#rkLlm').value.trim() || null,
  };
  if (!body.name) return toast('请填写规则名称', 'err');
  if (!body.keywords.length) return toast('至少填一个关键词', 'err');
  try {
    if (editingRuleId) await req('/risks/rules/' + editingRuleId, { method: 'PATCH', body: JSON.stringify(body) });
    else await req('/risks/rules', { method: 'POST', body: JSON.stringify(body) });
    toast('已保存', 'ok'); $('#riskModalMask').classList.remove('show'); loadRiskConfig();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
};
window.editRule = async (id) => openRiskModal(await req('/risks/rules').then((rs) => rs.find((x) => x.id === id)));
window.toggleRule = async (id, enabled) => {
  try { await req('/risks/rules/' + id, { method: 'PATCH', body: JSON.stringify({ enabled }) }); loadRiskConfig(); }
  catch (e) { toast(e.message, 'err'); }
};
window.delRule = async (id) => {
  if (!confirm('确认删除该规则？')) return;
  try { const r = await req('/risks/rules/' + id, { method: 'DELETE' }); toast(r.message, 'ok'); loadRiskConfig(); }
  catch (e) { toast(e.message, 'err'); }
};
window.delLayer = async (id) => {
  if (!confirm('确认删除该管理层？其投递目标一并删除。')) return;
  try { const r = await req('/risks/layers/' + id, { method: 'DELETE' }); toast(r.message, 'ok'); loadRiskConfig(); }
  catch (e) { toast(e.message, 'err'); }
};
/* ---- 删除：群 / 投递目标 / 记录 / 消息 ---- */
window.delRoom = async (roomId, fromPicker) => {
  if (!confirm('确认删除该群？将连带删除该群全部已存档聊天记录、附件、风险事件与结构化记录，且本地媒体文件一并清除，不可恢复！')) return;
  try {
    const r = await req('/rooms/' + encodeURIComponent(roomId), { method: 'DELETE' });
    toast(`已删除群 ${roomId}（消息${r.deleted_messages}/附件${r.deleted_attachments}/风险${r.deleted_risk_events}/记录${r.deleted_records}）`, 'ok');
    if (fromPicker) { openPicker('room'); } else { loadRooms(); if ($('#view-dashboard').classList.contains('active')) loadDashboard(); closeDrawer(); }
  } catch (e) { toast('删除失败：' + e.message, 'err'); }
};
window.delLayerInPicker = async (id) => {
  if (!confirm('确认删除该管理层？其投递目标一并删除。')) return;
  try { await req('/risks/layers/' + id, { method: 'DELETE' }); toast('已删除管理层', 'ok'); openPicker('layer'); }
  catch (e) { toast(e.message, 'err'); }
};
window.delTarget = async (id) => {
  if (!confirm('确认删除该投递目标？')) return;
  try { const r = await req('/risks/targets/' + id, { method: 'DELETE' }); toast(r.message || '已删除', 'ok'); loadRiskConfig(); }
  catch (e) { toast(e.message, 'err'); }
};
window.delRecord = async (id) => {
  if (!confirm('确认删除该结构化记录？关联的消息仍保留。')) return;
  try { await req('/records/' + id, { method: 'DELETE' }); toast('已删除记录', 'ok'); if (typeof loadRecords === 'function') loadRecords(1); closeDrawer(); }
  catch (e) { toast(e.message, 'err'); }
};
window.delMessage = async (id) => {
  if (!confirm('确认删除该消息？其附件一并删除，风险事件保留（解除关联）。')) return;
  try { await req('/messages/' + id, { method: 'DELETE' }); toast('已删除消息', 'ok'); loadMessages(1); closeDrawer(); }
  catch (e) { toast(e.message, 'err'); }
};
window.toggleTarget = async (id, enabled) => {
  try { await req('/risks/targets/' + id, { method: 'PATCH', body: JSON.stringify({ enabled }) }); toast('已更新', 'ok'); }
  catch (e) { toast(e.message, 'err'); }
};
window.testLayer = async (id) => {
  try {
    const r = await req('/risks/layers/' + id + '/test', { method: 'POST' });
    const results = (r.data && r.data.results) || [];
    openDrawer('投递测试 · 层 ' + id, `
      <div class="kv">
        <span class="k">总体结果</span><span class="v">${r.ok ? '<span class="tag tag-done">全部可达</span>' : '<span class="tag tag-failed">存在失败通道</span>'}</span>
      </div>
      <h4>各通道明细</h4>
      ${results.length ? results.map((x) => `<div class="dist-item">
          <span>${esc(CH_LABEL[x.channel] || x.channel)} · ${x.ok ? '✅ 成功' : '❌ 失败'}</span>
          <span class="muted">${esc(x.detail || '')}</span>
        </div>`).join('') : '<div class="dist-item"><span>该层暂无投递目标</span></div>'}
    `);
  } catch (e) { toast(e.message, 'err'); }
};

let _targetLayerId = null;
let _targetEditId = null;
function openTargetModal(layerId, target) {
  _targetLayerId = layerId;
  _targetEditId = target ? target.id : null;
  $('#targetModalTitle').textContent = target ? '编辑投递目标' : '添加投递目标';
  const ch = target ? target.channel : 'webhook';
  $('#tgChannel').value = ch;
  $('#tgTarget').value = target ? (target.target || '') : '';
  $('#tgLabel').value = target ? (target.label || '') : '';
  $('#tgEnabled').checked = target ? target.enabled !== false : true;
  applyChannelHint(ch);
  $('#targetModalMask').classList.add('show');
}
function applyChannelHint(ch) {
  const c = TARGET_CHANNELS[ch] || TARGET_CHANNELS.webhook;
  $('#tgTargetLabel').textContent = c.targetLabel;
  $('#tgTarget').placeholder = c.placeholder;
  $('#tgHint').textContent = c.hint;
  $('#tgTarget').disabled = (ch === 'system');
  if (ch === 'system') $('#tgTarget').value = '';
}
async function saveTargetModal() {
  const channel = $('#tgChannel').value;
  const target = channel === 'system' ? '' : $('#tgTarget').value.trim();
  const label = $('#tgLabel').value.trim() || null;
  const enabled = $('#tgEnabled').checked;
  if (channel !== 'system' && !target) return toast('请填写目标', 'err');
  try {
    if (_targetEditId) {
      await req('/risks/targets/' + _targetEditId, { method: 'PATCH', body: JSON.stringify({ target, label, enabled }) });
    } else {
      await req('/risks/targets', { method: 'POST', body: JSON.stringify({ layer_id: _targetLayerId, channel, target, label, enabled }) });
    }
    toast('已保存', 'ok');
    $('#targetModalMask').classList.remove('show');
    loadRiskConfig();
  } catch (e) { toast(e.message, 'err'); }
}
window.addTarget = function (layerId) { openTargetModal(layerId, null); };
window.editTarget = function (layerId, tid) {
  const layer = _layersCache.find((l) => l.id === layerId);
  const t = layer && (layer.targets || []).find((x) => x.id === tid);
  if (!t) return toast('未找到该投递目标', 'err');
  openTargetModal(layerId, t);
};
// 投递目标弹窗事件绑定
$('#tgChannel').onchange = (e) => applyChannelHint(e.target.value);
$('#targetModalCancel').onclick = () => $('#targetModalMask').classList.remove('show');
$('#targetModalMask').onclick = (e) => { if (e.target === $('#targetModalMask')) $('#targetModalMask').classList.remove('show'); };
$('#targetModalSave').onclick = saveTargetModal;
$('#layerNew').onclick = async () => {
  const id = prompt('管理层 ID（如 L4）：', 'L4'); if (!id) return;
  const name = prompt('名称：', '新管理层'); if (!name) return;
  const level = parseInt(prompt('层级（数字，越大越高）：', '4'), 10) || 4;
  try { await req('/risks/layers', { method: 'POST', body: JSON.stringify({ id, name, level }) }); toast('已创建', 'ok'); loadRiskConfig(); }
  catch (e) { toast(e.message, 'err'); }
};

/* ================ 模型配置 ================ */
const PROVIDER_LABEL = { ollama: '本地 Ollama', openai: '外部 OpenAI 兼容' };
const ROLE_LABEL = { extract: '结构化抽取', risk: '风险研判', extract_vision: '视觉抽取(多模态)' };
let editingModelId = null;

async function loadModels() {
  $('#modelRoleBanner').innerHTML = '角色绑定加载中…';
  $('#modelList').innerHTML = '<div class="empty">加载中…</div>';
  try {
    let list = [];
    let rb = { roles: [] };
    try { list = await req('/models'); } catch (e) {
      $('#modelRoleBanner').innerHTML = `<span style="color:#c0392b">角色绑定加载失败：${esc(e.message)}</span>`;
    }
    try { rb = await req('/models/roles'); } catch (e) {
      $('#modelRoleBanner').innerHTML = `<span style="color:#c0392b">角色绑定加载失败：${esc(e.message)}</span>`;
    }
    const served = {};
    (rb.roles || []).forEach((r) => { served[r.role] = r.served_by; });
    $('#modelRoleBanner').innerHTML = '角色绑定：' + (rb.roles || []).map((r) => {
      const s = r.served_by;
      const tag = s ? `${esc(s.name)}${s.via_default ? '（默认兜底）' : ''}` : '<span style="color:#c0392b">未绑定</span>';
      return `<b>${ROLE_LABEL[r.role] || r.role}</b> → ${tag}`;
    }).join('　|　');

    $('#modelList').innerHTML = list.length ? list.map((m) => `
      <div class="tpl-card ${m.enabled ? '' : 'disabled'}">
        <h4>${esc(m.name)} ${m.is_default ? '<span class="tag tag-done">默认</span>' : ''}
            <span class="tag tag-skipped">${PROVIDER_LABEL[m.provider] || m.provider}</span></h4>
        <div class="desc">${esc(m.base_url || '-')} · 模型 <b>${esc(m.model || '-')}</b></div>
        <div class="desc">用途：${(m.roles && m.roles.length) ? m.roles.map((r) => ROLE_LABEL[r] || r).join('、') : '（无）'} · 温度 ${m.temperature} · 超时 ${m.timeout}s</div>
        <div class="tpl-actions">
          <button class="btn btn-sm" onclick="mdTestSaved('${m.id}')">测试</button>
          <button class="btn btn-sm" onclick="mdEdit('${m.id}')">编辑</button>
          <button class="btn btn-sm" onclick="mdToggle('${m.id}',${!m.enabled})">${m.enabled ? '停用' : '启用'}</button>
          <button class="btn btn-sm btn-warn" onclick="mdDel('${m.id}')">删除</button>
        </div>
      </div>`).join('') : '<div class="empty">暂无模型连接，点右上角「新建模型连接」</div>';
  } catch (e) { $('#modelList').innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

function openModelModal(m) {
  editingModelId = m ? m.id : null;
  $('#modelModalTitle').textContent = m ? '编辑连接：' + m.name : '新建模型连接';
  $('#mdName').value = m?.name || '';
  $('#mdProvider').value = m?.provider || 'ollama';
  $('#mdEnabled').checked = m ? m.enabled : true;
  $('#mdDefault').checked = m ? !!m.is_default : false;
  $('#mdBaseUrl').value = m?.base_url || '';
  $('#mdApiKey').value = '';
  $('#mdModel').value = m?.model || '';
  $('#mdTemp').value = m?.temperature ?? 0.1;
  $('#mdTimeout').value = m?.timeout ?? 180;
  $('#mdRoleExtract').checked = m ? (m.roles || []).includes('extract') : true;
  $('#mdRoleRisk').checked = m ? (m.roles || []).includes('risk') : true;
  $('#mdRoleVision').checked = m ? (m.roles || []).includes('extract_vision') : false;
  $('#mdTestResult').textContent = '';
  $('#mdModelList').innerHTML = '';
  $('#modelModalMask').classList.add('show');
}

$('#modelNew').onclick = () => openModelModal(null);
$('#modelModalCancel').onclick = () => $('#modelModalMask').classList.remove('show');
$('#modelModalMask').onclick = (e) => { if (e.target === $('#modelModalMask')) $('#modelModalMask').classList.remove('show'); };

$('#modelModalSave').onclick = async () => {
  const roles = [];
  if ($('#mdRoleExtract').checked) roles.push('extract');
  if ($('#mdRoleRisk').checked) roles.push('risk');
  if ($('#mdRoleVision').checked) roles.push('extract_vision');
  const body = {
    name: $('#mdName').value.trim(), provider: $('#mdProvider').value,
    base_url: $('#mdBaseUrl').value.trim(), api_key: $('#mdApiKey').value,
    model: $('#mdModel').value.trim(), temperature: parseFloat($('#mdTemp').value) || 0.1,
    timeout: parseInt($('#mdTimeout').value, 10) || 180,
    enabled: $('#mdEnabled').checked, is_default: $('#mdDefault').checked, roles,
  };
  if (!body.name) return toast('请填写连接名称', 'err');
  if (!body.base_url) return toast('请填写 Base URL', 'err');
  try {
    if (editingModelId) await req('/models/' + editingModelId, { method: 'PATCH', body: JSON.stringify(body) });
    else await req('/models', { method: 'POST', body: JSON.stringify(body) });
    toast('已保存', 'ok'); $('#modelModalMask').classList.remove('show'); loadModels();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
};

$('#mdFetch').onclick = async () => {
  const provider = $('#mdProvider').value;
  const base_url = $('#mdBaseUrl').value.trim();
  const api_key = $('#mdApiKey').value;
  if (!base_url) return toast('请先填 Base URL', 'err');
  try {
    const r = await req('/models/fetch-models', { method: 'POST', body: JSON.stringify({ provider, base_url, api_key, config_id: editingModelId || undefined }) });
    const opts = (r.models || []).map((n) => `<option value="${esc(n)}">`).join('');
    $('#mdModelList').innerHTML = opts;
    toast(r.models && r.models.length ? `已拉取 ${r.models.length} 个模型` : '未返回模型列表', r.models && r.models.length ? 'ok' : 'err');
  } catch (e) { toast('拉取失败：' + e.message, 'err'); }
};

$('#mdTest').onclick = async () => {
  const body = {
    provider: $('#mdProvider').value, base_url: $('#mdBaseUrl').value.trim(),
    api_key: $('#mdApiKey').value, model: $('#mdModel').value.trim(),
    config_id: editingModelId || undefined,
  };
  if (!body.base_url) return toast('请先填 Base URL', 'err');
  $('#mdTestResult').textContent = '测试中…';
  try {
    const r = await req('/models/probe', { method: 'POST', body: JSON.stringify(body) });
    const d = r.data || {};
    const ok = d.reachable && d.sample_ok;
    const parts = [];
    parts.push(d.reachable ? `✓ 连通（${d.latency_ms ?? '?'}ms）` : `✗ 不可达`);
    if (d.reachable) parts.push(d.sample_ok ? '✓ 样例 JSON 正常' : '✗ 样例 JSON 失败');
    if (d.models && d.models.length) parts.push(`模型数 ${d.models.length}`);
    if (d.error) parts.push('⚠ ' + d.error);
    $('#mdTestResult').textContent = parts.join('　');
    $('#mdTestResult').style.color = ok ? '#1a7f37' : '#c0392b';
    if (d.models && d.models.length && !$('#mdModel').value) {
      $('#mdModel').value = d.models[0];
    }
  } catch (e) { $('#mdTestResult').textContent = '测试失败：' + e.message; $('#mdTestResult').style.color = '#c0392b'; }
};

window.mdEdit = async (id) => openModelModal(await req('/models/' + id).catch(() => null));
window.mdToggle = async (id, enabled) => {
  try { await req('/models/' + id, { method: 'PATCH', body: JSON.stringify({ enabled }) }); loadModels(); }
  catch (e) { toast(e.message, 'err'); }
};
window.mdDel = async (id) => {
  if (!confirm('确认删除该模型连接？正在使用它的用途将回退到默认连接。')) return;
  try { const r = await req('/models/' + id, { method: 'DELETE' }); toast(r.message, 'ok'); loadModels(); }
  catch (e) { toast(e.message, 'err'); }
};
window.mdTestSaved = async (id) => {
  try {
    const r = await req('/models/' + id + '/test', { method: 'POST' });
    const d = r.data || {};
    const ok = d.reachable && d.sample_ok;
    const msg = [d.reachable ? `连通(${d.latency_ms ?? '?'}ms)` : '不可达',
                 d.sample_ok ? '样例正常' : (d.model ? '样例失败' : ''),
                 d.models && d.models.length ? `模型${d.models.length}个` : ''].filter(Boolean).join(' / ');
    toast('测试：' + (msg || (d.error || '')), ok ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
};

/* ================ 抽取路线对比 ================ */
async function loadExtractCompare() {
  try {
    const m = await req('/extract/modes');
    const cur = m.current_mode;
    const v = m.vision || {};
    const status = `当前模式：<b>${cur === 'vision' ? '视觉模型直接看图（实验）' : 'OCR + 文本模型（现状）'}</b>　|　` +
      `视觉模型：${v.configured ? `已配置（${esc(v.name)} / ${esc(v.model)}）` : '<span style="color:#c0392b">未配置</span>'}`;
    $('#extractModeStatus').innerHTML = status;
  } catch (e) { $('#extractModeStatus').innerHTML = `<span style="color:#c0392b">加载失败：${esc(e.message)}</span>`; }
}

async function setExtractMode(mode) {
  try {
    const r = await req('/extract/set-mode', { method: 'POST', body: JSON.stringify({ mode }) });
    toast(r.message, 'ok');
    loadExtractCompare();
  } catch (e) { toast('切换失败：' + e.message, 'err'); }
}
$('#modeOcrLlm').onclick = () => setExtractMode('ocr_llm');
$('#modeVision').onclick = () => setExtractMode('vision');

$('#cmpRun').onclick = async () => {
  const n = parseInt($('#cmpSampleSize').value, 10) || 5;
  $('#cmpSummary').textContent = '对比运行中（视觉路线若未配模型会跳过，OCR 路线通常十几秒/张）…';
  $('#cmpResult').innerHTML = '<div class="empty">运行中…</div>';
  try {
    const r = await req('/extract/compare', { method: 'POST', body: JSON.stringify({ sample_size: n }) });
    const s = r.summary || {};
    const a = s.route_a || {}, b = s.route_b || {};
    const fmt = (x) => (x == null ? '—' : (typeof x === 'number' && x < 1 ? Math.round(x * 100) + '%' : x + 'ms'));
    $('#cmpSummary').innerHTML = `样本 ${s.doc_count} 张（${s.generated_samples ? '自动合成样例' : '本地真实附件'}）　|　` +
      `OCR路线：成功 ${a.success} 覆盖 ${fmt(a.avg_coverage)} 耗时 ${fmt(a.avg_latency_ms)}　|　` +
      `视觉路线：成功 ${b.success} 覆盖 ${fmt(b.avg_coverage)} 耗时 ${fmt(b.avg_latency_ms)}` +
      (s.vision_available ? '' : '　<span style="color:#c0392b">（视觉模型未配置，已跳过）</span>');
    $('#cmpResult').innerHTML = (r.details || []).length ? (r.details).map((d) => {
      const row = (x) => `${x.ok ? '✓' : '✗'} 覆盖 ${x.coverage == null ? '—' : Math.round(x.coverage * 100) + '%'} 耗时 ${x.latency_ms || '—'}ms${x.error ? ' ⚠' + esc(x.error) : ''}`;
      return `<div class="kv-row">
        <div class="kv-k">${esc(d.name)} <span class="tag tag-skipped">${esc(d.file_ext)}</span></div>
        <div class="kv-v">
          <div><b>OCR路线</b> ${row(d.a)} ${d.a.template ? '（' + esc(d.a.template) + '）' : ''}</div>
          <div><b>视觉路线</b> ${row(d.b)} ${d.b.template ? '（' + esc(d.b.template) + '）' : ''}</div>
          ${d.note ? '<div class="desc">' + esc(d.note) + '</div>' : ''}
        </div>
      </div>`;
    }).join('') : '<div class="empty">无可用样本</div>';
  } catch (e) { $('#cmpSummary').textContent = '对比失败：' + e.message; }
};

/* ================ 系统管理：用户 ================ */
async function loadUsers() {
  const el = $('#adminUsersBox');
  if (!el) return;
  el.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const users = await req('/users');
    el.innerHTML = `
      <div class="admin-bar">
        <h3>用户列表（${users.length}）</h3>
        <button class="btn btn-sm btn-primary" data-perm="users:add" onclick="openUserForm()">+ 新增用户</button>
      </div>
      <table><thead><tr><th>用户名</th><th>姓名</th><th>角色</th><th>状态</th><th>超管</th><th>最近登录</th><th>操作</th></tr></thead>
      <tbody>${users.map((u) => `<tr>
        <td>${esc(u.username)}</td>
        <td>${esc(u.display_name || '-')}</td>
        <td>${u.roles.length ? u.roles.map((r) => `<span class="tag tag-done">${esc(r.name)}</span>`).join(' ') : '<span class="muted">-</span>'}</td>
        <td>${u.is_active ? '<span class="tag tag-done">启用</span>' : '<span class="tag tag-failed">停用</span>'}</td>
        <td>${u.is_super ? '✔' : ''}</td>
        <td>${fmtTime(u.last_login_at)}</td>
        <td>
          <button class="btn btn-sm" data-perm="users:edit" onclick="openUserForm('${u.id}')">编辑</button>
          <button class="btn btn-sm" data-perm="users:edit" onclick="resetUserPwd('${u.id}')">重置密码</button>
          ${u.is_super ? '' : `<button class="btn btn-sm btn-warn" data-perm="users:delete" onclick="delUser('${u.id}')">删除</button>`}
        </td>
      </tr>`).join('')}</tbody></table>`;
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
  applyPermGates();
}

async function openUserForm(userId) {
  let roles = [];
  try { roles = await req('/roles'); } catch (e) { /* 无角色权限时仍可建普通用户 */ }
  let u = null;
  if (userId) {
    try { const users = await req('/users'); u = users.find((x) => x.id === userId); } catch (e) { toast('加载用户失败：' + e.message, 'err'); return; }
    if (!u) { toast('用户不存在', 'err'); return; }
  }
  const roleChk = roles.map((r) => {
    const on = u && u.roles.some((x) => x.id === r.id);
    return `<label class="perm-item"><input type="checkbox" class="u-role" value="${r.id}" ${on ? 'checked' : ''}> ${esc(r.name)}</label>`;
  }).join('') || '<span class="muted">无可用角色</span>';
  openDrawer(u ? '编辑用户：' + u.username : '新增用户', `
    <div class="form-grid" style="grid-template-columns:1fr">
      ${u ? '' : '<div class="form-row"><label>用户名（登录账号，不可重复）</label><input id="ufUsername" placeholder="如 zhangsan"></div>'}
      <div class="form-row"><label>显示姓名</label><input id="ufName" value="${u ? esc(u.display_name || '') : ''}" placeholder="如 张三"></div>
      <div class="form-row"><label>${u ? '重置密码（留空=不修改）' : '初始密码（至少6位）'}</label><input id="ufPwd" type="password" placeholder="${u ? '留空不修改' : '至少 6 位'}"></div>
      <div class="form-row inline"><label class="chk"><input type="checkbox" id="ufActive" ${!u || u.is_active ? 'checked' : ''}> 启用该账号</label></div>
      <div class="form-row"><label>分配角色</label><div class="perm-tree" style="grid-template-columns:1fr">${roleChk}</div></div>
    </div>
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" onclick="saveUser('${u ? u.id : ''}')">保存</button>
      <button class="btn btn-sm" onclick="closeDrawer()">取消</button>
    </div>`);
}

window.saveUser = async (userId) => {
  const name = $('#ufName').value.trim();
  const pwd = $('#ufPwd').value;
  const active = $('#ufActive').checked;
  const roleIds = $$('.u-role:checked').map((x) => x.value);
  try {
    if (userId) {
      const body = { display_name: name, is_active: active };
      if (pwd) body.password = pwd;
      await req('/users/' + userId, { method: 'PATCH', body: JSON.stringify({ ...body, role_ids: roleIds }) });
      toast('已保存', 'ok');
    } else {
      const username = $('#ufUsername').value.trim();
      if (!username) { toast('请填写用户名', 'err'); return; }
      if (pwd.length < 6) { toast('密码至少 6 位', 'err'); return; }
      await req('/users', { method: 'POST', body: JSON.stringify({ username, password: pwd, display_name: name, is_active: active, role_ids: roleIds }) });
      toast('已创建用户', 'ok');
    }
    closeDrawer();
    loadUsers();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
};

window.delUser = async (id) => {
  if (!confirm('确认删除该用户？删除后其账号将无法登录。')) return;
  try {
    await req('/users/' + id, { method: 'DELETE' });
    toast('已删除', 'ok');
    loadUsers();
  } catch (e) { toast('删除失败：' + e.message, 'err'); }
};

window.resetUserPwd = async (id) => {
  const pwd = prompt('请输入新密码（至少 6 位）：');
  if (!pwd) return;
  if (pwd.length < 6) { toast('密码至少 6 位', 'err'); return; }
  try {
    await req('/users/' + id + '/reset-password', { method: 'POST', body: JSON.stringify({ new_password: pwd }) });
    toast('密码已重置', 'ok');
  } catch (e) { toast('重置失败：' + e.message, 'err'); }
};

/* ================ 系统管理：角色 ================ */
async function loadRoles() {
  const el = $('#adminRolesBox');
  if (!el) return;
  el.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const roles = await req('/roles');
    el.innerHTML = `
      <div class="admin-bar">
        <h3>角色列表（${roles.length}）</h3>
        <button class="btn btn-sm btn-primary" data-perm="roles:add" onclick="openRoleForm()">+ 新增角色</button>
      </div>
      <table><thead><tr><th>角色</th><th>编码</th><th>说明</th><th>权限数</th><th>成员数</th><th>内置</th><th>操作</th></tr></thead>
      <tbody>${roles.map((r) => `<tr>
        <td><b>${esc(r.name)}</b></td>
        <td><code>${esc(r.code)}</code></td>
        <td class="wrap">${esc(r.description || '-')}</td>
        <td>${r.code === 'admin' ? '全部' : r.permission_codes.length}</td>
        <td>${r.user_count}</td>
        <td>${r.is_builtin ? '<span class="tag tag-skipped">内置</span>' : ''}</td>
        <td>
          <button class="btn btn-sm" data-perm="roles:edit" onclick="openRoleForm('${r.id}')">${r.code === 'admin' ? '查看' : '编辑权限'}</button>
          ${r.is_builtin ? '' : `<button class="btn btn-sm btn-warn" data-perm="roles:delete" onclick="delRole('${r.id}')">删除</button>`}
        </td>
      </tr>`).join('')}</tbody></table>`;
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
  applyPermGates();
}

async function openRoleForm(roleId) {
  let cats = [];
  try { cats = await req('/permissions'); } catch (e) { toast('加载权限目录失败：' + e.message, 'err'); return; }
  let r = null;
  if (roleId) {
    try { const roles = await req('/roles'); r = roles.find((x) => x.id === roleId); } catch (e) { toast('加载角色失败：' + e.message, 'err'); return; }
  }
  if (r && r.code === 'admin') {
    openDrawer('超级管理员', '<div class="empty">超级管理员拥有全部权限，无需（也不能）在权限树上勾选。可到「用户管理」绑定其他角色给具体用户。</div>');
    return;
  }
  const has = new Set(r ? r.permission_ids : []);
  const tree = cats.map((c) => `
    <div class="perm-group">
      <h4>${esc(c.name)} <code style="font-size:10px">${esc(c.module)}</code></h4>
      ${c.actions.map((a) => `<label class="perm-item"><input type="checkbox" class="perm-chk" value="${a.code}" ${has.has(a.id) ? 'checked' : ''}> ${esc(a.name)}</label>`).join('')}
    </div>`).join('');
  openDrawer(r ? '编辑角色：' + r.name : '新增角色', `
    <div class="form-grid" style="grid-template-columns:1fr">
      ${r ? '' : `<div class="form-row"><label>角色名称</label><input id="rfName" placeholder="如 财务专员"></div>
       <div class="form-row"><label>角色编码（小写字母/数字/下划线）</label><input id="rfCode" placeholder="如 finance"></div>`}
      <div class="form-row"><label>角色说明</label><input id="rfDesc" value="${r ? esc(r.description || '') : ''}" placeholder="这个角色负责什么"></div>
      <div class="form-row"><label>权限分配</label><div class="perm-tree">${tree}</div></div>
    </div>
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" onclick="saveRole('${r ? r.id : ''}')">保存</button>
      <button class="btn btn-sm" onclick="closeDrawer()">取消</button>
    </div>`);
}

window.saveRole = async (roleId) => {
  const permIds = $$('.perm-chk:checked').map((x) => x.value);
  try {
    if (roleId) {
      await req('/roles/' + roleId, { method: 'PATCH', body: JSON.stringify({ permission_ids: permIds }) });
      toast('权限已保存', 'ok');
    } else {
      const name = $('#rfName').value.trim();
      const code = $('#rfCode').value.trim();
      const desc = $('#rfDesc').value.trim();
      if (!name || !code) { toast('请填写角色名称与编码', 'err'); return; }
      await req('/roles', { method: 'POST', body: JSON.stringify({ name, code, description: desc, permission_ids: permIds }) });
      toast('角色已创建', 'ok');
    }
    closeDrawer();
    loadRoles();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
};

window.delRole = async (id) => {
  if (!confirm('确认删除该角色？')) return;
  try {
    await req('/roles/' + id, { method: 'DELETE' });
    toast('已删除', 'ok');
    loadRoles();
  } catch (e) { toast('删除失败：' + e.message, 'err'); }
};

/* ================ 系统管理：权限目录 ================ */
async function loadPermCatalog() {
  const el = $('#adminPermsBox');
  if (!el) return;
  el.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const cats = await req('/permissions');
    const total = cats.reduce((s, c) => s + c.actions.length, 0);
    el.innerHTML = `
      <div class="admin-bar"><h3>权限目录（${cats.length} 个模块 · ${total} 项按钮权限）</h3></div>
      <p class="hint">权限 = 「模块:操作」，如 records:delete 表示「结构化数据 → 删除记录」。在「角色管理 → 编辑权限」中按模块勾选分配。</p>
      <div class="perm-tree" style="grid-template-columns:repeat(auto-fill,minmax(260px,1fr))">
        ${cats.map((c) => `<div class="perm-group">
          <h4>${esc(c.name)} <code style="font-size:10px">${esc(c.module)}</code></h4>
          ${c.actions.map((a) => `<div class="perm-item">${esc(a.name)} <code style="font-size:10px;color:var(--muted)">${esc(a.code)}</code></div>`).join('')}
        </div>`).join('')}
      </div>`;
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

/* ================ License 授权（私有化年费） ================ */
async function loadLicense() {
  const el = $('#adminLicenseBox');
  if (!el) return;
  el.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const s = await req('/license/status');
    const badge = {
      valid: '<span class="tag" style="background:#e8f5e9;color:#2e7d32">有效</span>',
      grace: '<span class="tag" style="background:#fff3e0;color:#e65100">宽限期</span>',
      expired: '<span class="tag tag-danger">已过期</span>',
      not_found: '<span class="tag tag-skipped">未授权</span>',
      machine_mismatch: '<span class="tag tag-danger">机器不匹配</span>',
      invalid: '<span class="tag tag-danger">无效</span>',
    }[s.status] || esc(s.status || '');
    const days = s.days_left;
    const daysTxt = (days === null || days === undefined) ? '—' : (days < 0 ? `${days} 天（已超期）` : `${days} 天`);
    const mods = (s.module_labels && s.module_labels.length)
      ? s.module_labels.map((m) => `<span class="lvl-tag">${esc(m)}</span>`).join(' ')
      : '<span class="muted">—</span>';
    el.innerHTML = `
      <div class="admin-bar"><h3>License 授权</h3>
        <button class="btn btn-primary btn-sm" onclick="doActivateLicense()">激活 / 更新许可证</button>
      </div>
      <p class="hint">${esc(s.message || '')}${s.required ? '　⚠️ 本机为强制校验模式（LICENSE_REQUIRED=true），无有效许可证将受限运行。' : '　当前为开发/演示模式（不强制校验），生产部署请设置 LICENSE_REQUIRED=true。'}</p>
      <div class="kv-grid">
        <div><label>状态</label><div>${badge}</div></div>
        <div><label>客户名称</label><div>${esc(s.customer || '—')}</div></div>
        <div><label>签发日期</label><div>${esc(s.issued_at || '—')}</div></div>
        <div><label>到期日期</label><div>${esc(s.expire_at || '—')} <span class="muted">（剩余 ${daysTxt}）</span></div></div>
        <div><label>授权模块</label><div>${mods}</div></div>
        <div><label>群数上限</label><div>${s.max_rooms ? s.max_rooms + ' 个' : '不限'}</div></div>
        <div><label>机器绑定</label><div>${s.machine_bound ? '已绑定本机（换机需重新签发）' : '未绑定（可迁移部署）'}</div></div>
        <div><label>本机指纹</label><div><code style="font-size:11px">${esc(s.fingerprint || '—')}</code></div></div>
      </div>
      <div class="hint" style="margin-top:12px">年费模式说明：续费时向厂商提供「本机指纹」申请新的许可证文件，在下方激活后自动替换。许可证使用 RSA 非对称签名，无法自行篡改或伪造。</div>`;
  } catch (e) { el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

window.doActivateLicense = () => {
  openDrawer('激活 / 更新许可证', `
    <div class="form-grid" style="grid-template-columns:1fr">
      <div class="form-row"><label>粘贴许可证内容</label>
        <textarea id="licText" rows="6" placeholder="粘贴厂商签发的许可证全文（License 文件内容）" style="width:100%"></textarea>
      </div>
      <div class="form-row"><label>或上传许可证文件</label><input id="licFile" type="file" accept=".key,.lic,.txt"></div>
    </div>
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" onclick="submitActivateLicense()">验证并激活</button>
      <button class="btn btn-sm" onclick="closeDrawer()">取消</button>
    </div>`);
};

window.submitActivateLicense = async () => {
  const text = ($('#licText').value || '').trim();
  const f = $('#licFile');
  let body;
  if (text) body = { license_text: text };
  else if (f && f.files && f.files[0]) body = { license_text: (await f.files[0].text()).trim() };
  else { toast('请粘贴许可证内容或选择文件', 'err'); return; }
  try {
    await req('/license/activate', { method: 'POST', body: JSON.stringify(body) });
    toast('许可证已激活', 'ok');
    closeDrawer();
    loadLicense();
  } catch (e) { toast('激活失败：' + e.message, 'err'); }
};

/* ================ 修改密码 ================ */
function openChangePwd() {
  openDrawer('修改密码', `
    <div class="form-grid" style="grid-template-columns:1fr">
      <div class="form-row"><label>原密码</label><input id="cpOld" type="password"></div>
      <div class="form-row"><label>新密码（至少6位）</label><input id="cpNew" type="password"></div>
    </div>
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" onclick="doChangePwd()">确认修改</button>
      <button class="btn btn-sm" onclick="closeDrawer()">取消</button>
    </div>`);
}
window.doChangePwd = async () => {
  const oldP = $('#cpOld').value, newP = $('#cpNew').value;
  if (!oldP || newP.length < 6) { toast('请填写完整，新密码至少 6 位', 'err'); return; }
  try {
    await req('/auth/change-password', { method: 'POST', body: JSON.stringify({ old_password: oldP, new_password: newP }) });
    toast('密码已修改，请重新登录', 'ok');
    closeDrawer();
    logout();
  } catch (e) { toast('修改失败：' + e.message, 'err'); }
};

/* ================ 启动 ================ */
bindSubtabs();
$('#btnLogin').onclick = doLogin;
$('#loginPass').onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };
$('#btnLogout').onclick = logout;
$('#btnChangePwd').onclick = openChangePwd;

(async function boot() {
  loadAuth();
  applyAuthUI();
  const token = localStorage.getItem(LS_TOKEN);
  if (!token) { showLogin(); return; }
  // 用 /auth/me 刷新权限（服务端重启后 token 可能失效，此时回落登录页）
  try {
    const me = await req('/auth/me');
    saveAuth(me, token);
  } catch (e) {
    showLogin();
    return;
  }
  applyAuthUI();
  refreshModePill();
  const active = document.querySelector('.tab.active');
  const loader = MAIN_LOADERS[active && active.dataset.view];
  (loader || loadDashboard)();
})();
