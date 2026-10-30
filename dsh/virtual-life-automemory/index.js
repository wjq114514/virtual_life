// @ts-check
/**
 * Automatic-memory extraction bridge for the Virtual Life companion runtime.
 *
 * The runtime's automatic-memory worker normally generates a compact memory
 * summary (facts / preferences / relationships plus a short narrative) by
 * calling its own model. When the runtime runs with `auto_memory_external`
 * enabled it instead *parks* the job (table `auto_memory_external`,
 * status=parked) and returns a receipt with the assembled context
 * (instruction + JSON schema + source dialogue). This plugin is the DSH-side
 * generator: it polls the parked jobs, asks the DSH LLM to produce the
 * Extraction JSON, and commits the result back through the runtime's
 * external-commit route.
 *
 * The runtime keeps every provenance, evidence, birthday-slot, relation and
 * stale-source gate; DSH only writes the body of the extraction. A generation
 * failure or malformed body leaves the job parked, and the runtime reaps it
 * after its commit deadline (releasing the model-token reservation), so a dead
 * generator can never leak a reservation or a half-written memory.
 *
 * This bundle is deliberately self-contained (global `fetch` + DI only), like
 * the persona/innerlife/proactivity bridges: the DSH bundle loader resolves the
 * real path of this file, and a bare `import('@deepseek-ai/dsh-llm')` would
 * fail to resolve from there. So the LLM stream is assembled inline.
 */

/** @type {Map<string, {jobId: string, startedAt: number, status: string, error?: string}>} */
const attempts = new Map();

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

/** Strip a ```json ... ``` fence if the model wrapped its answer. */
function stripFences(text) {
  const trimmed = text.trim();
  const fence = /^```(?:json)?\s*([\s\S]*?)\s*```$/i.exec(trimmed);
  if (fence) return fence[1].trim();
  return trimmed;
}

/**
 * Minimal inline stream assembler: accumulates text deltas and remembers the
 * terminal usage/finish chunks. The extraction answer is plain text, so we do
 * not need the full BlockAssembler; tool calls and reasoning are ignored.
 */
class InlineAssembler {
  constructor() {
    this.text = '';
    this.usage = undefined;
    this.finish = undefined;
  }

  push(chunk) {
    if (!chunk || typeof chunk !== 'object') return;
    switch (chunk.type) {
      case 'text-delta':
        this.text += chunk.text ?? '';
        return;
      case 'usage':
        this.usage = chunk.usage;
        return;
      case 'finish':
        this.finish = chunk.reason ?? { kind: 'stop' };
        return;
      default:
        return;
    }
  }
}

/**
 * Generate the Extraction JSON for one parked job with the DSH LLM and commit
 * it back. Returns the runtime commit response, or a status record on failure.
 */
async function generateAndCommit(ctx, cfg, job) {
  const companionId = job.companion_id;
  const jobId = job.job_id;
  const context = job.context || {};
  const sources = Array.isArray(context.sources) ? context.sources : [];

  const sourceText = sources
    .map((s) => `[source_event_id: ${s.source_event_id}]\n用户：${s.user_message || ''}\n助手：${s.assistant_message || ''}`)
    .join('\n\n');

  const system =
    '你是虚拟伙伴的记忆整理助手。只从下方用户原话中提取稳定的记忆，'
    + '助手文本只是对话行为，不作为证据。严格按给定 JSON schema 返回，不要用 Markdown 代码块包裹，'
    + '不要输出任何解释文字。证据不足时用空数组。summary 保持简洁（约200中文字或120英文词）。'
    + 'semantic_key 必须是稳定领域槽位名（如 preferred_drink），不是其取值。'
    + '不要推断生日或主体。relations 引用 memories 的下标，端点必须出现在对应记忆内容与证据里。';

  const user =
    `# 任务\n${context.instruction || '从下方对话提取稳定的用户记忆。'}\n\n`
    + `# 数据源\n${sourceText || '（无数据源）'}\n\n`
    + `# 输出 schema\n${JSON.stringify(context.schema || {}, null, 2)}`;

  const messages = [
    { role: 'user', content: [{ type: 'text', text: user }] },
  ];

  const assembler = new InlineAssembler();
  try {
    const options = {
      provider: cfg.provider,
      model: cfg.model,
      messages,
      system,
      maxTokens: cfg.maxTokens,
      purpose: 'compaction',
    };
    for await (const chunk of ctx.llm.stream(options)) assembler.push(chunk);
  } catch (error) {
    const err = String(error?.message || error);
    attempts.set(jobId, { jobId, startedAt: Date.now(), status: 'generate_failed', error: err });
    return { jobId, status: 'generate_failed', error: err };
  }

  if (assembler.finish && assembler.finish.kind === 'error') {
    const err = String(assembler.finish);
    attempts.set(jobId, { jobId, startedAt: Date.now(), status: 'generate_failed', error: err });
    return { jobId, status: 'generate_failed', error: err };
  }

  const extractionText = stripFences(assembler.text);
  const usage = assembler.usage || {};
  const inputTokens = typeof usage.inputTokens === 'number' ? usage.inputTokens : 0;
  const outputTokens = typeof usage.outputTokens === 'number' ? usage.outputTokens : 0;

  if (!extractionText) {
    attempts.set(jobId, { jobId, startedAt: Date.now(), status: 'empty_output' });
    return { jobId, status: 'empty_output' };
  }

  try {
    const committed = await runtimeFetch(
      cfg.baseUrl, cfg.token,
      `/v1/companions/${encodeURIComponent(companionId)}/memories/automation/external/${encodeURIComponent(jobId)}/commit`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          extraction_text: extractionText,
          input_tokens: inputTokens,
          output_tokens: outputTokens,
        }),
      },
    );
    attempts.set(jobId, { jobId, startedAt: Date.now(), status: committed.status || 'committed' });
    return committed;
  } catch (error) {
    const err = String(error?.message || error);
    attempts.set(jobId, { jobId, startedAt: Date.now(), status: 'commit_failed', error: err });
    return { jobId, status: 'commit_failed', error: err };
  }
}

async function pollOnce(ctx, cfg) {
  const pending = await runtimeFetch(
    cfg.baseUrl, cfg.token,
    `/v1/companions/${encodeURIComponent(cfg.companionId)}/memories/automation/external/pending`,
  );
  const items = Array.isArray(pending.items) ? pending.items : [];
  const handled = [];
  for (const job of items.slice(0, cfg.maxPerPoll)) {
    const result = await generateAndCommit(ctx, cfg, job);
    handled.push(result);
  }
  return { polled: items.length, handled, pendingCount: pending.count ?? items.length };
}

export const inject = ['tools', 'timer', 'llm'];

export function apply(ctx, config) {
  const cfg = {
    baseUrl: config.baseUrl || 'http://127.0.0.1:8038',
    token: config.token || '',
    companionId: config.companionId || '',
    provider: config.provider || '',
    model: config.model || '',
    pollMs: Math.max(5000, config.pollMs || 30000),
    maxTokens: config.maxTokens || 1200,
    maxPerPoll: Math.max(1, Math.min(5, config.maxPerPoll || 1)),
    order: config.order || 240,
  };
  const disposers = [];

  if (!cfg.companionId) return () => {};

  const textOutput = { schema: { type: 'object' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }] };

  /** @type {Promise<object>|null} */
  let inFlight = null;
  const tick = () => {
    if (inFlight) return inFlight;
    inFlight = pollOnce(ctx, cfg)
      .catch(() => ({ polled: 0, handled: [] }))
      .finally(() => { inFlight = null; });
    return inFlight;
  };

  tick();

  disposers.push(ctx.tools.register({
    name: 'virtual_life_automemory_status',
    description:
      'Inspect the Virtual Life automatic-memory external bridge: recent poll outcome, pending job count, and per-job attempt records. Read-only.',
    parameters: {
      type: 'object', additionalProperties: false,
      properties: { companionId: { type: 'string', description: 'Companion id.' } },
      required: ['companionId'],
    },
    output: textOutput,
    async execute(args) {
      const companionId = String(args.companionId || cfg.companionId);
      const pending = await runtimeFetch(
        cfg.baseUrl, cfg.token,
        `/v1/companions/${encodeURIComponent(companionId)}/memories/automation/external/pending`,
      ).catch((error) => ({ items: [], count: 0, error: String(error?.message || error) }));
      return {
        companionId,
        provider: cfg.provider,
        model: cfg.model,
        pendingCount: pending.count ?? (Array.isArray(pending.items) ? pending.items.length : 0),
        pending,
        attempts: [...attempts.values()].slice(-20),
      };
    },
  }));

  disposers.push(ctx.tools.register({
    name: 'virtual_life_automemory_poll',
    description:
      'Poll the Virtual Life parked automatic-memory jobs and generate/commit any waiting extraction through the DSH LLM. Returns the per-job outcome.',
    parameters: {
      type: 'object', additionalProperties: false,
      properties: { companionId: { type: 'string', description: 'Companion id.' } },
      required: ['companionId'],
    },
    output: textOutput,
    async execute(args) {
      const companionId = String(args.companionId || cfg.companionId);
      if (companionId !== cfg.companionId) {
        throw new Error(`this bridge is configured for companion ${cfg.companionId}, not ${companionId}`);
      }
      return tick();
    },
  }));

  disposers.push(ctx.timer.interval(tick, cfg.pollMs));

  return () => {
    for (const dispose of disposers) dispose();
  };
}

export const name = 'virtual-life-automemory';
