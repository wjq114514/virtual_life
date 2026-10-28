# dsh-virtual-life-proactivity

DSH (DeepSeek Harness) delivery driver for the Virtual Life companion runtime's
**proactive participation** feature.

The runtime owns the proactive half: its background loop watches node attention
samples (focus / boundary events) and inner activity, produces *candidates*, and
runs a proactive dialogue turn when a candidate is eligible. This plugin is the
*delivery* half — it keeps the bound device node (and its output access + lease)
alive, polls the runtime's proactive-output channel, and turns each ready message
into a scheduled independent turn in a DSH session via `ctx.schedule`.

## Tools

| Tool | Purpose |
| --- | --- |
| `virtual_life_proactivity_status` | Read the runtime's participation state for a companion: policy, recent decisions, schedule snapshot, and this driver's delivery/cursor counters. |
| `virtual_life_proactivity_deliver` | Poll the output channel on demand and schedule each new message; `dryRun` reports without scheduling. |

## Config (`sessions[]`)

```yaml
- id: virtual-life-proactivity
  name: '@virtuallife/dsh-virtual-life-proactivity'
  config:
    baseUrl: http://127.0.0.1:8038
    token: local-dev-token
    keepNodeAlive: true        # heartbeat node + web access, renew the output lease
    poller:
      enabled: true
      intervalMs: 6000
      maxPerPoll: 5
    delivery:
      title: 虚拟伙伴主动参与
      promptPrefix: 虚拟伙伴主动想说话：
      delaySeconds: 1
    sessions:
      - sessionId: session-9cb72bcb-0ab3-4b47-ad52-53de361bad82
        companionId: proact-demo
        nodeId: proact-node
        accessId: proact-access-2
```

## Verified end-to-end flow (2026-10-27)

1. A `break_started` attention boundary on the bound node produces a proactive
   candidate; the runtime marks it eligible (policy enabled, `chat` mode, valid
   output lease, node online with `text.display`, `focus=unfocused`, user present
   and available, initiative ≥ 0.65) and runs it to completion.
2. The plugin's poller polls `/v1/companions/{id}/proactivity/output` with a
   persisted event cursor.
3. Each fresh `companion.proactive_completed` item is scheduled via
   `ctx.schedule.create(sessionId, { title, prompt, after_seconds })`.
4. The reminder fires and the message is injected into the session as a
   `{source: {kind: 'schedule'}}` user turn.

Observed delivery: message `6185e0bc-eaa2-4b46-b7df-abe089aa8952`, prompt
`虚拟伙伴主动想说话：那我先安静陪着你休息，等你想说话时再聊。`, scheduled
`06:58:54.445Z` and delivered `06:58:54.457Z`.

## Gotchas (why these are wired the way they are)

- **`after_seconds` must be a positive integer.** `schedule.create` throws
  `invalid_rule` for `after_seconds <= 0`, which stalls the cursor. The plugin
  clamps to `Math.max(1, ...)`.
- **The output lease must stay valid.** The runtime only authorizes output
  polling while the bound node holds a live lease; the plugin renews it before
  each poll (`keepNodeAlive`).
- **The node and its web access must stay online.** The runtime considers a node
  offline after its heartbeat TTL and an access after its access TTL; with
  `keepNodeAlive` the plugin heartbeats both on every poller tick.
- **The poll cursor must be persisted.** A fresh subscription (no
  `after_sequence`) establishes a cursor without replaying older remarks; the
  plugin keeps a per-`companion|node|access` cursor in module state.
- **Eligibility gates.** A candidate is only queued when policy is enabled, the
  companion is in `chat` mode, the personality `initiative` trait ≥ 0.65, focus
  is `unfocused` (not `focused`/`suspected_stuck`), the user is present and not
  busy, and the device is not in a meeting/sharing. During the demo these were
  hit one by one: `waiting_for_user` (mode ≠ chat), `focused`, then
  `initiative_below_threshold` — each is the runtime correctly refusing to
  interrupt.
