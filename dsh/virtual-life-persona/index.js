// @ts-check
/**
 * DSH persona/context bridge for the Virtual Life companion runtime.
 *
 * This plugin is the *identity* half of the integration: it refreshes the
 * companion's personality definition, mood/emotion state, slow variables, and
 * inner activity from the runtime on a timer, and registers a dynamic prompt
 * context block that carries that compact, bounded snapshot into the DSH
 * system prompt. The read is synchronous from a small cache (the same
 * pattern as the proactivity poller), so the prompt assembly never awaits a
 * network call and a failed refresh simply renders nothing.
 *
 * The block is labeled data, not instructions: it tells the model what the
 * companion is like, never what to do. It deliberately does NOT include the
 * current user message or history — those belong to the dialogue turn, not to
 * the standing context.
 */

/** @type {{text: string, fetchedAt: number}|null} */
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

/** Compact, bounded renderer for the companion snapshot. */
function renderCompanion(data) {
  const p = data.personality;
  if (!p || !p.definition) return '';
  const d = p.definition;
  const lines = [];
  lines.push(`[VIRTUAL_LIFE_COMPANION]`);
  lines.push(JSON.stringify({
    name: p.name,
    companion_id: data.companionId,
    description: d.description,
    personality: d.personality,
    values: d.values,
    boundaries: d.boundaries,
    traits: d.traits,
    speech_style: d.speech_style,
    scenario: d.scenario,
  }));
  lines.push(`[/VIRTUAL_LIFE_COMPANION]`);

  if (data.state && data.state.state) {
    const s = data.state.state;
    lines.push(`[VIRTUAL_LIFE_STATE]`);
    lines.push(JSON.stringify({
      mood: s.mood, energy: s.energy, social_battery: s.social_battery,
      emotion: data.emotion && data.emotion.state ? data.emotion.state : undefined,
    }));
    lines.push(`[/VIRTUAL_LIFE_STATE]`);
  }

  if (data.slow && data.slow.enabled) {
    lines.push(`[VIRTUAL_LIFE_SLOW_VARIABLES]`);
    lines.push(JSON.stringify({
      engagement: data.slow.engagement,
      familiarity: data.slow.familiarity,
      relationship_warmth: data.slow.relationship_warmth,
      tone_hint: data.slow.tone_hint,
      sample_size: data.slow.sample_size,
    }));
    lines.push(`[/VIRTUAL_LIFE_SLOW_VARIABLES]`);
  }

  if (data.inner && data.inner.enabled && data.inner.current) {
    const a = data.inner.current;
    lines.push(`[VIRTUAL_LIFE_INNER_ACTIVITY]`);
    lines.push(JSON.stringify({
      kind: a.kind, trigger: a.trigger, content: a.content,
      salience: a.salience, urge: a.urge,
    }));
    lines.push(`[/VIRTUAL_LIFE_INNER_ACTIVITY]`);
  }

  return lines.join('\n');
}

async function refresh(cfg) {
  const [personality, state, slow, emotion, inner] = await Promise.all([
    fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}/personality`).catch(() => null),
    fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}/personality/state`).catch(() => null),
    fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}/slow-variables`).catch(() => null),
    fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}/emotion`).catch(() => null),
    fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}/inner-life`).catch(() => null),
  ]);
  const companion = await fetchJson(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(cfg.companionId)}`).catch(() => null);
  let text = renderCompanion({
    companionId: cfg.companionId,
    name: companion && companion.name,
    personality,
    state,
    slow,
    emotion,
    inner,
  });
  if (text.length > cfg.maxContextChars) text = text.slice(0, cfg.maxContextChars);
  cache = { text, fetchedAt: Date.now() };
}

export const inject = ['systemPrompt', 'timer'];

export function apply(ctx, config) {
  const cfg = {
    baseUrl: config.baseUrl || 'http://127.0.0.1:8038',
    token: config.token || '',
    companionId: config.companionId || '',
    refreshMs: Math.max(5000, config.refreshMs || 30000),
    maxContextChars: config.maxContextChars || 4000,
    order: config.order || 220,
  };
  const disposers = [];

  if (!cfg.companionId) {
    // No companion bound: register nothing, render nothing. The profile stays
    // valid and a later config change activates the block.
    return () => {};
  }

  // Warm the cache once, then keep it fresh on a timer. A failed refresh leaves
  // the last good snapshot; a first-ever failure leaves the block empty.
  const refreshNow = () => refresh(cfg).catch(() => {});
  refreshNow();
  disposers.push(ctx.timer.interval(refreshNow, cfg.refreshMs));

  disposers.push(ctx.systemPrompt.context({
    name: 'virtual-life:companion',
    order: cfg.order,
    interpolate: false,
    text: () => (cache && cache.text ? cache.text : ''),
  }));

  return () => { for (const dispose of disposers) dispose(); };
}

export const name = 'virtual-life-persona';
