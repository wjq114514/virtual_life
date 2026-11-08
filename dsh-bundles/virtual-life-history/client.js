window.__ModuleLoader__.load({
  id: '@local/virtual-life-history',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const TAB_ID = '@local/virtual-life-history';
    const KIND = 'virtual-life-history';
    const API = 'http://127.0.0.1:8038';
    const COMPANION = 'proact-demo';
    const style = `
      .vlh-root { display:flex; flex-direction:column; height:100%; min-height:0; color:var(--dsw-alias-foreground); background:var(--dsw-alias-background); font:13px/1.5 var(--dsw-alias-font-family, sans-serif); }
      .vlh-toolbar { display:flex; gap:8px; align-items:center; padding:10px; border-bottom:1px solid var(--dsw-alias-border); }
      .vlh-toolbar select,.vlh-toolbar input,.vlh-toolbar button,.vlh-more { color:inherit; background:var(--dsw-alias-background); border:1px solid var(--dsw-alias-border); border-radius:4px; padding:5px 8px; }
      .vlh-toolbar input { min-width:0; width:150px; }
      .vlh-list { overflow:auto; padding:10px; display:flex; flex-direction:column; gap:8px; }
      .vlh-item { border:1px solid var(--dsw-alias-border); border-radius:6px; padding:8px 10px; white-space:pre-wrap; overflow-wrap:anywhere; }
      .vlh-meta { display:flex; justify-content:space-between; gap:8px; opacity:.68; font-size:11px; margin-bottom:4px; }
      .vlh-status { padding:16px 10px; opacity:.75; }
      .vlh-error { color:var(--dsw-alias-error, #b42318); }
      .vlh-more { margin:4px 10px 10px; cursor:pointer; }
    `;
    function HistoryPage({ useTabInfo, t }) {
      const [rows, setRows] = React.useState([]);
      const [hasMore, setHasMore] = React.useState(false);
      const [error, setError] = React.useState('');
      const [loading, setLoading] = React.useState(false);
      const [role, setRole] = React.useState('all');
      const [token, setToken] = React.useState(() => sessionStorage.getItem('vlh-token') || '');
      const [tokenDraft, setTokenDraft] = React.useState(token);
      const requestVersion = React.useRef(0);
      const currentBefore = React.useRef(null);
      const loadingRef = React.useRef(false);
      const controllerRef = React.useRef(null);
      const tabInfo = useTabInfo();
      const translateText = t;
      const load = React.useCallback(async (older) => {
        if (older && loadingRef.current) return;
        if (!older) controllerRef.current?.abort();
        loadingRef.current = true;
        const version = ++requestVersion.current;
        const controller = new AbortController();
        controllerRef.current = controller;
        setLoading(true); setError('');
        try {
          const url = new URL(`${API}/v1/companions/${encodeURIComponent(COMPANION)}/dialogue/history`);
          url.searchParams.set('limit', '30');
          if (older && currentBefore.current) url.searchParams.set('before_sequence', String(currentBefore.current));
          const headers = { Accept: 'application/json' };
          if (token) headers.Authorization = `Bearer ${token}`;
          const response = await fetch(url, { headers, credentials: 'omit', signal: controller.signal });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const data = await response.json();
          if (version !== requestVersion.current) return;
          if (!Array.isArray(data.items)) throw new Error('Unexpected history response');
          setRows(current => older ? [...current, ...data.items] : data.items);
          currentBefore.current = data.next_before_sequence ?? null;
          setHasMore(Boolean(data.has_more));
        } catch (err) {
          if (version === requestVersion.current && err?.name !== 'AbortError') setError(String(err?.message || err));
        } finally {
          if (version === requestVersion.current) { loadingRef.current = false; setLoading(false); controllerRef.current = null; }
        }
      }, [token]);
      React.useEffect(() => {
        load(false);
        return () => { requestVersion.current += 1; controllerRef.current?.abort(); };
      }, [load, tabInfo.tab.signal]);
      const visible = rows.flatMap(item => [
        { key: `${item.event_id}:user`, role: 'user', message: item.user_message || '', occurredAt: item.occurred_at, sequence: item.sequence },
        { key: `${item.event_id}:assistant`, role: 'assistant', message: item.assistant_message || '', occurredAt: item.occurred_at, sequence: item.sequence },
      ]).filter(item => item.message && (role === 'all' || item.role === role));
      return h('div', { className: 'vlh-root' },
        h('style', null, style),
        h('div', { className: 'vlh-toolbar' },
          h('strong', null, translateText('title')),
          h('input', { type: 'password', value: tokenDraft, placeholder: translateText('token'), 'aria-label': translateText('token'), onChange: e => setTokenDraft(e.target.value), onKeyDown: e => { if (e.key === 'Enter') { sessionStorage.setItem('vlh-token', tokenDraft); setToken(tokenDraft); currentBefore.current = null; setRows([]); } }, autoComplete: 'off' }),
          h('select', { value: role, onChange: e => setRole(e.target.value), 'aria-label': translateText('filter') },
            h('option', { value: 'all' }, translateText('all')),
            h('option', { value: 'user' }, translateText('user')),
            h('option', { value: 'assistant' }, translateText('assistant'))),
          h('button', { onClick: () => load(false), disabled: loading, title: translateText('refresh') }, loading ? '…' : '↻')),
        error ? h('div', { className: 'vlh-status vlh-error', role: 'alert' }, `${translateText('error')}: ${error}`) : null,
        h('div', { className: 'vlh-list', 'aria-live': 'polite' }, visible.length ? visible.map((item, index) => h('article', { className: 'vlh-item', key: `${item.sequence ?? item.event_id ?? index}:${item.role}` },
          h('div', { className: 'vlh-meta' }, h('span', null, item.role === 'assistant' ? translateText('assistant') : translateText('user'),
          ), h('time', null, item.occurredAt || '')),
          h('div', null, item.message)))
          : h('div', { className: 'vlh-status' }, loading ? translateText('loading') : translateText('empty'))),
        hasMore ? h('button', { className: 'vlh-more', onClick: () => load(true), disabled: loading }, translateText('older')) : null);
    }
    return {
      inject: ['slots', 'sidebarRightTabs', 'locale'],
      apply(ctx) {
        const t = ctx.locale.bind('virtualLifeHistory');
        ctx.effect(() => {
          const disposeLocale = ctx.locale.register('virtualLifeHistory', {
            en: {
              title: 'Virtual Life History', description: 'Read-only conversation history from a Virtual Life companion.', token: 'Runtime API token',
              filter: 'Filter messages', all: 'All', user: 'You', assistant: 'Companion', system: 'Other',
              refresh: 'Refresh history', error: 'Could not load history', loading: 'Loading history…', empty: 'No messages found.', older: 'Load older messages',
            },
            zh: {
              title: 'Virtual Life 会话历史', description: '只读浏览 Virtual Life 伙伴的对话记录。', token: '运行时 API 令牌',
              filter: '筛选消息', all: '全部', user: '你', assistant: '伙伴', system: '其他',
              refresh: '刷新历史', error: '加载历史失败', loading: '正在加载历史…', empty: '暂无会话记录。', older: '加载更早记录',
            },
          });
          const disposeTab = ctx.sidebarRightTabs.register({
            id: TAB_ID,
            kind: KIND,
            title: () => t('title'),
            guide: [{ id: 'history', order: 80, title: () => t('title'), description: () => t('description') }],
          });
          return () => { disposeTab(); disposeLocale(); };
        });
        ctx.effect(() => ctx.slots.inject('sidebar.right.pane.tab', () => ctx.slots.register({
          name: 'sidebar.right.pane.tab', key: TAB_ID,
        }, (props) => h(HistoryPage, { ...props, t }))), 'virtual-life-history: tab body');
      },
    };
  },
});
