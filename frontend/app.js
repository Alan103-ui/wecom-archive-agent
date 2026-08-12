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
const APP_JS_VERSION = '2026-08-10-8';
function markJsVersion() {
  const el = $('#jsVer');
  if (el) el.textContent = 'JS:' + APP_JS_VERSION + (typeof loadRooms === 'function' ? '' : ' ⚠缺loadRooms');
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', markJsVersion);
else markJsVersion();

/* ---------------- 基础工具 ---------------- */
async function req(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) { /* 非 JSON 响应 */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

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
  };
});

/* ---------------- 子标签切换（同一主视图内的子面板） ---------------- */
const SUB_LOADERS = {
  'risk-events': () => loadRisks(1),
  'risk-routing': loadRouting,
  'risk-config': loadRiskConfig,
  'risk-ocr-vision': loadOcrVisionConfig,
  'data-records': () => initRecords(),
  'data-attachments': () => loadAttachments(1),
  'data-messages': () => loadMessages(1),
  'cfg-templates': loadTemplates,
  'cfg-models': loadModels,
  'cfg-extract-compare': loadExtractCompare,
  'cfg-system': loadSystem,
  'cfg-wecom': loadWeComConfig,
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
        <label class="switch" title="采集开关" onclick="event.stopPropagation()">
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
async function loadDashboard() {
  let s;
  try { s = await req('/system/stats'); } catch (e) { return toast('加载统计失败：' + e.message, 'err'); }

  const T = s.totals;
  $('#statCards').innerHTML = [
    ['消息', T.messages], ['附件', T.attachments], ['OCR 结果', T.ocr_results],
    ['结构化记录', T.records], ['风险事件', T.risk_events || 0], ['群数', T.rooms],
  ].map(([lbl, num]) => `<div class="card"><div class="num">${num}</div><div class="lbl">${lbl}</div></div>`).join('');

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
}

/* ================ 群与监控 ================ */
async function loadRooms() {
  const [rooms, rules, stats] = await Promise.all([
    req('/rooms').catch(() => []),
    req('/risks/rules').catch(() => []),
    req('/risks/stats').catch(() => ({ risk_events: 0, pending: 0, by_room: {} })),
  ]);
  $('#roomStatCards').innerHTML = [
    ['监控群数', rooms.length],
    ['采集中', rooms.filter((r) => r.enabled).length],
    ['风险事件', stats.risk_events || 0],
    ['待处置', stats.pending || 0],
  ].map(([l, n]) => `<div class="card"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join('');
  renderRoomCards($('#roomList'), rooms, rules, stats.by_room || {});
}

window.openRoom = async function (roomId) {
  const [rooms, evs, rules] = await Promise.all([
    req('/rooms').catch(() => []),
    req('/risks/events?room_id=' + encodeURIComponent(roomId) + '&page=1&page_size=8').catch(() => ({ items: [] })),
    req('/risks/rules').catch(() => []),
  ]);
  const room = rooms.find((r) => r.room_id === roomId) || { room_id: roomId };
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
      <span class="k">群主</span><span class="v">${esc(room.owner || '-')}</span>
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
    const [rules, layers] = await Promise.all([req('/risks/rules'), req('/risks/layers')]);
    const ruleRows = rules.length ? rules.map((rl) => `<tr>
        <td>${esc(rl.name)}</td>
        <td><span class="tag tag-skipped">${esc(rl.category)}</span></td>
        <td>${sevTag(rl.severity)}</td>
        <td>${(rl.scope_rooms && rl.scope_rooms.length) ? rl.scope_rooms.length + ' 个群' : '全部群'}</td>
        <td>${layerTags(layersOf(rl))}</td>
      </tr>`).join('') : '<tr><td colspan="5" class="empty">暂无规则</td></tr>';

    const layerCards = layers.length ? layers.map((l) => {
      const coverRules = rules.filter((rl) => layersOf(rl).includes(l.id));
      const roomSet = new Set(); let allGroups = false;
      coverRules.forEach((rl) => { const s = rl.scope_rooms || []; if (s.length === 0) allGroups = true; s.forEach((x) => roomSet.add(x)); });
      const coverTxt = allGroups ? '<span class="tag tag-done">全部群</span>'
        : (roomSet.size ? [...roomSet].map((x) => `<span class="tag tag-skipped">${esc(x)}</span>`).join(' ') : '<span class="muted">无（仅严重度兜底可能覆盖）</span>');
      const chans = [...new Set((l.targets || []).filter((t) => t.enabled).map((t) => t.channel))];
      return `<div class="layer-card">
        <h4>${esc(l.name)} <span class="lvl">${esc(l.id)}</span></h4>
        <div class="desc">接收方式：${chans.map((c) => `<span class="tag tag-processing">${esc(c)}</span>`).join(' ') || '未配置'}</div>
        <div class="desc">会收到来自：${coverTxt}</div>
        <div class="desc">覆盖规则 ${coverRules.length} 条</div>
      </div>`;
    }).join('') : '<div class="empty">暂无管理层</div>';

    box.innerHTML = `<div class="grid-2">
      <div><h4>规则 → 通知管理层</h4>
        <div class="table-wrap"><table><thead><tr><th>规则</th><th>分类</th><th>严重度</th><th>作用群</th><th>通知层</th></tr></thead>
        <tbody>${ruleRows}</tbody></table></div>
      </div>
      <div><h4>管理层 → 会从哪些群收到预警</h4>
        <div class="layer-list">${layerCards}</div>
      </div>
    </div>`;
  } catch (e) { box.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
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
          <th>业务时间</th><th>群名称</th>${d.columns.map((c) => `<th>${esc(c.label)}</th>`).join('')}<th>置信度</th><th>操作</th>
        </tr></thead><tbody>${
          d.rows.map((r) => `<tr>
            <td>${fmtTime(r.__biz_time)}</td><td>${esc(roomName(r.__room_id))}</td>
            ${d.columns.map((c) => `<td class="wrap">${esc(r[c.key] ?? '')}</td>`).join('')}
            <td>${r.__confidence != null ? (r.__confidence * 100).toFixed(0) + '%' : '-'}</td>
            <td><button class="btn btn-sm" onclick="showRecord('${r.__id}')">详情</button> <button class="btn btn-sm btn-warn" onclick="delRecord('${r.__id}')">删除</button></td>
          </tr>`).join('')}</tbody></table>`;
      renderPager('#recPager', d.total, page, 30, loadRecords);
    } else {
      const d = await req('/records?' + qs({
        template_name: tplName, page, page_size: 20,
        status: $('#recStatus').value, keyword: $('#recKeyword').value,
      }));
      if (!d.items.length) { wrap.innerHTML = '<div class="empty">暂无数据</div>'; $('#recPager').innerHTML = ''; return; }
      wrap.innerHTML = `<table><thead><tr>
          <th>业务时间</th><th>群名称</th><th>模板</th><th>状态</th><th>抽取字段</th><th>置信度</th><th>复核</th><th>操作</th>
        </tr></thead><tbody>${
          d.items.map((r) => `<tr>
            <td>${fmtTime(r.biz_time)}</td><td>${esc(roomName(r.room_id))}</td><td>${esc(r.template_name || '-')}</td>
            <td>${tag(r.status)}</td>
            <td class="wrap">${esc(JSON.stringify(r.fields_json || {}).slice(0, 160))}</td>
            <td>${r.confidence != null ? (r.confidence * 100).toFixed(0) + '%' : '-'}</td>
            <td>${r.reviewed ? '✓' : ''}</td>
            <td><button class="btn btn-sm" onclick="showRecord('${r.id}')">详情</button> <button class="btn btn-sm btn-warn" onclick="delRecord('${r.id}')">删除</button></td>
          </tr>`).join('')}</tbody></table>`;
      renderPager('#recPager', d.total, page, 20, loadRecords);
    }
  } catch (e) { wrap.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`; }
}

window.showRecord = async function (id) {
  const r = await req('/records/' + id).catch((e) => { toast(e.message, 'err'); return null; });
  if (!r) return;
  const fields = r.fields_json || {};
  const rows = Object.entries(fields).map(([k, v]) => `
    <tr><td style="color:var(--muted)">${esc(k)}</td>
    <td><input data-fk="${esc(k)}" value="${esc(typeof v === 'object' ? JSON.stringify(v) : v ?? '')}"></td></tr>`).join('');

  openDrawer('结构化记录详情', `
    <div class="kv">
      <span class="k">模板</span><span class="v">${esc(r.template_name || '-')}</span>
      <span class="k">状态</span><span class="v">${tag(r.status)}</span>
      <span class="k">置信度</span><span class="v">${r.confidence != null ? (r.confidence * 100).toFixed(0) + '%' : '-'}</span>
      <span class="k">模型</span><span class="v">${esc(r.model || '-')}</span>
      <span class="k">耗时</span><span class="v">${r.duration_ms} ms</span>
      <span class="k">业务时间</span><span class="v">${fmtTime(r.biz_time)}</span>
      ${r.error ? `<span class="k">错误</span><span class="v" style="color:#f87171">${esc(r.error)}</span>` : ''}
    </div>
    <h4>字段（可直接修改后保存，视为已复核）</h4>
    <table class="field-table"><tbody>${rows || '<tr><td>无字段</td></tr>'}</tbody></table>
    <div class="row-btns">
      <button class="btn btn-primary btn-sm" id="drSaveRec">保存修正</button>
      ${r.attachment_id ? `<button class="btn btn-sm" onclick="showAttachment('${r.attachment_id}')">查看来源附件</button>` : ''}
      <button class="btn btn-sm btn-warn" onclick="delRecord('${r.id}')">删除</button>
    </div>
    <h4>原始 JSON</h4><pre class="code">${esc(JSON.stringify(fields, null, 2))}</pre>`);

  $('#drSaveRec').onclick = async () => {
    const patch = {};
    $$('#drawerBody input[data-fk]').forEach((inp) => {
      let v = inp.value;
      try { if (/^[[{]/.test(v.trim())) v = JSON.parse(v); } catch (e) { /* 保留字符串原样 */ }
      patch[inp.dataset.fk] = v === '' ? null : v;
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
          <button class="btn btn-sm" onclick="retryAtt('${a.id}')">重跑</button>
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
    <h4>OCR 文本 ${ocr ? `（${ocr.text_length} 字 · ${ocr.duration_ms}ms · 置信度 ${
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
  tbody.innerHTML = '<tr><td colspan="7" class="empty">加载中…</td></tr>';
  try {
    await getRoomNameMap();
    const d = await req('/messages?' + qs({
      page, page_size: 20, keyword: $('#msgKeyword').value,
      msg_type: $('#msgType').value,
      has_attachment: $('#msgHasAtt').checked ? 'true' : '',
    }));
    tbody.innerHTML = d.items.length ? d.items.map((m) => `<tr>
        <td>${m.seq}</td><td>${fmtTime(m.msg_time)}</td>
        <td>${esc(m.from_name || m.from_id)}</td><td>${esc(roomName(m.room_id))}</td><td>${esc(m.msg_type)}</td>
        <td class="wrap">${esc((m.content_text || '').slice(0, 120))}</td>
        <td>${m.attachment_count ? `<button class="btn btn-sm" onclick="showMessage('${m.id}')">${m.attachment_count} 个</button>` : '-'} <button class="btn btn-sm btn-warn" onclick="delMessage('${m.id}')">删除</button></td>
      </tr>`).join('')
      : '<tr><td colspan="7" class="empty">暂无消息</td></tr>';
    renderPager('#msgPager', d.total, page, 20, loadMessages);
  } catch (e) { tbody.innerHTML = `<tr><td colspan="7" class="empty">加载失败：${esc(e.message)}</td></tr>`; }
}

window.showMessage = async function (id) {
  const m = await req('/messages/' + id).catch((e) => { toast(e.message, 'err'); return null; });
  if (!m) return;
  openDrawer('消息详情', `
    <div class="kv">
      <span class="k">seq / msgid</span><span class="v">${m.seq} / ${esc(m.msgid)}</span>
      <span class="k">时间</span><span class="v">${fmtTime(m.msg_time)}</span>
      <span class="k">发送人</span><span class="v">${esc(m.from_name || m.from_id)}</span>
      <span class="k">群</span><span class="v">${esc(m.room_id || '(单聊)')}</span>
      <span class="k">类型</span><span class="v">${esc(m.msg_type)}</span>
    </div>
    <h4>正文</h4><div class="ocr-text">${esc(m.content_text || '(无)')}</div>
    <h4>附件（${m.attachments.length}）</h4>
    ${m.attachments.map((a) => `<div class="dist-item">
        <span>${esc(a.file_name || a.media_type)} · ${fmtSize(a.file_size)}</span>
        <span>${tag(a.ocr_status)} <button class="btn btn-sm" onclick="showAttachment('${a.id}')">查看</button></span>
      </div>`).join('') || '<div class="dist-item"><span>无</span></div>'}
    <div class="row-btns"><button class="btn btn-sm btn-warn" onclick="delMessage('${m.id}')">删除该消息</button></div>
    <h4>原始 JSON</h4><pre class="code">${esc(JSON.stringify(m.raw_json || {}, null, 2))}</pre>`);
};

$('#msgSearch').onclick = () => loadMessages(1);
$('#msgKeyword').onkeydown = (e) => { if (e.key === 'Enter') loadMessages(1); };

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
          <button class="btn btn-sm" onclick="editTpl('${t.id}')">编辑</button>
          <button class="btn btn-sm" onclick="toggleTpl('${t.id}',${!t.enabled})">${t.enabled ? '停用' : '启用'}</button>
          <button class="btn btn-sm btn-warn" onclick="delTpl('${t.id}')">删除</button>
        </div>
      </div>`).join('') : '<div class="empty">暂无模板，点「恢复默认模板」</div>';
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
    const members = (r.members || []).map((m) =>
      `<div class="dist-item"><span>${esc(m.userid || '-')}</span><span class="muted">${esc(m.type == 2 ? '外部联系人' : '企业成员')}</span></div>`
    ).join('') || '<div class="dist-item"><span>无成员</span></div>';
    const admins = (r.admins || []).length ? `<div class="dist-item"><span class="muted">群管理员：${esc(r.admins.join(', '))}</span></div>` : '';
    box.innerHTML = `<div class="dist-item"><span>群主：${esc(r.owner || '-')}</span></div>` + members + admins;
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
async function loadRisks(page = 1) {
  riskPage = page;
  const wrap = $('#riskTable').querySelector('tbody');
  wrap.innerHTML = '<tr><td colspan="10" class="empty">加载中…</td></tr>';
  try {
    const d = await req('/risks/events?' + qs({
      page, page_size: 20, severity: $('#riskSev').value,
      status: $('#riskStatus').value, keyword: $('#riskKeyword').value,
    }));
    if (!d.items.length) {
      wrap.innerHTML = '<tr><td colspan="10" class="empty">暂无风险事件，下一轮扫描会自动研判新消息</td></tr>';
      $('#riskPager').innerHTML = '';
    } else {
      wrap.innerHTML = d.items.map((r) => `<tr>
        <td>${fmtTime(r.created_at)}</td>
        <td>${esc(r.category)}</td>
        <td>${sevTag(r.severity)}</td>
        <td>${esc(r.room_id)}</td>
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
    $('#riskStatCards').innerHTML = [
      ['风险事件', s.total], ['待处置', s.pending],
      ['严重', s.by_severity.critical || 0], ['高', s.by_severity.high || 0],
      ['中', s.by_severity.medium || 0], ['预警失败', s.by_alert_status.failed || s.by_alert_status.partial || 0],
    ].map(([l, n]) => `<div class="card"><div class="num">${n}</div><div class="lbl">${l}</div></div>`).join('');
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
      <span class="k">群</span><span class="v">${esc(r.room_id || '-')}</span>
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
      (logs && logs.length) ? logs.map((l) => `<tr><td>${esc(l.channel)}</td><td class="wrap">${esc(l.target)}</td><td>${tag(l.status)}</td><td class="wrap">${esc(l.detail || '')}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">无</td></tr>'
    }</tbody></table></div>`);
  $('#rkAck').onclick = async () => { try { await req('/risks/events/' + id + '/acknowledge', { method: 'POST', body: JSON.stringify({ reviewer: 'web' }) }); toast('已确认', 'ok'); closeDrawer(); loadRisks(riskPage); } catch (e) { toast(e.message, 'err'); } };
  $('#rkResend').onclick = async () => { try { const rr = await req('/risks/events/' + id + '/resend', { method: 'POST' }); toast(rr.message, 'ok'); closeDrawer(); loadRisks(riskPage); } catch (e) { toast(e.message, 'err'); } };
};

$('#riskSearch').onclick = () => loadRisks(1);
$('#riskKeyword').onkeydown = (e) => { if (e.key === 'Enter') loadRisks(1); };
$('#riskRescan').onclick = async () => {
  if (!confirm('把全部已扫消息重置为待扫描，下一轮风险作业将重扫（已发预警可能重复）？')) return;
  try { const r = await req('/risks/rescan', { method: 'POST', body: JSON.stringify({}) }); toast(r.message, 'ok'); loadRisks(1); }
  catch (e) { toast(e.message, 'err'); }
};

/* ================ 风控配置 ================ */
let editingRuleId = null;
async function loadRiskConfig() {
  try {
    const [rules, layers] = await Promise.all([req('/risks/rules'), req('/risks/layers')]);
    $('#ruleList').innerHTML = rules.length ? rules.map((t) => `
      <div class="tpl-card ${t.enabled ? '' : 'disabled'}">
        <h4>${esc(t.name)} <span class="tag tag-${t.severity === 'critical' ? 'failed' : t.severity === 'high' ? 'warn' : 'processing'}">${SEV_LABEL[t.severity] || t.severity}</span> <span class="tag tag-skipped">${esc(t.category)}</span></h4>
        <div class="desc">${esc(t.description || '')}</div>
        <div class="kw">${(t.keywords || []).map((k) => `<span>${esc(k)}</span>`).join('') || '<span>无关键词</span>'}</div>
        <div class="desc">路由层：${(t.alert_layers && t.alert_layers.length) ? t.alert_layers.join(',') : '按严重度兜底'} · 作用群：${t.scope_rooms && t.scope_rooms.length ? t.scope_rooms.length + ' 个' : '全群'}</div>
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
            <span class="ch">${esc(t.channel)}</span>
            <span class="tg">${esc(t.label || t.target || '')}</span>
            <span class="act">
              <label class="chk"><input type="checkbox" data-tid="${t.id}" ${t.enabled ? 'checked' : ''} onchange="toggleTarget('${t.id}',this.checked)"> 启用</label>
              <button class="btn btn-sm" onclick="testLayer('${l.id}')">测试</button>
              <button class="btn btn-sm btn-warn" onclick="delTarget('${t.id}')">删除</button>
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
  try { const r = await req('/risks/layers/' + id + '/test', { method: 'POST' });
    const lines = (r.data.results || []).map((x) => `${x.channel}: ${x.ok ? '✓' : '✗'} ${x.detail}`).join('\n');
    toast('测试结果：\n' + lines, r.ok ? 'ok' : 'err');
  } catch (e) { toast(e.message, 'err'); }
};
window.addTarget = async function (layerId) {
  const channel = prompt('通道（webhook/app/email/system）：', 'webhook');
  if (!channel) return;
  const target = prompt('目标（Webhook URL / userid 或 party:xxx / 邮箱）：', '');
  if (target === null) return;
  const label = prompt('备注名（可选）：', '');
  try {
    await req('/risks/targets', { method: 'POST', body: JSON.stringify({ layer_id: layerId, channel, target, label: label || null, enabled: true }) });
    toast('已添加', 'ok'); loadRiskConfig();
  } catch (e) { toast(e.message, 'err'); }
};
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

/* ================ 启动 ================ */
bindSubtabs();
(async function boot() {
  try {
    const cfg = await req('/system/config');
    $('#modePill').textContent = '采集模式：' + (cfg.collector_mode === 'mock' ? 'mock（演示）' : 'archive（会话存档）');
    const dm = (cfg.models || []).find((m) => m.is_default) || (cfg.models || [])[0];
    $('#subTitle').textContent = dm ? `群聊 → OCR → ${dm.model} 结构化 → 业务基础数据`
                                    : '群聊 → OCR → 信息抽取 → 业务基础数据';
  } catch (e) { /* 后端未就绪时不阻塞页面 */ }
  loadDashboard();
})();
