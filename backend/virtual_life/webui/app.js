/* ==========================================================================
 * 虚拟生命 · 观测台
 *
 * A single-page observer for one Virtual Life runtime: formatted logs, the
 * durable event stream, memory records, the knowledge graph, inner-life
 * activity and the device graph. No framework, no build step — the API serves
 * these three files from /ui/.
 *
 * Data access is same-origin: the runtime accepts either a Bearer token or the
 * HMAC-signed `vl_webui_session` cookie. The token is exchanged once through
 * POST /v1/session and never stored in localStorage.
 * ========================================================================== */

'use strict';

const API = '/v1';
const STORE = {
  theme: 'vl.theme',
  companion: 'vl.companion',
};

const VIEWS = {
  overview: { title: '总览', sub: '伙伴状态、记忆与运行时的整体视图', poll: 5000 },
  logs: { title: '日志', sub: '运行日志流，按级别、来源与内容筛选', poll: 1200 },
  events: { title: '事件', sub: '持久化的事件序列与载荷', poll: 2000 },
  memories: { title: '记忆', sub: '记忆记录、摘要与自动记忆诊断', poll: 0 },
  graph: { title: '知识图谱', sub: '实体、关系与它们引用的证据', poll: 0 },
  inner: { title: '内心活动', sub: '情绪、需求与内心独白的时间线', poll: 5000 },
  devices: { title: '设备', sub: '节点、能力与在线状态', poll: 8000 },
};

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
const KINDS = ['episode', 'fact', 'preference', 'relationship'];

const S = {
  companions: [],
  companionId: null,
  view: 'overview',
  auto: true,
  gen: 0,
  timers: new Set(),
  logs: { items: [], cursor: 0, levels: new Set(LEVELS), q: '', follow: true, max: 3000 },
  events: { items: [], cursor: 0, open: new Set(), max: 400 },
  inner: { thinking: new Set(), cursor: 0 },
  memories: { tab: 'records', kind: '', q: '', page: 0, limit: 20, open: new Set() },
  graph: { hops: 2, query: '', selected: null, zoom: 1, panX: 0, panY: 0 },
};

/* ================================================================ helpers */

function h(tag, props, ...kids) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'style') node.setAttribute('style', value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else node.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(node, kids);
  return node;
}

function ns(tag, props, ...kids) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else node.setAttribute(key, String(value));
    }
  }
  append(node, kids);
  return node;
}

function append(node, kids) {
  for (const kid of kids.flat(4)) {
    if (kid === null || kid === undefined || kid === false || kid === '') continue;
    node.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

function $(selector, root = document) {
  return root.querySelector(selector);
}

function clear(node) {
  node.replaceChildren();
  return node;
}

function num(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return Number(value).toFixed(digits);
}

function pct(value) {
  const v = Math.max(0, Math.min(1, Number(value) || 0));
  return `${(v * 100).toFixed(1)}%`;
}

function shortId(id, head = 10) {
  if (!id) return '—';
  const text = String(id);
  return text.length <= head ? text : text.slice(0, head);
}

function fmtTime(iso, style = 'full') {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  const pad = (v, n = 2) => String(v).padStart(n, '0');
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  switch (style) {
    case 'ms':
      return `${clock}.${pad(date.getMilliseconds(), 3)}`;
    case 'clock':
      return clock;
    case 'short':
      return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock.slice(0, 5)}`;
    case 'rel': {
      const delta = (Date.now() - date.getTime()) / 1000;
      if (delta < 45) return '刚刚';
      if (delta < 3600) return `${Math.round(delta / 60)} 分钟前`;
      if (delta < 86400) return `${Math.round(delta / 3600)} 小时前`;
      return `${Math.round(delta / 86400)} 天前`;
    }
    default:
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`;
  }
}

function chip(text, tone) {
  return h('span', { class: `chip${tone ? ` chip--${tone}` : ''}` }, text);
}

function metric(value, label, suffix) {
  return h(
    'div',
    { class: 'metric' },
    h('div', { class: 'metric-value' }, String(value), suffix ? h('small', null, suffix) : null),
    h('div', { class: 'metric-label' }, label),
  );
}

function barRow(label, value, tone) {
  return h(
    'div',
    { class: 'bar-row' },
    h('span', null, label),
    h('div', { class: 'bar' }, h('div', { class: `bar-fill${tone ? ` bar-fill--${tone}` : ''}`, style: `width:${pct(value)}` })),
    h('span', { class: 'bar-val' }, num(value, 2)),
  );
}

function card(title, body, opts = {}) {
  const head = h('div', { class: 'card-head' }, h('h2', null, title), opts.tools || null);
  return h(
    'section',
    { class: `card${opts.className ? ` ${opts.className}` : ''}` },
    head,
    h('div', { class: `card-body${opts.flush ? ' card-body--flush' : ''}` }, body),
    opts.foot || null,
  );
}

function emptyState(title, sub) {
  return h('div', { class: 'empty' }, h('div', { class: 'empty-title' }, title), h('div', { class: 'empty-sub' }, sub));
}

function ring(label, value, toneClass = '') {
  const r = 29;
  const circumference = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(1, Number(value) || 0));
  const svg = ns(
    'svg',
    { viewBox: '0 0 74 74' },
    ns('circle', { class: 'ring-track', cx: 37, cy: 37, r }),
    ns('circle', {
      class: `ring-arc ${toneClass}`.trim(),
      cx: 37,
      cy: 37,
      r,
      'stroke-dasharray': `${circumference}`,
      'stroke-dashoffset': `${circumference * (1 - clamped)}`,
    }),
  );
  return h(
    'div',
    { class: 'ring' },
    svg,
    h('div', { class: 'ring-caption' }, label),
    h('div', { class: 'ring-value' }, num(value, 2)),
  );
}

/* ================================================================== json */

const JSON_RE = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

function jsonPretty(value) {
  const pre = h('pre', { class: 'json' });
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  let last = 0;
  for (const match of text.matchAll(JSON_RE)) {
    const index = match.index ?? 0;
    if (index > last) pre.appendChild(document.createTextNode(text.slice(last, index)));
    const [raw, str, colon, literal, number] = match;
    if (str) {
      pre.appendChild(h('span', { class: colon ? 'k' : 's' }, str));
      if (colon) pre.appendChild(document.createTextNode(colon));
    } else if (literal) {
      pre.appendChild(h('span', { class: 'b' }, literal));
    } else if (number) {
      pre.appendChild(h('span', { class: 'n' }, number));
    } else {
      pre.appendChild(document.createTextNode(raw));
    }
    last = index + raw.length;
  }
  if (last < text.length) pre.appendChild(document.createTextNode(text.slice(last)));
  return pre;
}

/* =========================================================== log message */

const HL_RE =
  /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\b[A-Za-z_][A-Za-z0-9_.\-]{0,40}=)(?!=)|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})|(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d{1,5})?\b)|(\b[1-5]\d\d\b)|(\/(?:api|ui|v\d|healthz|plugins|virtual-life)[A-Za-z0-9_/\-{}.?=&\:%]*)|(\b[A-Za-z_]*(?:Error|Exception|Timeout|Refused)\b|\b(?:Traceback|failed|failure|denied|refused|timeout|panic|WARN(?:ING)?)\b)|(\b\d+(?:\.\d+)?(?:ms|s|MB|KB|GB|bytes)?\b)/g;

function highlightInto(target, text) {
  let last = 0;
  for (const match of text.matchAll(HL_RE)) {
    const index = match.index ?? 0;
    if (index > last) target.appendChild(document.createTextNode(text.slice(last, index)));
    const [raw, str, key, uuid, ip, code, path, err, number] = match;
    if (str) target.appendChild(h('span', { class: 'hl hl--str' }, raw));
    else if (key) target.appendChild(h('span', { class: 'hl hl--key' }, key));
    else if (uuid) target.appendChild(h('span', { class: 'hl hl--uuid' }, uuid));
    else if (ip) target.appendChild(h('span', { class: 'hl hl--ip' }, ip));
    else if (code) {
      const tone = code.startsWith('5') ? ' hl--code-5' : code.startsWith('4') ? ' hl--code-4' : code.startsWith('2') ? ' hl--code-2' : '';
      target.appendChild(h('span', { class: `hl hl--code${tone}` }, code));
    } else if (path) target.appendChild(h('span', { class: 'hl hl--path' }, path));
    else if (err) target.appendChild(h('span', { class: 'hl hl--err' }, err));
    else if (number) target.appendChild(h('span', { class: 'hl hl--num' }, number));
    else target.appendChild(document.createTextNode(raw));
    last = index + raw.length;
  }
  if (last < text.length) target.appendChild(document.createTextNode(text.slice(last)));
}

function renderMessage(text, logger) {
  const frag = document.createDocumentFragment();
  if (logger) frag.appendChild(h('span', { class: 'log-logger' }, `${logger} · `));
  const raw = String(text ?? '');
  const access = /^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)([\s\S]*)$/.exec(raw);
  if (access) {
    frag.appendChild(h('span', { class: 'hl hl--path' }, `${access[1]} ${access[2]}`));
    highlightInto(frag, access[3]);
  } else {
    highlightInto(frag, raw);
  }
  return frag;
}

/* =================================================================== api */

const auth = { dialog: null, pending: false };

async function api(path, options = {}) {
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(`${API}${path}`, init);
  } catch (error) {
    const failure = new Error(`无法连接运行时：${error.message}`);
    failure.offline = true;
    throw failure;
  }
  if (response.status === 401) {
    openAuth();
    const failure = new Error('需要连接运行时');
    failure.needsAuth = true;
    throw failure;
  }
  if (!response.ok) {
    let detail = '';
    try {
      const payload = await response.json();
      detail = payload?.detail?.message || payload?.detail || payload?.error?.message || '';
      if (typeof detail !== 'string') detail = JSON.stringify(detail);
    } catch (error) {
      detail = await response.text().catch(() => '');
    }
    throw new Error(`HTTP ${response.status}${detail ? ` · ${String(detail).slice(0, 240)}` : ''}`);
  }
  if (response.status === 204) return null;
  const type = response.headers.get('content-type') || '';
  return type.includes('json') ? response.json() : response.text();
}

function openAuth() {
  if (!auth.dialog || auth.dialog.open) return;
  setConn('error', '需要令牌');
  $('#auth-error').hidden = true;
  auth.dialog.showModal();
}

function closeAuth() {
  if (auth.dialog?.open) auth.dialog.close();
}

async function submitAuth(token) {
  const button = $('#auth-submit');
  button.disabled = true;
  try {
    await api('/session', { method: 'POST', body: { token } });
    closeAuth();
    setConn('busy', '已连接');
    await boot();
  } catch (error) {
    const box = $('#auth-error');
    box.textContent = error.message;
    box.hidden = false;
  } finally {
    button.disabled = false;
  }
}

/* ================================================================ chrome */

function setConn(state, text) {
  const dot = $('#conn .conn-dot');
  if (dot) dot.dataset.state = state;
  const label = $('#conn-text');
  if (label) label.textContent = text;
}

function showBanner(message, kind = 'error') {
  const banner = $('#banner');
  if (!banner) return;
  banner.dataset.kind = kind;
  banner.textContent = message;
  banner.hidden = !message;
}

function updateChrome() {
  const meta = VIEWS[S.view] || VIEWS.overview;
  $('#view-title').textContent = meta.title;
  $('#view-sub').textContent = meta.sub;
  for (const item of document.querySelectorAll('.nav-item')) {
    item.classList.toggle('is-active', item.dataset.view === S.view);
  }
  const select = $('#companion-select');
  if (select) {
    const options = S.companions.map((companion) => {
      const option = h('option', { value: companion.companion_id }, `${companion.name || companion.companion_id}`);
      option.title = companion.companion_id;
      return option;
    });
    select.replaceChildren(...options);
    if (S.companionId) select.value = S.companionId;
  }
}

function every(ms, fn) {
  if (!ms) return;
  const id = setInterval(() => {
    if (!S.auto || document.hidden) return;
    fn();
  }, ms);
  S.timers.add(id);
}

function stopTimers() {
  for (const id of S.timers) clearInterval(id);
  S.timers.clear();
}

/* ================================================================= boot */

async function boot() {
  try {
    S.companions = await api('/companions');
  } catch (error) {
    if (error.needsAuth) return;
    showBanner(error.message);
    setConn('error', '连接失败');
    return;
  }
  showBanner('');
  setConn('ok', '已连接');

  const saved = localStorage.getItem(STORE.companion);
  const known = S.companions.some((companion) => companion.companion_id === saved);
  const preferred =
    (known && saved) ||
    (S.companions.find((companion) => companion.companion_id === 'proact-demo')?.companion_id) ||
    S.companions[0]?.companion_id ||
    null;
  S.companionId = preferred;
  updateChrome();
  await render();
}

/* =============================================================== router */

async function render() {
  const gen = ++S.gen;
  stopTimers();
  updateChrome();
  const host = $('#view');
  if (S.renderedView !== S.view) {
    host.scrollTop = 0;
    S.renderedView = S.view;
  }
  clear(host);
  const meta = VIEWS[S.view] || VIEWS.overview;
  try {
    const builder = BUILDERS[S.view] || BUILDERS.overview;
    await builder(host, gen);
  } catch (error) {
    if (error.needsAuth) return;
    if (gen !== S.gen) return;
    clear(host);
    host.appendChild(emptyState('这一页没能加载', error.message));
    setConn('error', '请求失败');
  }
  if (meta.poll && VIEWS[S.view] !== 'logs' && VIEWS[S.view] !== 'events' && VIEWS[S.view] !== 'inner') {
    every(meta.poll, () => render());
  }
}

function go(view) {
  if (!VIEWS[view]) view = 'overview';
  if (S.view === view) return;
  S.view = view;
  location.hash = view;
  render();
}

/* ============================================================== overview */

async function viewOverview(host, gen) {
  const id = S.companionId;
  const [companion, inner, emotion, slow, stats, graph] = await Promise.all([
    id ? api(`/companions/${encodeURIComponent(id)}`).catch(() => null) : null,
    id ? api(`/companions/${encodeURIComponent(id)}/inner-life`).catch(() => null) : null,
    id ? api(`/companions/${encodeURIComponent(id)}/emotion`).catch(() => null) : null,
    id ? api(`/companions/${encodeURIComponent(id)}/slow-variables`).catch(() => null) : null,
    id ? api(`/companions/${encodeURIComponent(id)}/memories/stats`).catch(() => null) : null,
    api('/graph').catch(() => null),
  ]);
  if (gen !== S.gen) return;

  const grid = h('div', { class: 'grid' });

  /* companion */
  const activity = companion?.activity || {};
  grid.appendChild(
    card(
      '伙伴',
      h(
        'div',
        null,
        h(
          'div',
          { class: 'chip-row', style: 'margin-bottom:10px' },
          chip(companion?.name || id || '未选择'),
          companion?.interaction_mode ? chip(`模式 ${companion.interaction_mode}`, 'accent') : null,
          companion?.active_node_id ? chip(`节点 ${companion.active_node_id}`) : chip('无活跃节点'),
          companion?.lease_expires_at ? chip(`租约至 ${fmtTime(companion.lease_expires_at, 'short')}`, 'info') : null,
        ),
        h(
          'dl',
          { class: 'kv' },
          h('dt', null, '伙伴 ID'),
          h('dd', null, id || '—'),
          h('dt', null, '当前活动'),
          h('dd', null, Object.keys(activity).length ? Object.entries(activity).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join('  ') : '空闲'),
          h('dt', null, '版本 / 纪元'),
          h('dd', null, `${companion?.revision ?? '—'} / ${companion?.epoch ?? '—'}`),
          h('dt', null, '最近活动'),
          h('dd', null, companion?.last_activity_at ? `${fmtTime(companion.last_activity_at)}（${fmtTime(companion.last_activity_at, 'rel')}）` : '—'),
        ),
      ),
      { className: 'span-2' },
    ),
  );

  /* emotion */
  const mood = emotion?.effective_state || {};
  grid.appendChild(
    card(
      '情绪',
      h(
        'div',
        null,
        h(
          'div',
          { class: 'ring-row' },
          ring('愉悦', emotion?.state?.valence, 'ring--val'),
          ring('唤醒', emotion?.state?.arousal, 'ring--aro'),
          ring('掌控', emotion?.state?.control, 'ring--ctl'),
        ),
        h(
          'div',
          { class: 'chip-row', style: 'margin-top:12px' },
          chip(`心境 ${mood.mood || inner?.mood || '—'}`, 'accent'),
          chip(`能量 ${num(mood.energy ?? inner?.energy, 2)}`),
          chip(`社交电量 ${num(mood.social_battery ?? inner?.social_battery, 2)}`),
          emotion?.quiet_requested ? chip('静默请求', 'warn') : null,
        ),
      ),
    ),
  );

  /* inner */
  const current = inner?.current;
  grid.appendChild(
    card(
      '内心',
      h(
        'div',
        null,
        h(
          'div',
          { class: 'chip-row', style: 'margin-bottom:10px' },
          chip(inner?.dominant_kind || '—', 'accent'),
          inner?.model_assisted ? chip('模型参与', 'info') : chip('规则驱动'),
          current?.generator ? chip(current.generator, 'mono') : null,
        ),
        h('div', { class: 'bars' }, barRow('显著度', inner?.salience), barRow('冲动', inner?.urge)),
        current ? h('p', { class: 'quote', style: 'margin-top:12px' }, current.content) : h('p', { class: 'hint', style: 'margin-top:12px' }, '暂时没有正在进行的内心活动。'),
      ),
    ),
  );

  /* memory */
  const byKind = stats?.by_kind || {};
  grid.appendChild(
    card(
      '记忆',
      h(
        'div',
        null,
        h(
          'div',
          { class: 'metric-row' },
          metric(stats?.total_records ?? '—', '总数'),
          metric(stats?.active_records ?? '—', '有效'),
          metric(stats?.pinned_records ?? '—', '置顶'),
        ),
        h(
          'div',
          { class: 'bars', style: 'margin-top:14px' },
          ...KINDS.map((kind) => barRow(kind, stats?.total_records ? (byKind[kind]?.total || 0) / stats.total_records : 0)),
        ),
        h(
          'div',
          { class: 'chip-row', style: 'margin-top:12px' },
          chip(`约 ${stats?.stored_content_estimated_tokens ?? '—'} tokens`),
          stats?.token_quota?.over_budget ? chip('超出预算', 'err') : chip('预算内', 'ok'),
        ),
      ),
    ),
  );

  /* slow variables */
  grid.appendChild(
    card(
      '关系变量',
      h(
        'div',
        null,
        h(
          'div',
          { class: 'bars' },
          barRow('投入度', slow?.engagement),
          barRow('熟悉度', slow?.familiarity),
          barRow('关系温度', slow?.relationship_warmth),
        ),
        h(
          'div',
          { class: 'chip-row', style: 'margin-top:12px' },
          chip(`样本 ${slow?.sample_size ?? '—'}`),
          chip(`语气 ${slow?.tone_hint || '—'}`, 'mono'),
          slow?.enabled === false ? chip('未启用', 'warn') : null,
        ),
      ),
    ),
  );

  /* devices */
  const nodes = graph?.nodes || [];
  const online = nodes.filter((node) => node.online).length;
  grid.appendChild(
    card(
      '设备',
      h(
        'div',
        null,
        h('div', { class: 'metric-row' }, metric(nodes.length, '节点'), metric(online, '在线'), metric((graph?.edges || []).length, '连接')),
        h(
          'div',
          { class: 'chip-row', style: 'margin-top:12px' },
          ...nodes.slice(0, 8).map((node) => chip(`${node.label || node.node_id}`, node.online ? 'ok' : undefined)),
        ),
        nodes.length === 0 ? h('p', { class: 'hint', style: 'margin-top:10px' }, '还没有节点注册到该运行时。') : null,
      ),
    ),
  );

  /* recent activities */
  const activities = id ? await api(`/companions/${encodeURIComponent(id)}/inner-life/activities?limit=6`).catch(() => null) : null;
  if (gen !== S.gen) return;
  const items = activities?.items || [];
  grid.appendChild(
    card(
      '最近的内心活动',
      items.length
        ? h('div', { class: 'timeline' }, ...items.slice().reverse().map((item) => activityRow(item, true)))
        : emptyState('还没有内心活动', '内心活动由规则或模型按情绪与需求生成，出现后会显示在这里。'),
      { className: 'span-2', flush: true },
    ),
  );

  host.appendChild(grid);
}

function activityRow(item, compact) {
  return h(
    'div',
    { class: `tl-item${item.status === 'active' ? ' is-active' : ''}` },
    h(
      'div',
      { class: 'tl-head' },
      h('span', null, `#${item.sequence}`),
      chip(item.kind || '—', 'accent'),
      chip(item.trigger || '—'),
      h('span', null, fmtTime(item.created_at, 'short')),
    ),
    h('div', { class: 'tl-text' }, item.content),
    compact ? null : h('div', { class: 'bar-row', style: 'margin-top:8px;grid-template-columns:52px 1fr 44px' }, h('span', null, '显著'), h('div', { class: 'bar' }, h('div', { class: 'bar-fill', style: `width:${pct(item.salience)}` })), h('span', { class: 'bar-val' }, num(item.salience, 2))),
  );
}

/* ================================================================= logs */

async function viewLogs(host, gen) {
  const state = S.logs;
  const counters = h('span', { class: 'hint', id: 'log-count' });
  const list = h('div', { class: 'loglist', role: 'log', 'aria-live': 'polite' });

  const toolbar = h(
    'div',
    { class: 'toolbar' },
    ...LEVELS.map((level) =>
      h(
        'button',
        {
          class: `chip${state.levels.has(level) ? ' is-on' : ''}`,
          type: 'button',
          onclick: (event) => {
            if (state.levels.has(level)) state.levels.delete(level);
            else state.levels.add(level);
            event.currentTarget.classList.toggle('is-on', state.levels.has(level));
            rebuild();
          },
        },
        level,
      ),
    ),
    h('span', { class: 'dot-sep' }, '│'),
    h(
      'label',
      { class: 'search' },
      h('input', {
        type: 'search',
        placeholder: '筛选消息或来源',
        value: state.q,
        oninput: (event) => {
          state.q = event.target.value.trim().toLowerCase();
          rebuild();
        },
      }),
    ),
    h(
      'label',
      { class: 'switch' },
      h('input', {
        type: 'checkbox',
        checked: state.follow,
        onchange: (event) => {
          state.follow = event.target.checked;
          if (state.follow) list.scrollTop = list.scrollHeight;
        },
      }),
      h('span', null, '跟随'),
    ),
    h('span', { class: 'toolbar-spacer' }),
    counters,
    h('button', { class: 'btn btn-sm', type: 'button', onclick: () => { state.items = []; state.cursor = 0; rebuild(); } }, '清空'),
    h('button', { class: 'btn btn-sm', type: 'button', onclick: () => pollLogs() }, '立即拉取'),
  );

  host.append(toolbar, list);

  function matches(item) {
    if (!state.levels.has(String(item.level || '').toUpperCase())) return false;
    if (!state.q) return true;
    const haystack = `${item.message || ''} ${item.logger || ''} ${item.request_id || ''}`.toLowerCase();
    return haystack.includes(state.q);
  }

  function row(item) {
    const level = String(item.level || 'INFO').toUpperCase();
    const node = h(
      'div',
      { class: 'logrow', 'data-level': level, title: item.line || item.message || '' },
      h('span', { class: 'logtime' }, fmtTime(item.timestamp, 'ms')),
      h('span', { class: `loglv loglv--${level.toLowerCase()}` }, level.slice(0, 4)),
      h('span', { class: 'logmsg' }, renderMessage(item.message, item.logger)),
    );
    node.addEventListener('click', () => {
      const open = node.querySelector('.log-exp');
      if (open) {
        open.remove();
        node.classList.remove('is-open');
        return;
      }
      node.classList.add('is-open');
      node.appendChild(
        h(
          'div',
          { class: 'log-exp' },
          h('div', null, `#${item.sequence} · ${item.logger || '—'} · ${fmtTime(item.timestamp)}${item.request_id ? ` · req ${item.request_id}` : ''}`),
          h('div', { style: 'margin-top:6px' }, item.line || item.message || ''),
        ),
      );
    });
    return node;
  }

  function rebuild() {
    clear(list);
    const visible = state.items.filter(matches);
    if (!visible.length) {
      list.appendChild(
        emptyState(
          '还没有日志',
          '运行时把最近 2000 条日志保留在内存环形缓冲区里。请求进来、对话进行或后台任务运行时，这里会实时滚动。',
        ),
      );
    } else {
      const slice = visible.slice(-800);
      const frag = document.createDocumentFragment();
      for (const item of slice) frag.appendChild(row(item));
      list.appendChild(frag);
      if (state.follow) list.scrollTop = list.scrollHeight;
    }
    counters.textContent = `${visible.length} / ${state.items.length} 条`;
  }

  async function pollLogs() {
    try {
      const page = await api(`/flow/logs?after_sequence=${state.cursor}&limit=300`);
      if (gen !== S.gen) return;
      const items = page.items || [];
      state.cursor = page.next_sequence ?? state.cursor;
      if (items.length) {
        const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 48;
        state.items.push(...items);
        if (state.items.length > state.max) state.items.splice(0, state.items.length - state.max);
        for (const item of items) {
          if (matches(item)) {
            if (list.querySelector('.empty')) clear(list);
            list.appendChild(row(item));
          }
        }
        while (list.childElementCount > 800) list.firstElementChild?.remove();
        if (!list.childElementCount) rebuild();
        else if (state.follow && atBottom) list.scrollTop = list.scrollHeight;
        counters.textContent = `${list.querySelectorAll('.logrow').length} / ${state.items.length} 条`;
      }
      setConn('ok', `已连接 · 轮询 #${state.cursor}`);
    } catch (error) {
      if (error.needsAuth) return;
      setConn('error', '轮询失败');
    }
  }

  rebuild();
  await pollLogs();
  every(VIEWS.logs.poll, pollLogs);
}

/* =============================================================== events */

async function viewEvents(host, gen) {
  const state = S.events;
  const list = h('div', { class: 'evtlist' });
  const counter = h('span', { class: 'hint' });

  host.appendChild(
    h(
      'div',
      { class: 'toolbar' },
      h('span', { class: 'hint' }, '持久化的事件按序列号递增，重放不会重复。'),
      h('span', { class: 'toolbar-spacer' }),
      counter,
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => { state.items = []; state.cursor = 0; rebuild(); poll(); } }, '重新载入'),
    ),
  );
  host.appendChild(list);

  function typeTone(type) {
    if (/error|fail|denied/i.test(type)) return 'err';
    if (/memory|graph/i.test(type)) return 'info';
    if (/inner|emotion|proactive/i.test(type)) return 'accent';
    return undefined;
  }

  function build(item) {
    const open = state.open.has(item.sequence);
    const head = h(
      'div',
      { class: 'evt-head' },
      h('span', { class: 'evt-seq' }, `#${item.sequence}`),
      h(
        'div',
        { class: 'evt-title' },
        h('span', { class: 'evt-type' }, item.type),
        chip(typeToneLabel(item.type), typeTone(item.type)),
      ),
      h(
        'div',
        { class: 'evt-meta' },
        item.companion_id ? h('span', null, item.companion_id) : null,
        item.node_id ? h('span', null, item.node_id) : null,
        h('span', null, fmtTime(item.occurred_at, 'clock')),
      ),
    );
    const node = h('div', { class: 'evt' }, head);
    if (open) node.appendChild(h('div', { class: 'evt-body' }, jsonPretty(item.payload)));
    head.addEventListener('click', () => {
      if (state.open.has(item.sequence)) state.open.delete(item.sequence);
      else state.open.add(item.sequence);
      node.replaceWith(build(item));
    });
    return node;
  }

  function rebuild() {
    clear(list);
    if (!state.items.length) {
      list.appendChild(emptyState('还没有事件', '对话、迁移、记忆写入与主动陪伴都会产生事件。'));
    } else {
      const frag = document.createDocumentFragment();
      for (const item of state.items) frag.appendChild(build(item));
      list.appendChild(frag);
    }
    counter.textContent = `${state.items.length} 条`;
  }

  async function poll() {
    try {
      const page = await api(`/events?after_sequence=${state.cursor}&limit=200`);
      if (gen !== S.gen) return;
      const items = page.events || [];
      state.cursor = page.next_sequence ?? state.cursor;
      if (items.length) {
        state.items = [...items.slice().reverse(), ...state.items].slice(0, state.max);
        rebuild();
      }
    } catch (error) {
      if (error.needsAuth) return;
      setConn('error', '事件拉取失败');
    }
  }

  rebuild();
  await poll();
  every(VIEWS.events.poll, poll);
}

function typeToneLabel(type) {
  const parts = String(type || '').split('.');
  return parts[0] || 'event';
}

/* ============================================================= memories */

async function viewMemories(host, gen) {
  const id = S.companionId;
  if (!id) {
    host.appendChild(emptyState('没有可用的伙伴', '先在顶部选择一个伙伴。'));
    return;
  }
  const state = S.memories;

  const tabs = h(
    'div',
    { class: 'tabs' },
    ...[
      ['records', '记录'],
      ['summaries', '摘要'],
      ['diagnostics', '诊断'],
    ].map(([key, label]) =>
      h(
        'button',
        {
          class: `tab${state.tab === key ? ' is-active' : ''}`,
          type: 'button',
          onclick: () => {
            state.tab = key;
            render();
          },
        },
        label,
      ),
    ),
  );

  host.appendChild(h('div', { class: 'toolbar' }, tabs, h('span', { class: 'toolbar-spacer' })));

  const layout = h('div', { class: 'graph-layout' });
  const main = h('div', { style: 'min-width:0' });
  const side = h('div', { class: 'detail' });
  layout.append(main, side);
  host.appendChild(layout);

  const [stats, plan] = await Promise.all([
    api(`/companions/${encodeURIComponent(id)}/memories/stats`).catch(() => null),
    api(`/companions/${encodeURIComponent(id)}/memories/consolidation-plan`).catch(() => null),
  ]);
  if (gen !== S.gen) return;

  side.appendChild(
    card(
      '记忆统计',
      h(
        'div',
        null,
        h('div', { class: 'metric-row' }, metric(stats?.total_records ?? '—', '总数'), metric(stats?.active_records ?? '—', '有效'), metric(stats?.pinned_records ?? '—', '置顶')),
        h(
          'div',
          { class: 'bars', style: 'margin-top:14px' },
          ...KINDS.map((kind) =>
            barRow(kind, stats?.total_records ? (stats.by_kind?.[kind]?.total || 0) / stats.total_records : 0),
          ),
        ),
        h(
          'dl',
          { class: 'kv', style: 'margin-top:14px' },
          h('dt', null, '内容体积'),
          h('dd', null, `${stats?.content_utf8_bytes ?? '—'} B · ~${stats?.stored_content_estimated_tokens ?? '—'} tok`),
          h('dt', null, '来源事件'),
          h('dd', null, String(stats?.source_event_count ?? '—')),
          h('dt', null, '今日预算'),
          h('dd', null, `${stats?.token_quota?.spent_tokens ?? '—'} / ${stats?.token_quota?.daily_token_limit ?? '—'}`),
        ),
      ),
    ),
  );

  side.appendChild(
    card(
      '合并计划',
      h(
        'div',
        null,
        h(
          'div',
          { class: 'chip-row' },
          chip(`${plan?.eligible_episode_count ?? 0} 条可分集`, plan?.triggered ? 'ok' : undefined),
          chip(`阈值 ${plan?.threshold ?? '—'}`),
          plan?.execution_enabled ? chip('可执行', 'accent') : chip('未启用', 'warn'),
        ),
        h('p', { class: 'hint', style: 'margin-top:10px' }, plan?.reason || '没有需要合并的情节片段。'),
      ),
    ),
  );

  if (state.tab === 'records') await renderRecords(main, gen);
  else if (state.tab === 'summaries') await renderSummaries(main, gen);
  else await renderDiagnostics(main, gen);
}

function kindTone(kind) {
  return { episode: 'info', fact: 'accent', preference: 'ok', relationship: 'warn' }[kind];
}

async function renderRecords(main, gen) {
  const id = S.companionId;
  const state = S.memories;
  const listBox = h('div', { class: 'memlist' });
  const pager = h('div', { class: 'pager' });

  const toolbar = h(
    'div',
    { class: 'toolbar', style: 'position:static;margin:0 0 12px;padding:0 0 12px;background:transparent;backdrop-filter:none' },
    h(
      'div',
      { class: 'chip-row' },
      h('button', { class: `chip${state.kind === '' ? ' is-on' : ''}`, type: 'button', onclick: () => { state.kind = ''; state.page = 0; reload(); } }, '全部'),
      ...KINDS.map((kind) =>
        h(
          'button',
          { class: `chip${state.kind === kind ? ' is-on' : ''}`, type: 'button', onclick: () => { state.kind = kind; state.page = 0; reload(); } },
          kind,
        ),
      ),
    ),
    h(
      'label',
      { class: 'search' },
      h('input', {
        type: 'search',
        placeholder: '在已加载的记录里搜索',
        value: state.q,
        oninput: (event) => {
          state.q = event.target.value.trim().toLowerCase();
          paint();
        },
      }),
    ),
  );

  main.append(toolbar, listBox, pager);

  let payload = { items: [], total: 0 };

  function makeCard(item) {
    const open = state.open.has(item.memory_id);
    const cardNode = h(
      'article',
      { class: `mem${open ? ' is-open' : ''}` },
      h(
        'div',
        { class: 'mem-head' },
        chip(item.kind || '—', kindTone(item.kind)),
        item.layer ? chip(item.layer, 'mono') : null,
        item.active === false ? chip('已失效', 'err') : null,
        item.pinned ? chip('置顶', 'accent') : null,
        item.confirmed ? chip('已确认', 'ok') : null,
        item.effective_retention_policy ? chip(String(item.effective_retention_policy), 'mono') : null,
      ),
      h('div', { class: 'mem-content' }, open ? item.content : String(item.content || '').slice(0, 220) + (String(item.content || '').length > 220 ? '…' : '')),
      h(
        'div',
        { class: 'mem-meta' },
        h('span', null, fmtTime(item.occurred_at || item.created_at, 'short')),
        h('span', { class: 'mem-mini' }, '重要', h('div', { class: 'bar' }, h('div', { class: 'bar-fill', style: `width:${pct(item.importance)}` }))),
        h('span', { class: 'mem-mini' }, '置信', h('div', { class: 'bar' }, h('div', { class: 'bar-fill bar-fill--ok', style: `width:${pct(item.confidence)}` }))),
        h('span', { class: 'mono' }, shortId(item.memory_id, 14)),
        item.semantic_key ? h('span', { class: 'mono' }, item.semantic_key) : null,
      ),
      open
        ? h(
            'div',
            { style: 'margin-top:10px' },
            jsonPretty({
              memory_id: item.memory_id,
              kind: item.kind,
              layer: item.layer,
              revision: item.revision,
              occurred_at: item.occurred_at,
              valid_from: item.valid_from,
              valid_to: item.valid_to,
              retention_policy: item.retention_policy,
              effective_retention_policy: item.effective_retention_policy,
              tags: item.tags,
              source_event_ids: item.source_event_ids,
            }),
          )
        : null,
    );
    cardNode.addEventListener('click', () => {
      if (state.open.has(item.memory_id)) state.open.delete(item.memory_id);
      else state.open.add(item.memory_id);
      const fresh = makeCard(item);
      cardNode.replaceWith(fresh);
    });
    return cardNode;
  }

  function paint() {
    clear(listBox);
    const items = payload.items.filter((item) => !state.q || String(item.content || '').toLowerCase().includes(state.q));
    if (!items.length) {
      listBox.appendChild(emptyState('没有记忆记录', '对话、观察与主动陪伴会写入情节记忆；自动记忆会从中提炼事实和关系。'));
      return;
    }
    const frag = document.createDocumentFragment();
    for (const item of items) frag.appendChild(makeCard(item));
    listBox.appendChild(frag);
  }

  function paintPager() {
    const total = payload.total ?? 0;
    const pages = Math.max(1, Math.ceil(total / state.limit));
    const page = state.page;
    clear(pager).append(
      h('span', null, `共 ${total} 条 · 第 ${page + 1}/${pages} 页`),
      h('button', { class: 'btn btn-sm', type: 'button', disabled: page <= 0, onclick: () => { state.page -= 1; reload(); } }, '上一页'),
      h('button', { class: 'btn btn-sm', type: 'button', disabled: page >= pages - 1, onclick: () => { state.page += 1; reload(); } }, '下一页'),
    );
  }

  async function reload() {
    const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.page * state.limit) });
    if (state.kind) params.set('kind', state.kind);
    try {
      payload = await api(`/companions/${encodeURIComponent(id)}/memories?${params}`);
      if (gen !== S.gen) return;
      paint();
      paintPager();
    } catch (error) {
      if (error.needsAuth) return;
      clear(listBox).appendChild(emptyState('读取失败', error.message));
    }
  }

  await reload();
}

async function renderSummaries(main, gen) {
  const id = S.companionId;
  const box = h('div', { class: 'memlist' });
  main.appendChild(box);
  try {
    const page = await api(`/companions/${encodeURIComponent(id)}/memories/summaries?limit=50`);
    if (gen !== S.gen) return;
    const items = page.items || [];
    if (!items.length) {
      box.appendChild(emptyState('还没有摘要', '当情节记忆积累到阈值，运行时会把它们合并成一段摘要。'));
      return;
    }
    for (const item of items) {
      box.appendChild(
        h(
          'article',
          { class: 'mem' },
          h('div', { class: 'mem-head' }, chip('summary', 'accent'), chip(`${item.memory_versions?.length ?? 0} 个版本`), h('span', { class: 'mono', style: 'color:var(--muted);font-size:10.5px' }, fmtTime(item.created_at, 'short'))),
          h('div', { class: 'mem-content' }, item.content),
        ),
      );
    }
  } catch (error) {
    if (error.needsAuth) return;
    box.appendChild(emptyState('读取失败', error.message));
  }
}

async function renderDiagnostics(main, gen) {
  const id = S.companionId;
  const wrap = h('div', { class: 'table-wrap' });
  main.appendChild(wrap);
  try {
    const page = await api(`/companions/${encodeURIComponent(id)}/memories/logs?limit=120`);
    if (gen !== S.gen) return;
    const items = (page.items || []).slice().reverse();
    if (!items.length) {
      main.replaceChildren(emptyState('还没有记忆诊断日志', '记忆写入、召回与合并操作都会留下带耗时的诊断记录。'));
      return;
    }
    wrap.appendChild(
      h(
        'table',
        null,
        h('thead', null, h('tr', null, h('th', null, '序号'), h('th', null, '时间'), h('th', null, '操作'), h('th', null, '状态'), h('th', null, '耗时'), h('th', null, '详情'))),
        h(
          'tbody',
          null,
          ...items.map((item) =>
            h(
              'tr',
              null,
              h('td', { class: 'mono' }, String(item.sequence)),
              h('td', { class: 'mono' }, fmtTime(item.created_at, 'short')),
              h('td', null, h('span', { class: 'mono' }, item.operation)),
              h('td', null, chip(item.status, item.status === 'ok' || item.status === 'success' ? 'ok' : item.status === 'error' ? 'err' : 'info')),
              h('td', { class: 'mono' }, item.duration_ms == null ? '—' : `${item.duration_ms} ms`),
              h('td', { class: 'mono' }, item.details && Object.keys(item.details).length ? JSON.stringify(item.details).slice(0, 220) : '—'),
            ),
          ),
        ),
      ),
    );
  } catch (error) {
    if (error.needsAuth) return;
    main.replaceChildren(emptyState('读取失败', error.message));
  }
}

/* ================================================================ graph */

async function viewGraph(host, gen) {
  const id = S.companionId;
  if (!id) {
    host.appendChild(emptyState('没有可用的伙伴', '先在顶部选择一个伙伴。'));
    return;
  }
  const state = S.graph;

  const toolbar = h(
    'div',
    { class: 'toolbar' },
    h(
      'div',
      { class: 'chip-row' },
      ...[1, 2, 3].map((hops) =>
        h(
          'button',
          {
            class: `chip${state.hops === hops ? ' is-on' : ''}`,
            type: 'button',
            onclick: () => {
              state.hops = hops;
              render();
            },
          },
          `${hops} 跳`,
        ),
      ),
    ),
    h(
      'label',
      { class: 'search' },
      h('input', {
        type: 'search',
        placeholder: '按实体或关系搜索',
        value: state.query,
        onchange: (event) => {
          state.query = event.target.value.trim();
          render();
        },
      }),
    ),
    h('span', { class: 'toolbar-spacer' }),
    h('span', { class: 'hint' }, '滚轮缩放 · 拖动平移 · 点击节点查看证据'),
    h('button', { class: 'btn btn-sm', type: 'button', onclick: () => { state.selected = null; state.zoom = 1; state.panX = 0; state.panY = 0; render(); } }, '重置视图'),
  );

  const layout = h('div', { class: 'graph-layout' });
  const wrap = h('div', { class: 'graphwrap' });
  const side = h('div', { class: 'detail' });
  layout.append(wrap, side);
  host.append(toolbar, layout);

  const params = new URLSearchParams({ limit: '200', hops: String(state.hops) });
  if (state.query) params.set('query', state.query);
  let snapshot;
  try {
    snapshot = await api(`/companions/${encodeURIComponent(id)}/memories/knowledge-graph?${params}`);
  } catch (error) {
    if (error.needsAuth) return;
    wrap.appendChild(emptyState('读取失败', error.message));
    return;
  }
  if (gen !== S.gen) return;

  const entities = snapshot.entities || [];
  const relations = snapshot.relations || [];

  if (!entities.length) {
    wrap.appendChild(
      emptyState(
        state.query ? '没有匹配的实体' : '这个伙伴还没有知识图谱',
        '知识图谱由自动记忆从情节记忆里提炼：实体（人物、地点、事物）和它们之间的关系。记忆积累后会出现。',
      ),
    );
    side.appendChild(
      h('div', { class: 'detail-empty' }, '图谱为空时，这里会显示实体的别名、版本和关系证据。'),
    );
    return;
  }

  const model = layoutGraph(entities, relations, wrap);
  renderGraph(wrap, model, relations);
  paintDetail(side, model, relations, state);
}

function layoutGraph(entities, relations, wrap) {
  const width = Math.max(520, wrap.clientWidth || 720);
  const height = 470;
  const byId = new Map();
  const nodes = entities.map((entity, index) => {
    const angle = (index / Math.max(1, entities.length)) * Math.PI * 2;
    const radius = 120 + (index % 3) * 34;
    const node = {
      id: entity.entity_id,
      entity,
      label: entity.name || shortId(entity.entity_id, 8),
      x: width / 2 + Math.cos(angle) * radius,
      y: height / 2 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      degree: 0,
    };
    byId.set(entity.entity_id, node);
    return node;
  });

  const edges = [];
  for (const relation of relations) {
    const source = byId.get(relation.subject_entity_id) || nodes.find((node) => node.label === relation.subject);
    const target = byId.get(relation.object_entity_id) || nodes.find((node) => node.label === relation.object);
    if (!source || !target) continue;
    source.degree += 1;
    target.degree += 1;
    edges.push({ relation, source, target });
  }

  // A small deterministic force simulation: repulsion between nodes, springs
  // along relations, weak gravity to the centre. Runs synchronously — graphs
  // this size converge in well under a frame's worth of work.
  const iterations = 260;
  const centreX = width / 2;
  const centreY = height / 2;
  for (let step = 0; step < iterations; step += 1) {
    const cooling = 1 - step / iterations;
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.hypot(dx, dy) || 0.01;
        const force = 5200 / (dist * dist);
        dx /= dist;
        dy /= dist;
        a.vx -= dx * force;
        a.vy -= dy * force;
        b.vx += dx * force;
        b.vy += dy * force;
      }
      a.vx += (centreX - a.x) * 0.0022;
      a.vy += (centreY - a.y) * 0.0022;
    }
    for (const edge of edges) {
      const { source, target } = edge;
      let dx = target.x - source.x;
      let dy = target.y - source.y;
      const dist = Math.hypot(dx, dy) || 0.01;
      const desired = 150;
      const force = (dist - desired) * 0.012;
      dx /= dist;
      dy /= dist;
      source.vx += dx * force;
      source.vy += dy * force;
      target.vx -= dx * force;
      target.vy -= dy * force;
    }
    for (const node of nodes) {
      node.vx *= 0.82;
      node.vy *= 0.82;
      node.x += node.vx * cooling;
      node.y += node.vy * cooling;
      node.x = Math.max(56, Math.min(width - 56, node.x));
      node.y = Math.max(40, Math.min(height - 40, node.y));
    }
  }

  return { width, height, nodes, edges, byId };
}

function renderGraph(wrap, model, relations) {
  const state = S.graph;
  const root = ns('svg', { viewBox: `0 0 ${model.width} ${model.height}`, role: 'img', 'aria-label': '知识图谱' });
  const scene = ns('g', { transform: `translate(${state.panX} ${state.panY}) scale(${state.zoom})` });
  root.appendChild(scene);

  const edgeGroup = ns('g');
  const nodeGroup = ns('g');
  scene.append(edgeGroup, nodeGroup);

  const edgeItems = [];
  for (const edge of model.edges) {
    const line = ns('line', { x1: edge.source.x, y1: edge.source.y, x2: edge.target.x, y2: edge.target.y });
    const group = ns('g', { class: 'gedge' }, line);
    const label = ns(
      'text',
      {
        class: 'gedge-label',
        x: (edge.source.x + edge.target.x) / 2,
        y: (edge.source.y + edge.target.y) / 2 - 4,
      },
      edge.relation.predicate,
    );
    group.appendChild(label);
    edgeGroup.appendChild(group);
    edgeItems.push({ group, edge });
  }

  const nodeItems = [];
  for (const node of model.nodes) {
    const radius = 13 + Math.min(9, node.degree * 2.6);
    const group = ns('g', { class: 'gnode', transform: `translate(${node.x} ${node.y})` });
    group.append(
      ns('circle', { r: radius }),
      ns('text', { y: radius + 13 }, node.label.length > 14 ? `${node.label.slice(0, 13)}…` : node.label),
    );
    group.addEventListener('click', (event) => {
      event.stopPropagation();
      state.selected = state.selected === node.id ? null : node.id;
      paintSelection();
      paintDetail($('.detail'), model, relations, state);
    });
    group.addEventListener('mouseenter', () => hover(node.id));
    group.addEventListener('mouseleave', () => hover(null));
    nodeGroup.appendChild(group);
    nodeItems.push({ group, node });
  }

  let hovered = null;
  function hover(id) {
    hovered = id;
    paintSelection();
  }

  function paintSelection() {
    const focus = hovered || state.selected;
    for (const item of nodeItems) {
      const active = focus && item.node.id === focus;
      item.group.classList.toggle('is-active', Boolean(active || (state.selected && item.node.id === state.selected)));
      item.group.classList.toggle('is-dim', Boolean(focus && !active && !(state.selected && item.node.id === state.selected)));
    }
    for (const item of edgeItems) {
      const touches = focus && (item.edge.source.id === focus || item.edge.target.id === focus);
      item.group.classList.toggle('is-active', Boolean(touches));
      item.group.classList.toggle('is-dim', Boolean(focus && !touches));
    }
  }

  /* pan + zoom */
  let panning = false;
  let start = null;
  root.addEventListener('pointerdown', (event) => {
    panning = true;
    start = { x: event.clientX, y: event.clientY, panX: state.panX, panY: state.panY };
    root.classList.add('is-panning');
    root.setPointerCapture(event.pointerId);
  });
  root.addEventListener('pointermove', (event) => {
    if (!panning || !start) return;
    state.panX = start.panX + (event.clientX - start.x);
    state.panY = start.panY + (event.clientY - start.y);
    scene.setAttribute('transform', `translate(${state.panX} ${state.panY}) scale(${state.zoom})`);
  });
  root.addEventListener('pointerup', () => {
    panning = false;
    start = null;
    root.classList.remove('is-panning');
  });
  root.addEventListener(
    'wheel',
    (event) => {
      event.preventDefault();
      const next = Math.max(0.4, Math.min(2.6, state.zoom * (event.deltaY > 0 ? 0.92 : 1.08)));
      state.zoom = next;
      scene.setAttribute('transform', `translate(${state.panX} ${state.panY}) scale(${state.zoom})`);
    },
    { passive: false },
  );
  root.addEventListener('click', () => {
    if (state.selected) {
      state.selected = null;
      paintSelection();
      paintDetail($('.detail'), model, relations, state);
    }
  });

  wrap.replaceChildren(root);
  wrap.appendChild(
    h(
      'div',
      { class: 'glegend' },
      h('span', null, `${model.nodes.length} 实体`),
      h('span', { class: 'dot-sep' }, '·'),
      h('span', null, `${model.edges.length} 关系`),
      h('span', { class: 'dot-sep' }, '·'),
      h('span', null, `${S.graph.hops} 跳`),
    ),
  );
}

function paintDetail(side, model, relations, state) {
  if (!side) return;
  const node = state.selected ? model.byId.get(state.selected) : null;
  clear(side);
  if (!node) {
    side.appendChild(
      h(
        'div',
        { class: 'detail-empty' },
        '点击一个实体，查看它的别名、版本与关系证据。',
        h('br'),
        h('span', { class: 'hint' }, '关系上的标签是谓词，例如 has_name。'),
      ),
    );
    return;
  }
  const entity = node.entity;
  const touching = relations.filter(
    (relation) => relation.subject_entity_id === entity.entity_id || relation.object_entity_id === entity.entity_id,
  );
  side.appendChild(
    card(
      entity.name || entity.entity_id,
      h(
        'div',
        null,
        h(
          'div',
          { class: 'chip-row', style: 'margin-bottom:10px' },
          chip(`度 ${node.degree}`, 'accent'),
          chip(`rev ${entity.revision ?? '—'}`, 'mono'),
          entity.updated_at ? chip(fmtTime(entity.updated_at, 'rel'), 'mono') : null,
        ),
        entity.aliases?.length ? h('div', { class: 'chip-row', style: 'margin-bottom:10px' }, ...entity.aliases.map((alias) => chip(alias, 'info'))) : null,
        h('dl', { class: 'kv' }, h('dt', null, '实体 ID'), h('dd', null, entity.entity_id)),
        touching.length
          ? h(
              'div',
              { style: 'margin-top:12px;display:flex;flex-direction:column;gap:6px' },
              ...touching.map((relation) =>
                h(
                  'div',
                  { class: 'rel' },
                  h('span', null, relation.subject),
                  h('span', { class: 'rel-pred' }, relation.predicate),
                  h('span', null, relation.object),
                  chip(num(relation.confidence, 2), 'ok'),
                ),
              ),
            )
          : h('p', { class: 'hint', style: 'margin-top:12px' }, '这个实体还没有关系。'),
      ),
    ),
  );

  const evidence = touching.flatMap((relation) => relation.memory_versions || []);
  if (evidence.length) {
    side.appendChild(
      card(
        '证据记忆',
        h(
          'div',
          { style: 'display:flex;flex-direction:column;gap:6px' },
          ...evidence.slice(0, 8).map((entry) =>
            h('div', { class: 'rel' }, h('span', { class: 'mono' }, entry.memory_id), chip(`rev ${entry.revision}`, 'mono')),
          ),
        ),
      ),
    );
  }
}

/* ================================================================= inner */

async function viewInner(host, gen) {
  const id = S.companionId;
  if (!id) {
    host.appendChild(emptyState('没有可用的伙伴', '先在顶部选择一个伙伴。'));
    return;
  }
  const state = S.inner;

  async function load() {
    const [inner, activities] = await Promise.all([
      api(`/companions/${encodeURIComponent(id)}/inner-life`),
      api(`/companions/${encodeURIComponent(id)}/inner-life/activities?limit=25`),
    ]);
    if (gen !== S.gen) return;
    paint(inner, activities);
  }

  function paint(inner, activities) {
    clear(host);
    const current = inner.current;
    const grid = h('div', { class: 'grid' });

    grid.appendChild(
      card(
        '状态',
        h(
          'div',
          null,
          h(
            'div',
            { class: 'chip-row', style: 'margin-bottom:12px' },
            inner.enabled ? chip('内心活动已启用', 'ok') : chip('已停用', 'warn'),
            inner.model_assisted ? chip('模型参与', 'info') : chip('规则驱动'),
            chip(`主导 ${inner.dominant_kind || '—'}`, 'accent'),
            chip(`心境 ${inner.mood || '—'}`),
          ),
          h(
            'div',
            { class: 'metric-row' },
            metric(num(inner.energy, 2), '能量'),
            metric(num(inner.social_battery, 2), '社交电量'),
            metric(num(inner.salience, 2), '显著度'),
            metric(num(inner.urge, 2), '冲动'),
          ),
          h(
            'dl',
            { class: 'kv', style: 'margin-top:14px' },
            h('dt', null, '计算时间'),
            h('dd', null, fmtTime(inner.computed_at)),
            h('dt', null, '空闲时长'),
            h('dd', null, inner.idle_seconds == null ? '—' : `${Math.round(inner.idle_seconds / 60)} 分钟`),
            h('dt', null, '最近交互'),
            h('dd', null, fmtTime(inner.last_interaction_at)),
            h('dt', null, '上下文版本'),
            h('dd', null, shortId(inner.context_version, 16)),
          ),
        ),
        { className: 'span-2' },
      ),
    );

    const needs = inner.needs || {};
    const needEntries = Object.entries(needs);
    grid.appendChild(
      card(
        '需求',
        needEntries.length
          ? h(
              'div',
              null,
              radarChart(needEntries.map(([key]) => key), needEntries.map(([, value]) => Number(value) || 0)),
              h('div', { class: 'bars', style: 'margin-top:12px' }, ...needEntries.map(([key, value]) => barRow(key, value))),
            )
          : emptyState('没有需求数据', '需求在内心活动计算后出现。'),
      ),
    );

    grid.appendChild(
      card(
        '当前活动',
        current
          ? h(
              'div',
              null,
              h(
                'div',
                { class: 'chip-row', style: 'margin-bottom:10px' },
                chip(current.kind || '—', 'accent'),
                chip(current.trigger || '—'),
                chip(current.generator || '—', 'mono'),
                chip(current.status || '—'),
                h('span', { class: 'mono', style: 'color:var(--muted);font-size:10.5px' }, `#${current.sequence}`),
              ),
              h('p', { class: 'quote' }, current.content),
              h('div', { class: 'bars', style: 'margin-top:12px' }, barRow('显著度', current.salience), barRow('冲动', current.urge)),
              current.thinking
                ? h(
                    'div',
                    { style: 'margin-top:12px' },
                    h('div', { class: 'chip-row', style: 'margin-bottom:6px' }, chip('思考通道', 'warn')),
                    h('pre', { class: 'thinking' }, current.thinking),
                  )
                : null,
            )
          : emptyState('现在没有内心活动', '伙伴在空闲一段时间后会产生内心活动。'),
      ),
    );

    const items = (activities.items || []).slice().reverse();
    grid.appendChild(
      card(
        '时间线',
        items.length
          ? h(
              'div',
              { class: 'timeline' },
              ...items.map((item) => {
                const open = state.thinking.has(item.activity_id);
                const node = h('div', { class: `tl-item${item.status === 'active' ? ' is-active' : ''}` });
                node.append(
                  h(
                    'div',
                    { class: 'tl-head' },
                    h('span', null, `#${item.sequence}`),
                    chip(item.kind || '—', 'accent'),
                    chip(item.trigger || '—'),
                    chip(item.generator || '—', 'mono'),
                    h('span', null, fmtTime(item.created_at, 'short')),
                  ),
                  h('div', { class: 'tl-text' }, item.content),
                  h('div', { class: 'bars', style: 'margin-top:8px' }, barRow('显著度', item.salience), barRow('冲动', item.urge)),
                  item.thinking
                    ? h(
                        'div',
                        { class: 'tl-body' },
                        open
                          ? h('pre', { class: 'thinking' }, item.thinking)
                          : h(
                              'button',
                              {
                                class: 'btn btn-sm',
                                type: 'button',
                                onclick: (event) => {
                                  event.stopPropagation();
                                  state.thinking.add(item.activity_id);
                                  render();
                                },
                              },
                              '展开思考通道',
                            ),
                      )
                    : null,
                );
                return node;
              }),
            )
          : emptyState('还没有内心活动', '规则或模型产生的内心活动会按序列号累积在这里。'),
        { className: 'span-all' },
      ),
    );

    host.appendChild(grid);
  }

  await load();
  every(VIEWS.inner.poll, load);
}

function radarChart(labels, values) {
  const size = 240;
  const cx = size / 2;
  const cy = size / 2;
  const radius = size / 2 - 34;
  const count = labels.length;
  const svg = ns('svg', { class: 'radar', viewBox: `0 0 ${size} ${size}` });
  const point = (index, value) => {
    const angle = (Math.PI * 2 * index) / count - Math.PI / 2;
    return [cx + Math.cos(angle) * radius * value, cy + Math.sin(angle) * radius * value];
  };
  for (const level of [0.25, 0.5, 0.75, 1]) {
    const points = labels.map((_, index) => point(index, level).join(',')).join(' ');
    svg.appendChild(ns('polygon', { class: 'gridline', points }));
  }
  for (let index = 0; index < count; index += 1) {
    const [x, y] = point(index, 1);
    svg.appendChild(ns('line', { class: 'axis', x1: cx, y1: cy, x2: x, y2: y }));
    const [lx, ly] = point(index, 1.16);
    svg.appendChild(ns('text', { x: lx, y: ly + 3 }, labels[index]));
  }
  const shape = labels.map((_, index) => point(index, Math.max(0.02, Math.min(1, values[index] || 0))).join(',')).join(' ');
  svg.appendChild(ns('polygon', { class: 'shape', points: shape }));
  return svg;
}

/* =============================================================== devices */

async function viewDevices(host, gen) {
  const snapshot = await api('/graph');
  if (gen !== S.gen) return;
  const nodes = snapshot.nodes || [];
  const edges = snapshot.edges || [];

  if (!nodes.length) {
    host.appendChild(emptyState('还没有节点', '客户端（Android、Linux、system-node）注册后，会出现在这里。'));
    return;
  }

  const online = nodes.filter((node) => node.online);
  const kinds = new Map();
  for (const node of nodes) {
    const kind = node.kind || 'other';
    kinds.set(kind, (kinds.get(kind) || 0) + 1);
  }
  const reach = new Map();
  for (const edge of edges) {
    for (const side of [edge.source_node_id, edge.target_node_id]) {
      const entry = reach.get(side) || { up: 0, total: 0 };
      entry.total += 1;
      if (edge.available) entry.up += 1;
      reach.set(side, entry);
    }
  }
  const blocked = edges.filter((edge) => !edge.available);
  const reasons = new Map();
  for (const edge of blocked) {
    const reason = edge.reason || '未说明';
    reasons.set(reason, (reasons.get(reason) || 0) + 1);
  }

  const state = { q: '' };
  const tbody = h('tbody');
  const count = h('span', { class: 'hint' });
  const detailRow = (node, colSpan) => {
    const pair = reach.get(node.node_id) || { up: 0, total: 0 };
    return h(
      'tr',
      { class: 'is-open' },
      h(
        'td',
        { colspan: String(colSpan) },
        h(
          'dl',
          { class: 'kv' },
          h('dt', null, 'node_id'),
          h('dd', null, node.node_id),
          h('dt', null, '可达连接'),
          h('dd', null, `${pair.up} / ${pair.total}`),
          h('dt', null, '注册时间'),
          h('dd', null, fmtTime(node.created_at, 'short')),
          h('dt', null, '最近心跳'),
          h('dd', null, fmtTime(node.last_seen_at, 'full')),
          node.device && (node.device.device_id || (node.device.ip_addresses || []).length) ? h('dt', null, '设备') : null,
          node.device && (node.device.device_id || (node.device.ip_addresses || []).length)
            ? h('dd', null, node.device.device_id || (node.device.ip_addresses || []).join(', '))
            : null,
          node.device && (node.device.mac_addresses || []).length ? h('dt', null, 'MAC') : null,
          node.device && (node.device.mac_addresses || []).length ? h('dd', null, node.device.mac_addresses.join(', ')) : null,
          (node.accesses || []).length ? h('dt', null, '接入') : null,
          (node.accesses || []).length ? h('dd', null, node.accesses.map((access) => access.access_id || access.kind || '—').join(', ')) : null,
        ),
        h('pre', { class: 'json', style: 'margin-top:10px' }, jsonPretty({ context: node.context, capabilities: node.capabilities, device: node.device })),
      ),
    );
  };

  const redraw = () => {
    const q = state.q.trim().toLowerCase();
    const list = nodes
      .filter((node) => {
        if (!q) return true;
        const haystack = [node.node_id, node.label, node.kind, node.room_state, ...(node.capabilities || [])]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return haystack.includes(q);
      })
      .sort((left, right) => Number(Boolean(right.online)) - Number(Boolean(left.online)) || String(left.label || left.node_id).localeCompare(String(right.label || right.node_id)));

    clear(tbody);
    const shown = list.slice(0, 400);
    for (const node of shown) {
      const pair = reach.get(node.node_id) || { up: 0, total: 0 };
      const row = h(
        'tr',
        { class: 'is-clickable' },
        h(
          'td',
          null,
          h('div', { class: 'mem-title' }, node.label || shortId(node.node_id, 14)),
          h('div', { class: 'mono hint' }, node.node_id),
        ),
        h('td', null, chip(node.kind || '—', 'mono')),
        h('td', null, node.online ? chip('在线', 'ok') : chip('离线')),
        h('td', null, node.room_state ? chip(node.room_state) : '—'),
        h('td', { class: 'mono' }, `${pair.up}/${pair.total}`),
        h(
          'td',
          null,
          (node.capabilities || []).length
            ? h('div', { class: 'chip-row' }, ...node.capabilities.slice(0, 4).map((cap) => chip(cap, 'mono')), node.capabilities.length > 4 ? chip(`+${node.capabilities.length - 4}`, 'mono') : null)
            : '—',
        ),
        h('td', { class: 'mono' }, fmtTime(node.last_seen_at, 'short')),
      );
      let expanded = null;
      row.addEventListener('click', () => {
        if (expanded) {
          expanded.remove();
          expanded = null;
          row.classList.remove('is-open');
          return;
        }
        expanded = detailRow(node, 7);
        row.after(expanded);
        row.classList.add('is-open');
      });
      tbody.appendChild(row);
    }
    count.textContent = list.length > shown.length ? `显示 ${shown.length} / ${list.length} 个节点（搜索可收窄）` : `${list.length} 个节点`;
  };

  host.appendChild(
    h(
      'div',
      { class: 'grid', style: 'margin-bottom:14px' },
      h(
        'div',
        { class: 'card' },
        h(
          'div',
          { class: 'card-body' },
          h('div', { class: 'metric-label' }, '节点'),
          h('div', { class: 'metric-value' }, String(nodes.length)),
          h('div', { class: 'metric-sub' }, `${online.length} 个在线`),
        ),
      ),
      h(
        'div',
        { class: 'card' },
        h(
          'div',
          { class: 'card-body' },
          h('div', { class: 'metric-label' }, '类型分布'),
          h('div', { class: 'chip-row' }, ...[...kinds.entries()].sort((a, b) => b[1] - a[1]).map(([kind, n]) => chip(`${kind} · ${n}`, 'mono'))),
        ),
      ),
      h(
        'div',
        { class: 'card' },
        h(
          'div',
          { class: 'card-body' },
          h('div', { class: 'metric-label' }, '连接可达性'),
          h('div', { class: 'metric-value' }, `${edges.length - blocked.length}/${edges.length}`),
          h('div', { class: 'metric-sub' }, '可用 / 全部有向连接'),
          reasons.size ? h('div', { class: 'chip-row', style: 'margin-top:8px' }, ...[...reasons.entries()].sort((a, b) => b[1] - a[1]).slice(0, 4).map(([reason, n]) => chip(`${reason} · ${n}`, 'warn'))) : null,
        ),
      ),
    ),
  );

  host.appendChild(
    h(
      'div',
      { class: 'toolbar', style: 'position:static' },
      h(
        'div',
        { class: 'search' },
        h('input', {
          type: 'search',
          placeholder: '搜索节点、类型、能力…',
          value: state.q,
          oninput: (event) => {
            state.q = event.target.value;
            redraw();
          },
        }),
      ),
      h('div', { class: 'toolbar-spacer' }),
      count,
    ),
  );

  host.appendChild(
    h(
      'div',
      { class: 'table-wrap' },
      h(
        'table',
        null,
        h('thead', null, h('tr', null, ...['节点', '类型', '状态', '房间', '可达', '能力', '最近心跳'].map((title) => h('th', null, title)))),
        tbody,
      ),
    ),
  );

  redraw();
}

/* ================================================================ wiring */

const BUILDERS = {
  overview: viewOverview,
  logs: viewLogs,
  events: viewEvents,
  memories: viewMemories,
  graph: viewGraph,
  inner: viewInner,
  devices: viewDevices,
};

function bindUi() {
  auth.dialog = $('#auth-dialog');
  document.body.classList.toggle('embed', new URLSearchParams(location.search).has('embed'));

  for (const button of document.querySelectorAll('.nav-item')) {
    button.addEventListener('click', () => go(button.dataset.view));
  }

  $('#btn-refresh').addEventListener('click', () => render());
  $('#btn-theme').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(STORE.theme, next);
    } catch (error) {
      /* storage may be unavailable in private mode */
    }
  });

  $('#auto-refresh').addEventListener('change', (event) => {
    S.auto = event.target.checked;
  });

  $('#companion-select').addEventListener('change', (event) => {
    S.companionId = event.target.value || null;
    try {
      localStorage.setItem(STORE.companion, S.companionId || '');
    } catch (error) {
      /* ignore */
    }
    render();
  });

  $('#btn-session').addEventListener('click', () => openAuth());
  $('#auth-cancel').addEventListener('click', () => closeAuth());
  $('#auth-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const token = $('#auth-token').value.trim();
    if (!token) {
      const box = $('#auth-error');
      box.textContent = '请输入管理令牌';
      box.hidden = false;
      return;
    }
    submitAuth(token);
  });

  window.addEventListener('hashchange', () => {
    const view = location.hash.replace('#', '') || 'overview';
    if (view !== S.view) {
      S.view = VIEWS[view] ? view : 'overview';
      render();
    }
  });
}

function start() {
  bindUi();
  const initial = location.hash.replace('#', '') || 'overview';
  S.view = VIEWS[initial] ? initial : 'overview';
  boot().catch((error) => {
    const host = $('#view');
    clear(host).appendChild(emptyState('初始化失败', error.message));
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
else start();