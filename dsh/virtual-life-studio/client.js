window.__ModuleLoader__.load({
  id: '@virtuallife/dsh-virtual-life-studio',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const useState = React.useState;
    const useEffect = React.useEffect;
    const useMemo = React.useMemo;
    const useRef = React.useRef;
    const useCallback = React.useCallback;

    const NS = 'virtual-life-studio';
    const ROUTE = '/virtual-life-studio';
    const TABS = ['overview', 'logs', 'memories', 'graph', 'inner'];
    const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
    const KINDS = ['episode', 'fact', 'preference', 'relationship'];

    /* ------------------------------------------------------------------ i18n */

    const ZH = {
      'panel.title': 'Virtual Life 工作台',
      'panel.subtitle': '日志 · 记忆 · 知识图谱 · 内心活动',
      'tab.overview': '总览',
      'tab.logs': '日志',
      'tab.memories': '记忆',
      'tab.graph': '知识图谱',
      'tab.inner': '内心活动',
      'common.refresh': '刷新',
      'common.auto': '自动',
      'common.search': '搜索',
      'common.loading': '加载中…',
      'common.empty': '暂无数据',
      'common.error': '读取失败',
      'common.companion': '伙伴',
      'common.all': '全部',
      'common.total': '共',
      'common.prev': '上一页',
      'common.next': '下一页',
      'common.expand': '展开',
      'common.collapse': '收起',
      'overview.memories': '记忆',
      'overview.entities': '知识实体',
      'overview.relations': '关系',
      'overview.activities': '内心活动',
      'overview.state': '当前状态',
      'overview.inner': '此刻内心',
      'overview.needs': '内在需求',
      'overview.recent': '最近的内心活动',
      'state.mood': '心情',
      'state.energy': '精力',
      'state.social': '社交电量',
      'state.valence': '效价',
      'state.arousal': '唤醒',
      'state.control': '掌控',
      'inner.kind': '类型',
      'inner.trigger': '触发',
      'inner.salience': '显著度',
      'inner.urge': '冲动',
      'inner.thinking': '思考通道',
      'inner.showThinking': '展开思考通道',
      'inner.hideThinking': '收起思考通道',
      'inner.current': '当前活动',
      'inner.empty': '还没有内心活动记录',
      'inner.timeline': '活动时间线',
      'inner.enabled': '内心活动开关',
      'inner.on': '已启用',
      'inner.off': '已停用',
      'inner.model': '模型参与',
      'inner.rules': '规则生成',
      'inner.idle': '空闲',
      'inner.dominant': '主导类型',
      'needs.social': '社交',
      'needs.curiosity': '好奇',
      'needs.concern': '关切',
      'needs.reflection': '反思',
      'needs.rest': '休息',
      'logs.levels': '级别',
      'logs.follow': '跟随',
      'logs.empty': '还没有日志。运行时产生请求后，这里会实时滚动。',
      'logs.sequence': '序号',
      'logs.logger': '记录器',
      'logs.request': '请求',
      'memories.kind.episode': '情节',
      'memories.kind.fact': '事实',
      'memories.kind.preference': '偏好',
      'memories.kind.relationship': '关系',
      'memories.importance': '重要度',
      'memories.confidence': '置信度',
      'memories.retention': '保留策略',
      'memories.layer': '层',
      'memories.updated': '更新',
      'memories.empty': '这个伙伴还没有记忆记录',
      'memories.stats': '记忆统计',
      'memories.active': '活跃',
      'memories.pinned': '置顶',
      'memories.byKind': '分类',
      'memories.tokens': '内容体积',
      'memories.budget': '今日预算',
      'memories.sources': '来源事件',
      'graph.hops': '跳数',
      'graph.query': '检索实体',
      'graph.reset': '重置视图',
      'graph.empty': '这个伙伴还没有知识图谱。知识图谱由自动记忆从情节记忆里蒸馏出来。',
      'graph.entities': '实体',
      'graph.relations': '关系',
      'graph.aliases': '别名',
      'graph.evidence': '证据记忆',
      'graph.predicate': '谓词',
      'graph.confidence': '置信度',
      'graph.degree': '关联',
      'graph.revision': '版本',
      'graph.select': '点击节点查看详情',
      'time.just': '刚刚',
      'time.minute': '分钟前',
      'time.hour': '小时前',
      'time.day': '天前',
    };

    const EN = {
      'panel.title': 'Virtual Life Studio',
      'panel.subtitle': 'Logs · Memory · Knowledge graph · Inner life',
      'tab.overview': 'Overview',
      'tab.logs': 'Logs',
      'tab.memories': 'Memory',
      'tab.graph': 'Knowledge',
      'tab.inner': 'Inner life',
      'common.refresh': 'Refresh',
      'common.auto': 'Auto',
      'common.search': 'Search',
      'common.loading': 'Loading…',
      'common.empty': 'Nothing here yet',
      'common.error': 'Request failed',
      'common.companion': 'Companion',
      'common.all': 'All',
      'common.total': 'Total',
      'common.prev': 'Previous',
      'common.next': 'Next',
      'common.expand': 'Expand',
      'common.collapse': 'Collapse',
      'overview.memories': 'Memories',
      'overview.entities': 'Entities',
      'overview.relations': 'Relations',
      'overview.activities': 'Inner activities',
      'overview.state': 'Current state',
      'overview.inner': 'Inner weather',
      'overview.needs': 'Needs',
      'overview.recent': 'Recent inner activity',
      'state.mood': 'Mood',
      'state.energy': 'Energy',
      'state.social': 'Social battery',
      'state.valence': 'Valence',
      'state.arousal': 'Arousal',
      'state.control': 'Control',
      'inner.kind': 'Kind',
      'inner.trigger': 'Trigger',
      'inner.salience': 'Salience',
      'inner.urge': 'Urge',
      'inner.thinking': 'Thinking channel',
      'inner.showThinking': 'Show thinking channel',
      'inner.hideThinking': 'Hide thinking channel',
      'inner.current': 'Current activity',
      'inner.empty': 'No inner activity recorded yet',
      'inner.timeline': 'Activity timeline',
      'inner.enabled': 'Inner life',
      'inner.on': 'Enabled',
      'inner.off': 'Disabled',
      'inner.model': 'Model assisted',
      'inner.rules': 'Rules generated',
      'inner.idle': 'Idle',
      'inner.dominant': 'Dominant kind',
      'needs.social': 'Social',
      'needs.curiosity': 'Curiosity',
      'needs.concern': 'Concern',
      'needs.reflection': 'Reflection',
      'needs.rest': 'Rest',
      'logs.levels': 'Levels',
      'logs.follow': 'Follow',
      'logs.empty': 'No log lines yet. Once the runtime serves a request, they stream in here.',
      'logs.sequence': 'Sequence',
      'logs.logger': 'Logger',
      'logs.request': 'Request',
      'memories.kind.episode': 'Episode',
      'memories.kind.fact': 'Fact',
      'memories.kind.preference': 'Preference',
      'memories.kind.relationship': 'Relationship',
      'memories.importance': 'Importance',
      'memories.confidence': 'Confidence',
      'memories.retention': 'Retention',
      'memories.layer': 'Layer',
      'memories.updated': 'Updated',
      'memories.empty': 'This companion has no memory records',
      'memories.stats': 'Memory stats',
      'memories.active': 'Active',
      'memories.pinned': 'Pinned',
      'memories.byKind': 'By kind',
      'memories.tokens': 'Content size',
      'memories.budget': 'Today budget',
      'memories.sources': 'Source events',
      'graph.hops': 'Hops',
      'graph.query': 'Find entity',
      'graph.reset': 'Reset view',
      'graph.empty': 'No knowledge graph yet. It is distilled from episode memories by automatic memory.',
      'graph.entities': 'Entities',
      'graph.relations': 'Relations',
      'graph.aliases': 'Aliases',
      'graph.evidence': 'Evidence',
      'graph.predicate': 'Predicate',
      'graph.confidence': 'Confidence',
      'graph.degree': 'Links',
      'graph.revision': 'Revision',
      'graph.select': 'Select a node to inspect it',
      'time.just': 'just now',
      'time.minute': 'm ago',
      'time.hour': 'h ago',
      'time.day': 'd ago',
    };

    let translate = (key) => key;
    const localeListeners = new Set();

    /* --------------------------------------------------------------- helpers */

    function num(value, digits) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
      const n = Number(value);
      if (!Number.isFinite(n)) return '—';
      return digits === undefined ? String(Math.round(n)) : n.toFixed(digits);
    }

    function pct(value) {
      if (value === null || value === undefined) return 0;
      return Math.max(0, Math.min(100, Number(value) * 100));
    }

    function fmtTime(iso, t) {
      if (!iso) return '—';
      const then = new Date(iso);
      if (Number.isNaN(then.getTime())) return '—';
      const delta = (Date.now() - then.getTime()) / 1000;
      if (delta >= 0 && delta < 60) return t('time.just');
      if (delta >= 0 && delta < 3600) return `${Math.floor(delta / 60)}${t('time.minute')}`;
      if (delta >= 0 && delta < 86400) return `${Math.floor(delta / 3600)}${t('time.hour')}`;
      if (delta >= 0 && delta < 86400 * 7) return `${Math.floor(delta / 86400)}${t('time.day')}`;
      const pad = (n) => String(n).padStart(2, '0');
      return `${then.getMonth() + 1}/${then.getDate()} ${pad(then.getHours())}:${pad(then.getMinutes())}`;
    }

    function clock(iso) {
      if (!iso) return '—';
      const then = new Date(iso);
      if (Number.isNaN(then.getTime())) return '—';
      const pad = (n, w) => String(n).padStart(w || 2, '0');
      return `${pad(then.getHours())}:${pad(then.getMinutes())}:${pad(then.getSeconds())}.${pad(then.getMilliseconds(), 3)}`;
    }

    async function getJson(path) {
      let res;
      try {
        res = await fetch(`${ROUTE}/api${path}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
      } catch (error) {
        throw new Error(`${translate('common.error')}: ${error && error.message ? error.message : error}`);
      }
      if (!res.ok) {
        let detail = '';
        try {
          const body = await res.json();
          detail = body && body.detail ? JSON.stringify(body.detail) : '';
        } catch {
          detail = '';
        }
        throw new Error(`HTTP ${res.status}${detail ? ` · ${detail.slice(0, 200)}` : ''}`);
      }
      return res.json();
    }

    /* ------------------------------------------------------------ primitives */

    function useLocale() {
      const [, bump] = useState(0);
      useEffect(() => {
        const listener = () => bump((value) => value + 1);
        localeListeners.add(listener);
        return () => localeListeners.delete(listener);
      }, []);
      return translate;
    }

    function Chip(props) {
      return h('span', { className: `vls-chip${props.tone ? ` vls-chip--${props.tone}` : ''}${props.mono ? ' vls-mono' : ''}` }, props.children);
    }

    function Bar(props) {
      const value = pct(props.value);
      return h(
        'div',
        { className: 'vls-bar-row' },
        h('span', { className: 'vls-bar-label' }, props.label),
        h('div', { className: 'vls-bar' }, h('div', { className: `vls-bar-fill${props.tone ? ` vls-bar-fill--${props.tone}` : ''}`, style: { width: `${value}%` } })),
        h('span', { className: 'vls-bar-value vls-mono' }, props.display || num(pct(props.value), 0)),
      );
    }

    function Card(props) {
      const children = [];
      if (props.title) {
        children.push(
          h(
            'div',
            { className: 'vls-card-head' },
            h('span', { className: 'vls-card-title' }, props.title),
            props.sub ? h('span', { className: 'vls-card-sub' }, props.sub) : null,
            h('span', { className: 'vls-spacer' }),
            props.tools || null,
          ),
        );
      }
      children.push(h('div', { className: `vls-card-body${props.flush ? ' vls-card-body--flush' : ''}` }, props.children));
      return h('section', { className: `vls-card${props.className ? ` ${props.className}` : ''}` }, children);
    }

    function Metric(props) {
      return h(
        'div',
        { className: 'vls-metric' },
        h('div', { className: 'vls-metric-value vls-mono' }, props.value),
        h('div', { className: 'vls-metric-label' }, props.label),
        props.sub ? h('div', { className: 'vls-metric-sub' }, props.sub) : null,
      );
    }

    function Empty(props) {
      return h(
        'div',
        { className: 'vls-empty' },
        h('div', { className: 'vls-empty-title' }, props.title),
        props.sub ? h('div', { className: 'vls-empty-sub' }, props.sub) : null,
      );
    }

    function Loader(props) {
      return h('div', { className: 'vls-loader' }, props.label || translate('common.loading'));
    }

    function ErrorBox(props) {
      return h(
        'div',
        { className: 'vls-error' },
        h('span', null, props.error && props.error.message ? props.error.message : String(props.error)),
        props.onRetry ? h('button', { type: 'button', className: 'vls-btn vls-btn--sm', onClick: props.onRetry }, translate('common.refresh')) : null,
      );
    }

    function Json(props) {
      let text;
      try {
        text = JSON.stringify(props.value, null, 2);
      } catch {
        text = String(props.value);
      }
      if (text && text.length > 6000) text = `${text.slice(0, 6000)}\n…`;
      return h('pre', { className: 'vls-json vls-mono' }, text);
    }

    /* ------------------------------------------------------------------ data */

    function useResource(path, deps, enabled) {
      const [state, setState] = useState({ data: null, error: null, loading: true });
      const [nonce, setNonce] = useState(0);
      useEffect(() => {
        if (enabled === false || !path) return undefined;
        let alive = true;
        setState((prev) => ({ data: prev.data, error: null, loading: true }));
        getJson(path)
          .then((data) => {
            if (alive) setState({ data, error: null, loading: false });
          })
          .catch((error) => {
            if (alive) setState({ data: null, error, loading: false });
          });
        return () => {
          alive = false;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [path, nonce, enabled].concat(deps || []));
      return { data: state.data, error: state.error, loading: state.loading, reload: () => setNonce((value) => value + 1) };
    }

    /* ---------------------------------------------------------------- views */

    function OverviewView(props) {
      const t = props.t;
      const id = props.companionId;
      const refresh = [props.refreshToken];
      const inner = useResource(id ? `/companions/${encodeURIComponent(id)}/inner-life` : null, refresh);
      const emotion = useResource(id ? `/companions/${encodeURIComponent(id)}/emotion` : null, refresh);
      const stats = useResource(id ? `/companions/${encodeURIComponent(id)}/memories/stats` : null, refresh);
      const kg = useResource(id ? `/companions/${encodeURIComponent(id)}/memories/knowledge-graph?hops=2&limit=200` : null, refresh);
      const activities = useResource(id ? `/companions/${encodeURIComponent(id)}/inner-life/activities?limit=8` : null, refresh);

      if (inner.error) return h(ErrorBox, { error: inner.error, onRetry: inner.reload });
      if (inner.loading && !inner.data) return h(Loader);

      const snapshot = inner.data || {};
      const current = snapshot.current || null;
      const needs = snapshot.needs || {};
      const state = (emotion.data && emotion.data.effective_state) || {};
      const memoryStats = stats.data || {};
      const graph = kg.data || {};
      const recent = ((activities.data && activities.data.items) || []).slice().reverse();

      return h(
        'div',
        { className: 'vls-stack' },
        h(
          'div',
          { className: 'vls-grid' },
          h(Metric, { label: t('overview.memories'), value: num(memoryStats.active_records), sub: `${num(memoryStats.total_records)} ${t('common.total')}` }),
          h(Metric, { label: t('overview.entities'), value: num((graph.entities || []).length) }),
          h(Metric, { label: t('overview.relations'), value: num((graph.relations || []).length) }),
          h(Metric, { label: t('overview.activities'), value: num((activities.data && activities.data.items || []).length) }),
        ),
        h(
          'div',
          { className: 'vls-cols' },
          h(
            Card,
            { title: t('overview.state') },
            h(
              'div',
              { className: 'vls-chip-row' },
              h(Chip, { tone: 'accent' }, `${t('state.mood')} ${state.mood || snapshot.mood || '—'}`),
              h(Chip, null, `${t('state.energy')} ${num(pct(state.energy ?? snapshot.energy), 0)}%`),
              h(Chip, null, `${t('state.social')} ${num(pct(state.social_battery ?? snapshot.social_battery), 0)}%`),
            ),
            h('div', { className: 'vls-gap' }),
            h(Bar, { label: t('state.valence'), value: snapshot.valence, display: num(snapshot.valence, 2) }),
            h(Bar, { label: t('state.arousal'), value: (emotion.data && emotion.data.state && emotion.data.state.arousal) || 0, display: num((emotion.data && emotion.data.state && emotion.data.state.arousal), 2) }),
            h(Bar, { label: t('state.control'), value: (emotion.data && emotion.data.state && emotion.data.state.control) || 0, display: num((emotion.data && emotion.data.state && emotion.data.state.control), 2) }),
          ),
          h(
            Card,
            { title: t('overview.needs') },
            Object.keys(needs).length
              ? Object.entries(needs).map(([key, value]) => h(Bar, { key, label: t(`needs.${key}`) === `needs.${key}` ? key : t(`needs.${key}`), value, display: num(pct(value), 0) }))
              : h(Empty, { title: t('common.empty') }),
          ),
        ),
        h(
          Card,
          { title: t('overview.inner'), sub: current ? current.kind : null },
          current
            ? h(
                'div',
                null,
                h('blockquote', { className: 'vls-quote' }, current.content),
                h(
                  'div',
                  { className: 'vls-chip-row' },
                  h(Chip, { tone: 'accent', mono: true }, `${t('inner.kind')} ${current.kind}`),
                  h(Chip, { mono: true }, `${t('inner.trigger')} ${current.trigger || '—'}`),
                  h(Chip, { mono: true }, `${t('inner.salience')} ${num(current.salience, 2)}`),
                  h(Chip, { mono: true }, `${t('inner.urge')} ${num(current.urge, 2)}`),
                  h(Chip, { mono: true }, current.generator || '—'),
                ),
              )
            : h(Empty, { title: t('inner.empty') }),
        ),
        h(
          Card,
          { title: t('overview.recent'), flush: true },
          recent.length
            ? h(
                'ul',
                { className: 'vls-timeline' },
                recent.map((item) =>
                  h(
                    'li',
                    { key: item.activity_id, className: 'vls-tl' },
                    h(
                      'div',
                      { className: 'vls-tl-head' },
                      h(Chip, { tone: 'accent', mono: true }, item.kind),
                      h('span', { className: 'vls-tl-time vls-mono' }, fmtTime(item.created_at, t)),
                      h('span', { className: 'vls-spacer' }),
                      h('span', { className: 'vls-mono vls-dim' }, `#${item.sequence}`),
                    ),
                    h('div', { className: 'vls-tl-text' }, item.content),
                    h(Bar, { label: t('inner.salience'), value: item.salience, display: num(item.salience, 2) }),
                  ),
                ),
              )
            : h(Empty, { title: t('inner.empty') }),
        ),
      );
    }

    function LogsView(props) {
      const t = props.t;
      const [levels, setLevels] = useState(() => new Set(LEVELS));
      const [query, setQuery] = useState('');
      const [follow, setFollow] = useState(true);
      const [open, setOpen] = useState(null);
      const [items, setItems] = useState([]);
      const [error, setError] = useState(null);
      const cursor = useRef(0);
      const listRef = useRef(null);

      const matches = useCallback(
        (item) => {
          if (!levels.has(String(item.level || '').toUpperCase())) return false;
          if (!query.trim()) return true;
          const needle = query.trim().toLowerCase();
          return [item.message, item.logger, item.request_id].filter(Boolean).some((value) => String(value).toLowerCase().includes(needle));
        },
        [levels, query],
      );

      useEffect(() => {
        let alive = true;
        let timer = null;
        const pull = async () => {
          try {
            const page = await getJson(`/flow/logs?after_sequence=${cursor.current}&limit=${props.logLimit}`);
            if (!alive) return;
            if (page.next_sequence != null) cursor.current = page.next_sequence;
            if (page.items && page.items.length) {
              setItems((prev) => prev.concat(page.items).slice(-3000));
            }
            setError(null);
          } catch (caught) {
            if (alive) setError(caught);
          }
          if (alive && props.auto) timer = setTimeout(pull, 1200);
        };
        pull();
        return () => {
          alive = false;
          if (timer) clearTimeout(timer);
        };
      }, [props.auto, props.logLimit, props.refreshToken]);

      useEffect(() => {
        const node = listRef.current;
        if (node && follow) node.scrollTop = node.scrollHeight;
      }, [items, follow]);

      const visible = useMemo(() => items.filter(matches), [items, matches]);

      return h(
        'div',
        { className: 'vls-stack vls-fill' },
        h(
          'div',
          { className: 'vls-toolbar' },
          h('span', { className: 'vls-toolbar-label' }, t('logs.levels')),
          LEVELS.map((level) =>
            h(
              'button',
              {
                key: level,
                type: 'button',
                className: `vls-chip vls-chip--btn${levels.has(level) ? ` vls-chip--${level.toLowerCase()}` : ''}`,
                onClick: () => {
                  const next = new Set(levels);
                  if (next.has(level)) next.delete(level);
                  else next.add(level);
                  setLevels(next);
                },
              },
              level,
            ),
          ),
          h('input', { className: 'vls-input', type: 'search', placeholder: t('common.search'), value: query, onChange: (event) => setQuery(event.target.value) }),
          h('span', { className: 'vls-spacer' }),
          h(
            'label',
            { className: 'vls-switch' },
            h('input', { type: 'checkbox', checked: follow, onChange: (event) => setFollow(event.target.checked) }),
            h('span', null, t('logs.follow')),
          ),
          h('span', { className: 'vls-dim vls-mono' }, `${visible.length} / ${items.length}`),
        ),
        error ? h(ErrorBox, { error }) : null,
        h(
          'div',
          { className: 'vls-loglist', ref: listRef },
          visible.length
            ? visible.map((item) =>
                h(
                  'div',
                  { key: `${item.sequence}-${item.logger}`, className: `vls-logrow${open === item.sequence ? ' vls-logrow--open' : ''}` },
                  h('span', { className: 'vls-logtime vls-mono' }, clock(item.timestamp)),
                  h('span', { className: `vls-log-level vls-log-level--${String(item.level || 'info').toLowerCase()}` }, String(item.level || 'INFO')),
                  h(
                    'span',
                    { className: 'vls-log-msg', onClick: () => setOpen(open === item.sequence ? null : item.sequence) },
                    h('span', { className: 'vls-log-logger vls-mono' }, String(item.logger || '').split('.').pop()),
                    h('span', { className: 'vls-log-text' }, item.message),
                  ),
                  open === item.sequence
                    ? h(
                        'div',
                        { className: 'vls-log-exp' },
                        h('span', null, `${t('logs.sequence')} ${item.sequence}`),
                        h('span', null, `${t('logs.logger')} ${item.logger || '—'}`),
                        h('span', null, `${t('logs.request')} ${item.request_id || '—'}`),
                        item.line ? h(Json, { value: item.line }) : null,
                      )
                    : null,
                ),
              )
            : h(Empty, { title: t('logs.empty') }),
        ),
      );
    }

    function MemoriesView(props) {
      const t = props.t;
      const id = props.companionId;
      const [kind, setKind] = useState('');
      const [page, setPage] = useState(0);
      const [query, setQuery] = useState('');
      const [expanded, setExpanded] = useState(null);
      const limit = 20;
      const path = id ? `/companions/${encodeURIComponent(id)}/memories?limit=${limit}&offset=${page * limit}${kind ? `&kind=${kind}` : ''}` : null;
      const list = useResource(path, [kind, page, props.refreshToken]);
      const stats = useResource(id ? `/companions/${encodeURIComponent(id)}/memories/stats` : null, [props.refreshToken]);

      const items = ((list.data && list.data.items) || []).filter((item) =>
        query.trim() ? String(item.content || '').toLowerCase().includes(query.trim().toLowerCase()) : true,
      );
      const total = (list.data && list.data.total) || 0;
      const memoryStats = stats.data || {};

      return h(
        'div',
        { className: 'vls-split' },
        h(
          'div',
          { className: 'vls-stack' },
          h(
            'div',
            { className: 'vls-toolbar' },
            h(
              'button',
              { type: 'button', className: `vls-chip vls-chip--btn${kind === '' ? ' vls-chip--accent' : ''}`, onClick: () => { setKind(''); setPage(0); } },
              t('common.all'),
            ),
            KINDS.map((value) =>
              h(
                'button',
                { key: value, type: 'button', className: `vls-chip vls-chip--btn${kind === value ? ' vls-chip--accent' : ''}`, onClick: () => { setKind(value); setPage(0); } },
                t(`memories.kind.${value}`),
              ),
            ),
            h('input', { className: 'vls-input', type: 'search', placeholder: t('common.search'), value: query, onChange: (event) => setQuery(event.target.value) }),
          ),
          list.error ? h(ErrorBox, { error: list.error, onRetry: list.reload }) : null,
          list.loading && !list.data
            ? h(Loader)
            : items.length
              ? h(
                  'div',
                  { className: 'vls-memlist' },
                  items.map((item) => {
                    const isOpen = expanded === item.memory_id;
                    return h(
                      'article',
                      { key: item.memory_id, className: `vls-mem${isOpen ? ' vls-mem--open' : ''}` },
                      h(
                        'div',
                        { className: 'vls-mem-head', onClick: () => setExpanded(isOpen ? null : item.memory_id) },
                        h(Chip, { tone: 'accent', mono: true }, t(`memories.kind.${item.kind}`) === `memories.kind.${item.kind}` ? item.kind : t(`memories.kind.${item.kind}`)),
                        item.active === false ? h(Chip, { tone: 'warn' }, 'inactive') : null,
                        item.pinned ? h(Chip, { tone: 'accent' }, t('memories.pinned')) : null,
                        item.confirmed ? h(Chip, null, 'confirmed') : null,
                        h('span', { className: 'vls-spacer' }),
                        h('span', { className: 'vls-dim vls-mono' }, `v${item.revision} · ${fmtTime(item.updated_at, t)}`),
                      ),
                      h('p', { className: 'vls-mem-content' }, isOpen ? item.content : `${String(item.content || '').slice(0, 220)}${String(item.content || '').length > 220 ? '…' : ''}`),
                      h(
                        'div',
                        { className: 'vls-mem-meta' },
                        h(Bar, { label: t('memories.importance'), value: item.importance, display: num(item.importance, 2) }),
                        h(Bar, { label: t('memories.confidence'), value: item.confidence, display: num(item.confidence, 2) }),
                      ),
                      isOpen
                        ? h(
                            'div',
                            { className: 'vls-mem-detail' },
                            h(Json, {
                              value: {
                                memory_id: item.memory_id,
                                kind: item.kind,
                                layer: item.layer,
                                semantic_key: item.semantic_key,
                                tags: item.tags,
                                retention_policy: item.retention_policy,
                                effective_retention_policy: item.effective_retention_policy,
                                occurred_at: item.occurred_at,
                                valid_from: item.valid_from,
                                valid_to: item.valid_to,
                                source_event_ids: item.source_event_ids,
                                supersedes_id: item.supersedes_id,
                              },
                            }),
                          )
                        : null,
                    );
                  }),
                )
              : h(Empty, { title: t('memories.empty') }),
          h(
            'div',
            { className: 'vls-pager' },
            h('button', { type: 'button', className: 'vls-btn vls-btn--sm', disabled: page === 0, onClick: () => setPage(Math.max(0, page - 1)) }, t('common.prev')),
            h('span', { className: 'vls-dim vls-mono' }, `${page * limit + 1}–${Math.min(total, (page + 1) * limit)} / ${total}`),
            h('button', { type: 'button', className: 'vls-btn vls-btn--sm', disabled: (page + 1) * limit >= total, onClick: () => setPage(page + 1) }, t('common.next')),
          ),
        ),
        h(
          'aside',
          { className: 'vls-side' },
          h(
            Card,
            { title: t('memories.stats') },
            h(
              'dl',
              { className: 'vls-kv' },
              h('dt', null, t('memories.active')),
              h('dd', { className: 'vls-mono' }, num(memoryStats.active_records)),
              h('dt', null, t('common.total')),
              h('dd', { className: 'vls-mono' }, num(memoryStats.total_records)),
              h('dt', null, t('memories.pinned')),
              h('dd', { className: 'vls-mono' }, num(memoryStats.pinned_records)),
              h('dt', null, t('memories.tokens')),
              h('dd', { className: 'vls-mono' }, `${num(memoryStats.content_utf8_bytes)} B · ~${num(memoryStats.stored_content_estimated_tokens)} tok`),
              h('dt', null, t('memories.budget')),
              h('dd', { className: 'vls-mono' }, `${num(memoryStats.token_quota && memoryStats.token_quota.spent_tokens)} / ${num(memoryStats.token_quota && memoryStats.token_quota.daily_token_limit)}`),
              h('dt', null, t('memories.sources')),
              h('dd', { className: 'vls-mono' }, num(memoryStats.source_event_count)),
            ),
            h('div', { className: 'vls-gap' }),
            h(
              'div',
              { className: 'vls-chip-row' },
              ...Object.entries(memoryStats.by_kind || {}).map(([key, value]) =>
                h(Chip, { key, mono: true }, `${t(`memories.kind.${key}`) === `memories.kind.${key}` ? key : t(`memories.kind.${key}`)} ${num(value && value.active)}/${num(value && value.total)}`),
              ),
            ),
          ),
        ),
      );
    }

    function layoutGraph(entities, relations, width, height) {
      const nodes = entities.map((entity, index) => {
        const angle = (index / Math.max(1, entities.length)) * Math.PI * 2;
        const radius = Math.min(width, height) * 0.32;
        return {
          id: entity.name,
          entity,
          x: width / 2 + Math.cos(angle) * radius,
          y: height / 2 + Math.sin(angle) * radius,
          vx: 0,
          vy: 0,
        };
      });
      const byName = new Map(nodes.map((node) => [node.id, node]));
      const links = [];
      for (const relation of relations) {
        const source = byName.get(relation.subject);
        const target = byName.get(relation.object);
        if (source && target) links.push({ source, target, relation });
      }
      const degrees = new Map();
      for (const link of links) {
        degrees.set(link.source.id, (degrees.get(link.source.id) || 0) + 1);
        degrees.set(link.target.id, (degrees.get(link.target.id) || 0) + 1);
      }
      for (let step = 0; step < 260; step += 1) {
        for (let i = 0; i < nodes.length; i += 1) {
          for (let j = i + 1; j < nodes.length; j += 1) {
            const a = nodes[i];
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
        }
        for (const link of links) {
          const dx = link.target.x - link.source.x;
          const dy = link.target.y - link.source.y;
          const dist = Math.hypot(dx, dy) || 0.01;
          const force = (dist - 150) * 0.012;
          const ux = dx / dist;
          const uy = dy / dist;
          link.source.vx += ux * force;
          link.source.vy += uy * force;
          link.target.vx -= ux * force;
          link.target.vy -= uy * force;
        }
        for (const node of nodes) {
          node.vx += (width / 2 - node.x) * 0.0025;
          node.vy += (height / 2 - node.y) * 0.0025;
          node.vx *= 0.82;
          node.vy *= 0.82;
          node.x = Math.max(40, Math.min(width - 40, node.x + node.vx));
          node.y = Math.max(40, Math.min(height - 40, node.y + node.vy));
        }
      }
      return { nodes, links, degrees };
    }

    function GraphView(props) {
      const t = props.t;
      const id = props.companionId;
      const [hops, setHops] = useState(2);
      const [query, setQuery] = useState('');
      const [selected, setSelected] = useState(null);
      const path = id ? `/companions/${encodeURIComponent(id)}/memories/knowledge-graph?hops=${hops}&limit=200${query.trim() ? `&query=${encodeURIComponent(query.trim())}` : ''}` : null;
      const res = useResource(path, [hops, query, props.refreshToken]);

      const entities = (res.data && res.data.entities) || [];
      const relations = (res.data && res.data.relations) || [];
      const width = 900;
      const height = 520;
      const model = useMemo(() => layoutGraph(entities, relations, width, height), [res.data]);

      if (res.error) return h(ErrorBox, { error: res.error, onRetry: res.reload });
      if (res.loading && !res.data) return h(Loader);

      if (!entities.length) {
        return h(
          'div',
          { className: 'vls-stack' },
          h(Empty, { title: t('graph.empty') }),
        );
      }

      const selectedNode = selected ? model.nodes.find((node) => node.id === selected) : null;
      const touching = selected ? relations.filter((relation) => relation.subject === selected || relation.object === selected) : [];

      return h(
        'div',
        { className: 'vls-stack' },
        h(
          'div',
          { className: 'vls-toolbar' },
          h('span', { className: 'vls-toolbar-label' }, t('graph.hops')),
          [1, 2, 3].map((value) =>
            h('button', { key: value, type: 'button', className: `vls-chip vls-chip--btn${hops === value ? ' vls-chip--accent' : ''}`, onClick: () => setHops(value) }, String(value)),
          ),
          h('input', { className: 'vls-input', type: 'search', placeholder: t('graph.query'), value: query, onChange: (event) => setQuery(event.target.value) }),
          h('span', { className: 'vls-spacer' }),
          h('span', { className: 'vls-dim vls-mono' }, `${t('graph.entities')} ${entities.length} · ${t('graph.relations')} ${relations.length}`),
          h('button', { type: 'button', className: 'vls-btn vls-btn--sm', onClick: () => setSelected(null) }, t('graph.reset')),
        ),
        h(
          'div',
          { className: 'vls-split' },
          h(
            'div',
            { className: 'vls-graphwrap' },
            h(
              'svg',
              { viewBox: `0 0 ${width} ${height}`, className: 'vls-graph', role: 'img' },
              model.links.map((link) =>
                h(
                  'g',
                  { key: link.relation.relation_id },
                  h('line', {
                    x1: link.source.x,
                    y1: link.source.y,
                    x2: link.target.x,
                    y2: link.target.y,
                    className: `vls-edge${selected && (link.relation.subject === selected || link.relation.object === selected) ? ' vls-edge--active' : ''}`,
                  }),
                  h(
                    'text',
                    { x: (link.source.x + link.target.x) / 2, y: (link.source.y + link.target.y) / 2 - 4, className: 'vls-edge-label' },
                    link.relation.predicate,
                  ),
                ),
              ),
              model.nodes.map((node) => {
                const degree = model.degrees.get(node.id) || 0;
                const radius = 13 + Math.min(9, degree * 2.6);
                const active = selected === node.id;
                return h(
                  'g',
                  { key: node.id, className: `vls-node${active ? ' vls-node--active' : ''}`, onClick: () => setSelected(active ? null : node.id), style: { cursor: 'pointer' } },
                  h('circle', { cx: node.x, cy: node.y, r: radius, className: 'vls-node-dot' }),
                  h('text', { x: node.x, y: node.y + radius + 14, className: 'vls-node-label' }, node.entity.name),
                  h('text', { x: node.x, y: node.y + 4, className: 'vls-node-count' }, String(degree)),
                );
              }),
            ),
          ),
          h(
            'aside',
            { className: 'vls-side' },
            selectedNode
              ? h(
                  Card,
                  { title: selectedNode.entity.name },
                  h(
                    'div',
                    { className: 'vls-chip-row' },
                    h(Chip, { mono: true }, `${t('graph.degree')} ${model.degrees.get(selectedNode.id) || 0}`),
                    h(Chip, { mono: true }, `${t('graph.revision')} ${selectedNode.entity.revision}`),
                    h(Chip, { mono: true }, fmtTime(selectedNode.entity.updated_at, t)),
                  ),
                  (selectedNode.entity.aliases || []).length
                    ? h('div', { className: 'vls-chip-row vls-gap' }, ...selectedNode.entity.aliases.map((alias) => h(Chip, { key: alias, tone: 'accent' }, alias)))
                    : null,
                  h('div', { className: 'vls-gap' }),
                  h('div', { className: 'vls-section-title' }, t('graph.relations')),
                  h(
                    'ul',
                    { className: 'vls-rels' },
                    touching.map((relation) =>
                      h(
                        'li',
                        { key: relation.relation_id, className: 'vls-rel' },
                        h('div', null, h('span', { className: 'vls-mono' }, relation.subject), h('span', { className: 'vls-dim' }, ' → '), h('span', { className: 'vls-mono vls-accent' }, relation.predicate), h('span', { className: 'vls-dim' }, ' → '), h('span', { className: 'vls-mono' }, relation.object)),
                        h('div', { className: 'vls-chip-row' }, h(Chip, { mono: true }, `${t('graph.confidence')} ${num(relation.confidence, 2)}`), h(Chip, { mono: true }, `${t('graph.evidence')} ${(relation.memory_versions || []).length}`)),
                      ),
                    ),
                  ),
                  h('div', { className: 'vls-section-title' }, t('graph.evidence')),
                  h(Json, { value: { entity_id: selectedNode.entity.entity_id, source_event_ids: selectedNode.entity.source_event_ids } }),
                )
              : h(Empty, { title: t('graph.select') }),
          ),
        ),
      );
    }

    function Radar(props) {
      const entries = props.entries;
      const size = 260;
      const center = size / 2;
      const radius = size * 0.34;
      if (!entries.length) return null;
      const point = (index, value) => {
        const angle = (index / entries.length) * Math.PI * 2 - Math.PI / 2;
        const r = radius * Math.max(0.04, Math.min(1, Number(value) || 0));
        return [center + Math.cos(angle) * r, center + Math.sin(angle) * r];
      };
      const shape = entries.map((entry, index) => point(index, entry.value).join(',')).join(' ');
      return h(
        'svg',
        { viewBox: `0 0 ${size} ${size}`, className: 'vls-radar' },
        [0.25, 0.5, 0.75, 1].map((ring) =>
          h('polygon', {
            key: ring,
            className: 'vls-radar-grid',
            points: entries.map((_, index) => {
              const angle = (index / entries.length) * Math.PI * 2 - Math.PI / 2;
              return `${center + Math.cos(angle) * radius * ring},${center + Math.sin(angle) * radius * ring}`;
            }).join(' '),
          }),
        ),
        entries.map((entry, index) => {
          const [x, y] = point(index, 1);
          return h('line', { key: `axis-${entry.key}`, x1: center, y1: center, x2: x, y2: y, className: 'vls-radar-axis' });
        }),
        h('polygon', { points: shape, className: 'vls-radar-shape' }),
        entries.map((entry, index) => {
          const [x, y] = point(index, 1.18);
          return h('text', { key: `label-${entry.key}`, x, y, className: 'vls-radar-label', textAnchor: 'middle', dominantBaseline: 'middle' }, entry.label);
        }),
      );
    }

    function InnerView(props) {
      const t = props.t;
      const id = props.companionId;
      const inner = useResource(id ? `/companions/${encodeURIComponent(id)}/inner-life` : null, [props.refreshToken]);
      const activities = useResource(id ? `/companions/${encodeURIComponent(id)}/inner-life/activities?limit=25` : null, [props.refreshToken]);
      const [thinking, setThinking] = useState({});

      if (inner.error) return h(ErrorBox, { error: inner.error, onRetry: inner.reload });
      if (inner.loading && !inner.data) return h(Loader);

      const snapshot = inner.data || {};
      const current = snapshot.current || null;
      const needs = snapshot.needs || {};
      const entries = Object.keys(needs).map((key) => ({ key, label: t(`needs.${key}`) === `needs.${key}` ? key : t(`needs.${key}`), value: needs[key] }));
      const timeline = ((activities.data && activities.data.items) || []).slice().reverse();

      return h(
        'div',
        { className: 'vls-stack' },
        h(
          'div',
          { className: 'vls-cols' },
          h(
            Card,
            { title: t('inner.enabled') },
            h(
              'div',
              { className: 'vls-chip-row' },
              h(Chip, { tone: snapshot.enabled ? 'ok' : 'warn' }, snapshot.enabled ? t('inner.on') : t('inner.off')),
              h(Chip, { mono: true }, snapshot.model_assisted ? t('inner.model') : t('inner.rules')),
              h(Chip, { tone: 'accent', mono: true }, `${t('inner.dominant')} ${snapshot.dominant_kind || '—'}`),
            ),
            h('div', { className: 'vls-gap' }),
            h(Bar, { label: t('inner.salience'), value: snapshot.salience, display: num(snapshot.salience, 2) }),
            h(Bar, { label: t('inner.urge'), value: snapshot.urge, display: num(snapshot.urge, 2) }),
            h(Bar, { label: t('state.energy'), value: snapshot.energy, display: num(pct(snapshot.energy), 0) }),
            h(Bar, { label: t('state.social'), value: snapshot.social_battery, display: num(pct(snapshot.social_battery), 0) }),
            h(
              'dl',
              { className: 'vls-kv vls-gap' },
              h('dt', null, t('inner.idle')),
              h('dd', { className: 'vls-mono' }, snapshot.idle_seconds == null ? '—' : `${Math.round(snapshot.idle_seconds / 60)} min`),
              h('dt', null, t('memories.updated')),
              h('dd', { className: 'vls-mono' }, fmtTime(snapshot.computed_at, t)),
            ),
          ),
          h(
            Card,
            { title: t('overview.needs') },
            entries.length ? h('div', { className: 'vls-radar-wrap' }, h(Radar, { entries })) : h(Empty, { title: t('common.empty') }),
          ),
        ),
        h(
          Card,
          { title: t('inner.current'), sub: current ? current.kind : null },
          current
            ? h(
                'div',
                null,
                h('blockquote', { className: 'vls-quote' }, current.content),
                h(
                  'div',
                  { className: 'vls-chip-row' },
                  h(Chip, { tone: 'accent', mono: true }, `${t('inner.trigger')} ${current.trigger || '—'}`),
                  h(Chip, { mono: true }, `${t('inner.kind')} ${current.kind}`),
                  h(Chip, { mono: true }, `${t('inner.salience')} ${num(current.salience, 2)}`),
                  h(Chip, { mono: true }, `${t('inner.urge')} ${num(current.urge, 2)}`),
                  h(Chip, { mono: true }, current.generator || '—'),
                  current.thinking
                    ? h(
                        'button',
                        {
                          type: 'button',
                          className: 'vls-btn vls-btn--sm',
                          onClick: () => setThinking(Object.assign({}, thinking, { [current.activity_id]: !thinking[current.activity_id] })),
                        },
                        thinking[current.activity_id] ? t('inner.hideThinking') : t('inner.showThinking'),
                      )
                    : null,
                ),
                current.thinking && thinking[current.activity_id] ? h('pre', { className: 'vls-thinking vls-mono' }, current.thinking) : null,
              )
            : h(Empty, { title: t('inner.empty') }),
        ),
        h(
          Card,
          { title: t('inner.timeline'), flush: true },
          timeline.length
            ? h(
                'ul',
                { className: 'vls-timeline' },
                timeline.map((item) =>
                  h(
                    'li',
                    { key: item.activity_id, className: `vls-tl${current && current.activity_id === item.activity_id ? ' vls-tl--active' : ''}` },
                    h(
                      'div',
                      { className: 'vls-tl-head' },
                      h(Chip, { tone: 'accent', mono: true }, item.kind),
                      h('span', { className: 'vls-mono vls-dim' }, `#${item.sequence}`),
                      h('span', { className: 'vls-tl-time vls-mono' }, fmtTime(item.created_at, t)),
                      h('span', { className: 'vls-spacer' }),
                      h('span', { className: 'vls-mono vls-dim' }, `${t('inner.salience')} ${num(item.salience, 2)} · ${t('inner.urge')} ${num(item.urge, 2)}`),
                    ),
                    h('div', { className: 'vls-tl-text' }, item.content),
                    item.thinking
                      ? h(
                          'div',
                          null,
                          thinking[item.activity_id]
                            ? h(
                                'div',
                                null,
                                h('pre', { className: 'vls-thinking vls-mono' }, item.thinking),
                                h(
                                  'button',
                                  { type: 'button', className: 'vls-btn vls-btn--sm', onClick: () => setThinking(Object.assign({}, thinking, { [item.activity_id]: false })) },
                                  t('inner.hideThinking'),
                                ),
                              )
                            : h(
                                'button',
                                { type: 'button', className: 'vls-btn vls-btn--sm', onClick: () => setThinking(Object.assign({}, thinking, { [item.activity_id]: true })) },
                                t('inner.showThinking'),
                              ),
                        )
                      : null,
                  ),
                ),
              )
            : h(Empty, { title: t('inner.empty') }),
        ),
      );
    }

    /* ------------------------------------------------------------------ panel */

    function StudioPanel() {
      const t = useLocale();
      const [config, setConfig] = useState(null);
      const [companionId, setCompanionId] = useState('');
      const [companions, setCompanions] = useState([]);
      const [tab, setTab] = useState('overview');
      const [auto, setAuto] = useState(true);
      const [refreshToken, setRefreshToken] = useState(0);
      const [error, setError] = useState(null);

      useEffect(() => {
        let alive = true;
        getJson('/companions')
          .then((list) => {
            if (!alive) return;
            setCompanions(Array.isArray(list) ? list : []);
          })
          .catch((caught) => {
            if (alive) setError(caught);
          });
        fetch(`${ROUTE}/config`, { headers: { Accept: 'application/json' }, cache: 'no-store' })
          .then((res) => (res.ok ? res.json() : null))
          .then((value) => {
            if (!alive) return;
            setConfig(value);
            if (value && value.companionId) setCompanionId((current) => current || value.companionId);
          })
          .catch(() => {});
        return () => {
          alive = false;
        };
      }, []);

      useEffect(() => {
        if (companionId) return;
        if (companions.length) setCompanionId(companions[0].companion_id);
      }, [companions, companionId]);

      const pollMs = (config && config.refreshMs) || 5000;
      const logLimit = (config && config.logLimit) || 300;

      useEffect(() => {
        if (!auto) return undefined;
        const timer = setInterval(() => setRefreshToken((value) => value + 1), pollMs);
        return () => clearInterval(timer);
      }, [auto, pollMs]);

      const view = useMemo(() => {
        const shared = { t, companionId, auto, refreshToken, logLimit, refreshMs: pollMs };
        if (!companionId) return h(Empty, { title: t('common.empty') });
        if (tab === 'logs') return h(LogsView, Object.assign({ key: 'logs' }, shared));
        if (tab === 'memories') return h(MemoriesView, Object.assign({ key: 'memories' }, shared));
        if (tab === 'graph') return h(GraphView, Object.assign({ key: 'graph' }, shared));
        if (tab === 'inner') return h(InnerView, Object.assign({ key: 'inner' }, shared));
        return h(OverviewView, Object.assign({ key: 'overview' }, shared));
      }, [tab, companionId, t, auto, refreshToken, logLimit, pollMs]);

      return h(
        'div',
        { className: 'vls-root' },
        h('style', null, CSS),
        h(
          'header',
          { className: 'vls-head' },
          h(
            'div',
            { className: 'vls-brand' },
            h('span', { className: 'vls-brand-mark' }, '✳'),
            h('div', null, h('div', { className: 'vls-brand-title' }, t('panel.title')), h('div', { className: 'vls-brand-sub' }, t('panel.subtitle'))),
          ),
          h('span', { className: 'vls-spacer' }),
          h(
            'label',
            { className: 'vls-field' },
            h('span', null, t('common.companion')),
            h(
              'select',
              { className: 'vls-select', value: companionId, onChange: (event) => setCompanionId(event.target.value) },
              companions.length
                ? companions.map((item) => h('option', { key: item.companion_id, value: item.companion_id }, item.companion_id))
                : h('option', { value: companionId }, companionId || '—'),
            ),
          ),
          h(
            'label',
            { className: 'vls-switch' },
            h('input', { type: 'checkbox', checked: auto, onChange: (event) => setAuto(event.target.checked) }),
            h('span', null, t('common.auto')),
          ),
          h('button', { type: 'button', className: 'vls-btn', onClick: () => setRefreshToken((value) => value + 1) }, t('common.refresh')),
          h('span', { className: `vls-dot${error ? ' vls-dot--error' : ''}` }),
        ),
        h(
          'nav',
          { className: 'vls-tabs' },
          TABS.map((value) =>
            h('button', { key: value, type: 'button', className: `vls-tab${tab === value ? ' vls-tab--active' : ''}`, onClick: () => setTab(value) }, t(`tab.${value}`)),
          ),
        ),
        error ? h('div', { className: 'vls-banner' }, h(ErrorBox, { error, onRetry: () => { setError(null); setRefreshToken((value) => value + 1); } })) : null,
        h('div', { className: 'vls-body' }, view),
      );
    }

    function StudioIcon(props) {
      const size = props && props.size ? props.size : 18;
      const active = props && props.active;
      return h(
        'svg',
        { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true, style: { display: 'block', opacity: active ? 1 : 0.82 } },
        h('circle', { cx: '12', cy: '12', r: '9.2', stroke: 'currentColor', strokeWidth: '1.6', opacity: '0.5' }),
        h('circle', { cx: '12', cy: '5.6', r: '1.9', fill: 'currentColor' }),
        h('circle', { cx: '5.9', cy: '16.3', r: '1.9', fill: 'currentColor' }),
        h('circle', { cx: '18.1', cy: '16.3', r: '1.9', fill: 'currentColor' }),
        h('path', { d: 'M12 5.6 5.9 16.3M12 5.6l6.1 10.7M5.9 16.3h12.2', stroke: 'currentColor', strokeWidth: '1.3', opacity: '0.7' }),
      );
    }

    /* ------------------------------------------------------------------ styles */

    const CSS = `
.vls-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  gap: 0;
  color: var(--dsw-alias-label-primary);
  background: var(--dsw-alias-bg-base);
  font-size: 13px;
  line-height: 1.5;
}
.vls-root *, .vls-root *::before, .vls-root *::after { box-sizing: border-box; }
.vls-mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
.vls-dim { color: var(--dsw-alias-label-secondary); }
.vls-accent { color: var(--dsw-alias-brand-primary); }
.vls-spacer { flex: 1 1 auto; }
.vls-gap { margin-top: 10px; }

.vls-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-specific-sidebar-fill, var(--dsw-alias-bg-layer-1));
  flex: 0 0 auto;
}
.vls-brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.vls-brand-mark { color: var(--dsw-alias-brand-primary); font-size: 18px; line-height: 1; }
.vls-brand-title { font-weight: 600; letter-spacing: 0.01em; }
.vls-brand-sub { font-size: 11.5px; color: var(--dsw-alias-label-secondary); }
.vls-field { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--dsw-alias-label-secondary); }
.vls-select, .vls-input {
  font: inherit;
  color: var(--dsw-alias-label-primary);
  background: var(--dsw-alias-bg-layer-1);
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 8px;
  padding: 5px 9px;
  min-width: 0;
}
.vls-select { max-width: 240px; }
.vls-input { flex: 0 1 220px; }
.vls-select:focus-visible, .vls-input:focus-visible, .vls-btn:focus-visible, .vls-tab:focus-visible, .vls-chip--btn:focus-visible {
  outline: 2px solid var(--dsw-alias-brand-primary);
  outline-offset: 1px;
}
.vls-btn {
  font: inherit;
  color: var(--dsw-alias-label-primary);
  background: var(--dsw-alias-bg-layer-1);
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 8px;
  padding: 5px 11px;
  cursor: pointer;
}
.vls-btn:hover { background: var(--dsw-alias-bg-layer-2); }
.vls-btn:disabled { opacity: 0.45; cursor: default; }
.vls-btn--sm { padding: 3px 8px; font-size: 11.5px; }
.vls-switch { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--dsw-alias-label-secondary); cursor: pointer; }
.vls-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dsw-alias-state-success-primary); }
.vls-dot--error { background: var(--dsw-alias-state-error-primary); }

.vls-tabs { display: flex; gap: 2px; padding: 0 18px; border-bottom: 1px solid var(--dsw-alias-border-l1); flex: 0 0 auto; overflow-x: auto; }
.vls-tab {
  font: inherit;
  color: var(--dsw-alias-label-secondary);
  background: none;
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 9px 12px;
  cursor: pointer;
  white-space: nowrap;
}
.vls-tab:hover { color: var(--dsw-alias-label-primary); }
.vls-tab--active { color: var(--dsw-alias-brand-primary); border-bottom-color: var(--dsw-alias-brand-primary); }

.vls-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 16px 18px 26px; }
.vls-stack { display: flex; flex-direction: column; gap: 14px; }
.vls-stack.vls-fill { min-height: 100%; }
.vls-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.vls-cols { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.vls-split { display: grid; gap: 14px; grid-template-columns: minmax(0, 1fr) 300px; align-items: start; }
@media (max-width: 900px) { .vls-split { grid-template-columns: minmax(0, 1fr); } }

.vls-card {
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 12px;
  background: var(--dsw-alias-bg-layer-1);
  overflow: hidden;
}
.vls-card-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--dsw-alias-border-l1); }
.vls-card-title { font-weight: 600; font-size: 12.5px; }
.vls-card-sub { font-size: 11.5px; color: var(--dsw-alias-label-secondary); }
.vls-card-body { padding: 12px 14px; }
.vls-card-body--flush { padding: 0; }

.vls-metric { border: 1px solid var(--dsw-alias-border-l1); border-radius: 12px; padding: 12px 14px; background: var(--dsw-alias-bg-layer-1); }
.vls-metric-value { font-size: 24px; font-weight: 600; letter-spacing: -0.02em; }
.vls-metric-label { font-size: 11.5px; color: var(--dsw-alias-label-secondary); margin-top: 2px; }
.vls-metric-sub { font-size: 11px; color: var(--dsw-alias-label-secondary); opacity: 0.75; }

.vls-chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; font-size: 11.5px; border: 1px solid var(--dsw-alias-border-l1); background: var(--dsw-alias-bg-layer-2); color: var(--dsw-alias-label-secondary); white-space: nowrap; }
.vls-chip--accent { color: var(--dsw-alias-brand-primary); border-color: color-mix(in srgb, var(--dsw-alias-brand-primary) 40%, transparent); background: color-mix(in srgb, var(--dsw-alias-brand-primary) 12%, transparent); }
.vls-chip--ok { color: var(--dsw-alias-state-success-primary); border-color: color-mix(in srgb, var(--dsw-alias-state-success-primary) 40%, transparent); background: color-mix(in srgb, var(--dsw-alias-state-success-primary) 12%, transparent); }
.vls-chip--warn { color: var(--dsw-alias-state-warn-primary); border-color: color-mix(in srgb, var(--dsw-alias-state-warn-primary) 40%, transparent); background: color-mix(in srgb, var(--dsw-alias-state-warn-primary) 12%, transparent); }
.vls-chip--error, .vls-chip--critical { color: var(--dsw-alias-state-error-primary); border-color: color-mix(in srgb, var(--dsw-alias-state-error-primary) 40%, transparent); background: color-mix(in srgb, var(--dsw-alias-state-error-primary) 12%, transparent); }
.vls-chip--debug { color: var(--dsw-alias-label-secondary); }
.vls-chip--info { color: var(--dsw-alias-brand-primary); }
.vls-chip--btn { cursor: pointer; font: inherit; font-size: 11.5px; }
.vls-chip-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }

.vls-bar-row { display: grid; grid-template-columns: 82px minmax(0, 1fr) 54px; align-items: center; gap: 8px; padding: 2px 0; }
.vls-bar-label { font-size: 11.5px; color: var(--dsw-alias-label-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vls-bar { height: 6px; border-radius: 999px; background: var(--dsw-alias-bg-layer-2); overflow: hidden; }
.vls-bar-fill { height: 100%; border-radius: 999px; background: var(--dsw-alias-brand-primary); }
.vls-bar-value { font-size: 11px; text-align: right; color: var(--dsw-alias-label-secondary); }

.vls-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 10px; border: 1px solid var(--dsw-alias-border-l1); border-radius: 10px; background: var(--dsw-alias-bg-layer-1); }
.vls-toolbar-label { font-size: 11.5px; color: var(--dsw-alias-label-secondary); }
.vls-pager { display: flex; align-items: center; gap: 10px; justify-content: flex-end; }

.vls-loglist {
  flex: 1 1 auto;
  min-height: 320px;
  max-height: calc(100vh - 300px);
  overflow: auto;
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 10px;
  background: var(--dsw-alias-bg-layer-1);
}
.vls-logrow { display: grid; grid-template-columns: 96px 74px minmax(0, 1fr); gap: 8px; padding: 5px 10px; border-bottom: 1px solid var(--dsw-alias-border-l2, var(--dsw-alias-border-l1)); align-items: start; }
.vls-logrow:hover { background: var(--dsw-alias-bg-layer-2); }
.vls-logtime { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.vls-log-level { font-size: 10.5px; letter-spacing: 0.04em; font-weight: 600; }
.vls-log-level--debug { color: var(--dsw-alias-label-secondary); }
.vls-log-level--info { color: var(--dsw-alias-brand-primary); }
.vls-log-level--warning { color: var(--dsw-alias-state-warn-primary); }
.vls-log-level--error, .vls-log-level--critical { color: var(--dsw-alias-state-error-primary); }
.vls-log-msg { display: flex; gap: 8px; min-width: 0; cursor: pointer; }
.vls-log-logger { color: var(--dsw-alias-label-secondary); font-size: 11px; flex: 0 0 auto; }
.vls-log-text { min-width: 0; overflow-wrap: anywhere; font-size: 12px; }
.vls-log-exp { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 12px; padding: 6px 0 2px; font-size: 11px; color: var(--dsw-alias-label-secondary); }

.vls-memlist { display: flex; flex-direction: column; gap: 10px; }
.vls-mem { border: 1px solid var(--dsw-alias-border-l1); border-radius: 10px; background: var(--dsw-alias-bg-layer-1); padding: 10px 12px; }
.vls-mem--open { border-color: color-mix(in srgb, var(--dsw-alias-brand-primary) 45%, var(--dsw-alias-border-l1)); }
.vls-mem-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; cursor: pointer; }
.vls-mem-content { margin: 8px 0 6px; font-size: 12.5px; white-space: pre-wrap; overflow-wrap: anywhere; }
.vls-mem-meta { display: grid; gap: 2px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.vls-mem-detail { margin-top: 8px; }

.vls-graphwrap { border: 1px solid var(--dsw-alias-border-l1); border-radius: 12px; background: var(--dsw-alias-bg-layer-1); overflow: hidden; }
.vls-graph { width: 100%; height: auto; display: block; }
.vls-edge { stroke: var(--dsw-alias-border-l2, var(--dsw-alias-border-l1)); stroke-width: 1.4; }
.vls-edge--active { stroke: var(--dsw-alias-brand-primary); stroke-width: 2.2; }
.vls-edge-label { fill: var(--dsw-alias-label-secondary); font-size: 10.5px; text-anchor: middle; }
.vls-node-dot { fill: color-mix(in srgb, var(--dsw-alias-brand-primary) 22%, var(--dsw-alias-bg-layer-1)); stroke: var(--dsw-alias-brand-primary); stroke-width: 1.6; }
.vls-node--active .vls-node-dot { fill: color-mix(in srgb, var(--dsw-alias-brand-primary) 55%, var(--dsw-alias-bg-layer-1)); }
.vls-node-label { fill: var(--dsw-alias-label-primary); font-size: 11.5px; text-anchor: middle; }
.vls-node-count { fill: var(--dsw-alias-label-primary); font-size: 10.5px; text-anchor: middle; font-weight: 600; }
.vls-rels { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.vls-rel { border: 1px solid var(--dsw-alias-border-l1); border-radius: 8px; padding: 7px 9px; font-size: 12px; }
.vls-section-title { margin-top: 12px; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--dsw-alias-label-secondary); }

.vls-radar-wrap { display: flex; justify-content: center; }
.vls-radar { width: 100%; max-width: 300px; height: auto; }
.vls-radar-grid { fill: none; stroke: var(--dsw-alias-border-l1); stroke-width: 1; }
.vls-radar-axis { stroke: var(--dsw-alias-border-l1); stroke-width: 1; }
.vls-radar-shape { fill: color-mix(in srgb, var(--dsw-alias-brand-primary) 22%, transparent); stroke: var(--dsw-alias-brand-primary); stroke-width: 1.8; }
.vls-radar-label { fill: var(--dsw-alias-label-secondary); font-size: 10.5px; }

.vls-quote { margin: 0 0 10px; padding: 10px 12px; border-left: 3px solid var(--dsw-alias-brand-primary); background: var(--dsw-alias-bg-layer-2); border-radius: 0 8px 8px 0; font-size: 13px; }
.vls-thinking { margin: 10px 0 0; padding: 10px; border-radius: 8px; background: var(--dsw-alias-bg-overlay, var(--dsw-alias-bg-layer-2)); border: 1px solid var(--dsw-alias-border-l1); font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 320px; overflow: auto; }
.vls-json { margin: 8px 0 0; padding: 10px; border-radius: 8px; background: var(--dsw-alias-bg-overlay, var(--dsw-alias-bg-layer-2)); border: 1px solid var(--dsw-alias-border-l1); font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow: auto; }

.vls-timeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.vls-tl { padding: 10px 14px; border-bottom: 1px solid var(--dsw-alias-border-l1); }
.vls-tl:last-child { border-bottom: 0; }
.vls-tl--active { background: color-mix(in srgb, var(--dsw-alias-brand-primary) 8%, transparent); }
.vls-tl-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.vls-tl-time { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.vls-tl-text { margin-top: 6px; font-size: 12.5px; overflow-wrap: anywhere; }

.vls-kv { display: grid; grid-template-columns: minmax(88px, auto) minmax(0, 1fr); gap: 6px 14px; margin: 10px 0 0; font-size: 12px; }
.vls-kv dt { color: var(--dsw-alias-label-secondary); white-space: nowrap; }
.vls-kv dd { margin: 0; overflow-wrap: anywhere; }

.vls-empty { padding: 34px 18px; text-align: center; border: 1px dashed var(--dsw-alias-border-l1); border-radius: 12px; background: var(--dsw-alias-bg-layer-1); }
.vls-empty-title { font-weight: 600; }
.vls-empty-sub { margin-top: 6px; font-size: 12px; color: var(--dsw-alias-label-secondary); }
.vls-loader { padding: 22px; text-align: center; color: var(--dsw-alias-label-secondary); font-size: 12px; }
.vls-error { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 10px; border: 1px solid color-mix(in srgb, var(--dsw-alias-state-error-primary) 40%, transparent); background: color-mix(in srgb, var(--dsw-alias-state-error-primary) 10%, transparent); color: var(--dsw-alias-state-error-primary); font-size: 12px; overflow-wrap: anywhere; }
.vls-banner { padding: 10px 18px 0; }
.vls-side { display: flex; flex-direction: column; gap: 14px; }
`;

    /* ------------------------------------------------------------------ export */

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { zh: ZH, en: EN }), 'virtual-life-studio: dictionary');
        translate = ctx.locale.bind(NS);
        ctx.effect(
          () => {
            try {
              return ctx.on('locale/change', () => {
                translate = ctx.locale.bind(NS);
                for (const listener of localeListeners) listener();
              });
            } catch (error) {
              console.warn('[virtual-life-studio] locale change subscription unavailable', error);
              return () => {};
            }
          },
          'virtual-life-studio: locale change',
        );
        ctx.slots.inject('virtual-life-studio:icon', () =>
          ctx.slots.register({ name: 'sidebar.panellist', id: 'virtual-life-studio', order: 40, label: () => translate('panel.title') }, StudioIcon),
        );
        ctx.slots.inject('virtual-life-studio:panel', () => ctx.slots.register({ name: 'main', key: 'virtual-life-studio' }, StudioPanel));
      },
    };
  },
});