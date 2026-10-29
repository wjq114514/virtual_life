// @ts-check
/**
 * DSH inner-life consumer bridge for the Virtual Life companion runtime.
 *
 * This plugin is the *inner monologue* half of the integration. The runtime
 * decides *when* an inner activity is due (rules or model assisted) and, in
 * model mode, captures the model's reasoning channel as `thinking`. This
 * plugin polls the inner-life activity channel on a timer, keeps the newest
 * activity (and its thinking) in a small cache, and registers a labeled
 * bounded context block so the DSH model sees the companion's current inner
 * state — data, not instructions. It also exposes tools to inspect the
 * snapshot and to fetch the next activity page on demand.
 */

/** @type {{text: string, fetchedAt: number, activityId: string|null, sequence: number}|null} */
let cache = null;

async function fetchJson(baseUrl, token, path) {
  const headers = { Accept: 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(`${baseUrl}${path}`, { headers });
  } catch (error) {
    throw new Error(`runtime unreachable at ${baseUrl}: ${error.message}`);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`runtime HTTP ${res.status}: ${text.slice(0, 300)}`);
  }
  return res.json();
}

/** Compact, bounded renderer for the inner-life snapshot. */
function renderInner(data) {
  const inner = data.inner;
  if (!inner || !inner.enabled) return '';
  const lines = [];
  lines.push(`[VIRTUAL_LIFE_INNER_LIFE]`);
  lines.push(JSON.stringify({
    companion_id: data.companionId,
    enabled: inner.enabled,
    model_assisted: !!inner.model_assisted,
    dominant_kind: inner.dominant_kind,
    salience: inner.salience,
    urge: inner.urge,
    model_calls: inner.model_calls,
  }));
  lines.push(`[/VIRTUAL_LIFE_INNER_LIFE]`);
  const current = inner.current;
  if (current) {
    lines.push(`[VIRTUAL_LIFE_INNER_ACTIVITY]`);
    lines.push(JSON.stringify({
      activity_id: current.activity_id,
      sequence: current.sequence,
      kind: current.kind,
      trigger: current.trigger,
      content: current.content,
      salience: current.salience,
      urge: current.urge,
      generator: current.generator,
      // The captured inner monologue from the model's reasoning channel.
      // Null when the activity came from rules (no model run).
      thinking: current.thinking || null,
    }));
    lines.push(`[/VIRTUAL_LIFE_INNER_ACTIVITY]`);
  }
  return lines.join('\n');
}

async function refresh(cfg) {
  const inner = await fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}/inner-life`).catch(() => null);
  let text = renderInner({ companionId: cfg.companionId, inner });
  if (text.length > cfg.maxContextChars) text = text.slice(0, cfg.maxContextChars);
  const current = inner && inner.current;
  cache = {
    text,
    fetchedAt: Date.now(),
    activityId: current ? current.activity_id : null,
    sequence: current ? current.sequence : 0,
  };
  return cache;
}

export const inject = ['systemPrompt', 'timer', 'tools'];

export function apply(ctx, config) {
  const cfg = {
    baseUrl: config.baseUrl || 'http://127.0.0.1:8038',
    token: config.token || '',
    companionId: config.companionId || '',
    refreshMs: Math.max(5000, config.refreshMs || 30000),
    maxContextChars: config.maxContextChars || 3000,
    order: config.order || 230,
  };
  const disposers = [];

  if (!cfg.companionId) {
    return () => {};
  }

  const textOutput = { schema: { type: 'object' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }] };

  const refreshNow = () => refresh(cfg).catch(() => {});
  refreshNow();
  disposers.push(ctx.timer.interval(refreshNow, cfg.refreshMs));

  disposers.push(ctx.systemPrompt.context({
    name: 'virtual-life:inner-life',
    order: cfg.order,
    interpolate: false,
    text: () => (cache && cache.text ? cache.text : ''),
  }));

  disposers.push(ctx.tools.register({
    name: 'virtual_life_innerlife_status',
    description:
      "Inspect the Virtual Life inner-life state for a companion: enabled, model_assisted, dominant kind, salience, urge, and the current activity including its captured thinking (inner monologue). Read-only.",
    parameters: {
      type: 'object', additionalProperties: false,
      properties: { companionId: { type: 'string', description: 'Companion id.' } },
      required: ['companionId'],
    },
    output: textOutput,
    async execute(args) {
      const companionId = String(args.companionId || cfg.companionId);
      const inner = await fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(companionId)}/inner-life`);
      const current = inner.current || null;
      return {
        companionId,
        enabled: inner.enabled,
        model_assisted: !!inner.model_assisted,
        model_calls: inner.model_calls,
        dominant_kind: inner.dominant_kind,
        salience: inner.salience,
        urge: inner.urge,
        current,
        cachedActivityId: cache ? cache.activityId : null,
        cachedSequence: cache ? cache.sequence : null,
      };
    },
  }));

  disposers.push(ctx.tools.register({
    name: 'virtual_life_innerlife_activities',
    description:
      'Fetch a page of inner-life activities for a companion from the runtime, oldest-first after a cursor. Use afterSequence to page forward; returns items plus nextSequence/hasMore.',
    parameters: {
      type: 'object', additionalProperties: false,
      properties: {
        companionId: { type: 'string', description: 'Companion id.' },
        afterSequence: { type: 'integer', minimum: 0, description: 'Fetch activities after this sequence cursor.' },
        limit: { type: 'integer', minimum: 1, maximum: 100 },
      },
      required: ['companionId'],
    },
    output: textOutput,
    async execute(args) {
      const companionId = String(args.companionId);
      const params = new URLSearchParams();
      if (args.afterSequence != null) params.set('after_sequence', String(args.afterSequence));
      if (args.limit != null) params.set('limit', String(args.limit));
      const qs = params.toString() ? `?${params}` : '';
      const page = await fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(companionId)}/inner-life/activities${qs}`);
      return {
        companionId,
        items: page.items,
        nextSequence: page.next_sequence,
        hasMore: page.has_more,
      };
    },
  }));

  return () => { for (const dispose of disposers) dispose(); };
}

export const name = 'virtual-life-innerlife';
