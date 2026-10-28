// @ts-check
/**
 * DSH proactive-participation driver for the Virtual Life companion runtime.
 *
 * The runtime generates proactive candidates itself (attention boundaries and
 * inner activity, processed by its own background loop). This plugin is the
 * *delivery* half: it polls the runtime's proactive-output channel and turns
 * each ready message into a scheduled independent turn in a DSH session via
 * `ctx.schedule`. It also exposes two tools so the model can inspect the
 * runtime's participation state and trigger a delivery on demand.
 */

/** @type {Map<string, string>} event_id -> schedule id already delivered */
const delivered = new Map();

/** @type {Map<string, number>} companion|node|access -> last polled event sequence */
const cursors = new Map();

async function runtimeFetch(baseUrl, token, path, init = {}) {
  const headers = { ...(init.headers || {}) };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(`${baseUrl}${path}`, { ...init, headers });
  } catch (error) {
    throw new Error(`runtime unreachable at ${baseUrl}: ${error.message}`);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`runtime HTTP ${res.status}: ${text.slice(0, 400)}`);
  }
  return res.json();
}

async function pollOutput(baseUrl, token, { companionId, nodeId, accessId, epoch, afterSequence }) {
  const params = new URLSearchParams({ node_id: nodeId, access_id: accessId, epoch: String(epoch) });
  if (afterSequence != null) params.set('after_sequence', String(afterSequence));
  return runtimeFetch(
    baseUrl, token,
    `/v1/companions/${encodeURIComponent(companionId)}/proactivity/output?${params}`,
  );
}

async function heartbeatNode(baseUrl, token, nodeId) {
  return runtimeFetch(baseUrl, token, `/v1/nodes/${encodeURIComponent(nodeId)}/heartbeat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ context: { source: 'dsh-proactivity-driver' } }),
  });
}

async function heartbeatAccess(baseUrl, token, nodeId, accessId) {
  return runtimeFetch(
    baseUrl, token,
    `/v1/nodes/${encodeURIComponent(nodeId)}/access/${encodeURIComponent(accessId)}/heartbeat`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ context: { source: 'dsh-proactivity-driver' } }) },
  );
}

async function renewLease(baseUrl, token, companionId, nodeId, epoch) {
  return runtimeFetch(baseUrl, token, `/v1/companions/${encodeURIComponent(companionId)}/lease`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node_id: nodeId, epoch }),
  });
}

async function deliver(ctx, cfg, args, sessionId) {
  if (!ctx.schedule) throw new Error('the schedule service is not available in this profile');
  const { companionId, nodeId, accessId } = args;
  const cursorKey = `${companionId}|${nodeId}|${accessId}`;
  const companion = await runtimeFetch(
    cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(companionId)}`,
  );
  const epoch = args.epoch ?? companion.epoch ?? 0;
  if (cfg.keepNodeAlive) {
    try {
      await heartbeatNode(cfg.baseUrl, cfg.token, nodeId);
    } catch {
      // Node liveness is best effort; the lease/poll below report real errors.
    }
    try {
      await heartbeatAccess(cfg.baseUrl, cfg.token, nodeId, accessId);
    } catch {
      // A stale access is recreated by re-installing with a fresh accessId.
    }
  }
  if (!args.skipLease) {
    try {
      await renewLease(cfg.baseUrl, cfg.token, companionId, nodeId, epoch);
    } catch {
      // Renewal can legitimately fail (owner moved, node offline). The poll below
      // returns the authoritative error, and the next tick retries.
    }
  }
  let page;
  try {
    page = await pollOutput(cfg.baseUrl, cfg.token, {
      companionId, nodeId, accessId, epoch,
      afterSequence: args.afterSequence ?? cursors.get(cursorKey),
    });
  } catch (error) {
    // A cursor ahead of the runtime (after a restart) is unrecoverable; drop it so
    // the next tick re-opens a fresh subscription.
    if (String(error.message).includes('output_cursor_invalid')) cursors.delete(cursorKey);
    throw error;
  }
  const max = args.maxPerPoll ?? cfg.poller.maxPerPoll ?? 5;
  const fresh = page.items.filter((item) => !delivered.has(item.event_id));
  const scheduled = [];
  for (const item of fresh.slice(0, max)) {
    if (args.dryRun) {
      scheduled.push({ event_id: item.event_id, message: item.assistant_message, scheduled: false });
      continue;
    }
    const prompt = `${cfg.delivery.promptPrefix}${item.assistant_message}`;
    const record = await ctx.schedule.create(sessionId, {
      title: cfg.delivery.title,
      prompt,
      after_seconds: Math.max(1, args.afterSeconds ?? cfg.delivery.delaySeconds),
    });
    delivered.set(item.event_id, record.id);
    scheduled.push({ event_id: item.event_id, message: item.assistant_message, scheduled: true, scheduleId: record.id });
  }
  // Advance the cursor only once every fresh item was handled; otherwise the next
  // tick re-polls and the delivered set dedupes what already went out.
  if (fresh.length === 0 || scheduled.length === fresh.length) {
    if (page.next_sequence != null) cursors.set(cursorKey, page.next_sequence);
  }
  return {
    companionId, nodeId, accessId, epoch,
    nextSequence: page.next_sequence,
    cursor: cursors.get(cursorKey),
    polled: page.items.length,
    fresh: fresh.length,
    scheduled,
  };
}

export const inject = ['tools', 'timer', 'schedule'];

export function apply(ctx, config) {
  const cfg = {
    baseUrl: config.baseUrl || 'http://127.0.0.1:8038',
    token: config.token || '',
    keepNodeAlive: config.keepNodeAlive === true,
    poller: { enabled: false, intervalMs: 30000, maxPerPoll: 5, ...(config.poller || {}) },
    delivery: { title: '虚拟伙伴主动参与', promptPrefix: '虚拟伙伴主动想说话：', delaySeconds: 1, ...(config.delivery || {}) },
    sessions: Array.isArray(config.sessions) ? config.sessions : [],
  };
  const disposers = [];

  const textOutput = { schema: { type: 'object' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }] };

  disposers.push(ctx.tools.register({
    name: 'virtual_life_proactivity_status',
    description:
      'Inspect the Virtual Life proactive-participation state for a companion: policy, recent decisions, and schedule snapshot. Read-only.',
    parameters: {
      type: 'object', additionalProperties: false,
      properties: { companionId: { type: 'string', description: 'Companion id.' } },
      required: ['companionId'],
    },
    output: textOutput,
    async execute(args) {
      const companionId = String(args.companionId);
      const [policy, decisions, schedule] = await Promise.all([
        runtimeFetch(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(companionId)}/proactivity`),
        runtimeFetch(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(companionId)}/proactivity/decisions?latest=true&limit=5`),
        runtimeFetch(cfg.baseUrl, cfg.token, `/v1/companions/${encodeURIComponent(companionId)}/proactivity/schedule`),
      ]);
      return {
        companionId,
        policy,
        decisions,
        schedule,
        deliveredEventCount: delivered.size,
        cursors: Object.fromEntries(cursors),
      };
    },
  }));

  disposers.push(ctx.tools.register({
    name: 'virtual_life_proactivity_deliver',
    description:
      'Poll the Virtual Life proactive-output channel for a companion and schedule each new message into a session as an independent turn. Pass dryRun to report without scheduling.',
    parameters: {
      type: 'object', additionalProperties: false,
      properties: {
        companionId: { type: 'string', description: 'Companion id.' },
        nodeId: { type: 'string', description: 'Node owning the companion.' },
        accessId: { type: 'string', description: 'Live web access id bound to that node.' },
        sessionId: { type: 'string', description: 'Delivery session; defaults to the calling session.' },
        afterSeconds: { type: 'integer', minimum: 1, description: 'Delay in seconds before the delivery prompt fires (must be positive).' },
        maxPerPoll: { type: 'integer', minimum: 1, maximum: 20 },
        dryRun: { type: 'boolean', description: 'Report what would be delivered without scheduling.' },
        skipLease: { type: 'boolean', description: 'Skip the output-lease renewal before polling.' },
      },
      required: ['companionId', 'nodeId', 'accessId'],
    },
    output: textOutput,
    async execute(args, exec) {
      const sessionId = args.sessionId || exec.agent?.id;
      if (!sessionId) throw new Error('no session to deliver into; pass sessionId or call from an agent');
      const result = await deliver(ctx, cfg, { ...args, sessionId }, sessionId);
      return { ...result, sessionId };
    },
  }));

  if (cfg.poller.enabled) {
    const stop = ctx.timer.interval(async () => {
      for (const mapping of cfg.sessions) {
        try {
          await deliver(ctx, cfg, mapping, mapping.sessionId);
        } catch {
          // A failed poll must not stop the loop; the next tick retries.
        }
      }
    }, cfg.poller.intervalMs);
    disposers.push(stop);
  }

  return () => { for (const dispose of disposers) dispose(); };
}
