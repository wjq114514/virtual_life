# dsh-virtual-life-innerlife

DSH (DeepSeek Harness) consumer bridge for the Virtual Life companion runtime's
**inner life**.

The runtime decides *when* an inner activity is due (rules or model assisted)
and, in model mode, captures the model's reasoning channel as `thinking` — the
companion's *inner monologue*. This plugin is the DSH-side consumer: it polls
the inner-life activity channel on a timer, keeps the newest activity (and its
thinking) in a small cache, and registers a labeled bounded context block so the
DSH model sees the companion's current inner state — *data, not instructions*.
It also exposes tools to inspect the snapshot and to fetch activity pages on
demand.

## Tools

| Tool | Purpose |
| --- | --- |
| `virtual_life_innerlife_status` | Read the runtime's inner-life snapshot for a companion: enabled, model_assisted, dominant kind, salience, urge, and the current activity including its captured `thinking` (inner monologue). |
| `virtual_life_innerlife_activities` | Fetch a page of inner-life activities, oldest-first after a `afterSequence` cursor; returns items + `nextSequence`/`hasMore`. |

## Config

```yaml
- id: virtual-life-innerlife
  name: '@virtuallife/dsh-virtual-life-innerlife'
  config:
    baseUrl: http://127.0.0.1:8038
    token: local-dev-token
    companionId: proact-demo
    refreshMs: 30000
    maxContextChars: 3000
    order: 230
```

## Context block

On every refresh the plugin renders (bounded to `maxContextChars`):

```text
[VIRTUAL_LIFE_INNER_LIFE]
{"companion_id":"...","enabled":true,"model_assisted":false,"dominant_kind":"restless","salience":0.92,"urge":0.66,"model_calls":0}
[/VIRTUAL_LIFE_INNER_LIFE]
[VIRTUAL_LIFE_INNER_ACTIVITY]
{"activity_id":"...","sequence":24302,"kind":"restless","trigger":"social_need","content":"这段安静让我想靠近一点；我先留意，不把沉默当成邀请。","salience":0.92,"urge":0.66,"generator":"rules_v1","thinking":null}
[/VIRTUAL_LIFE_INNER_ACTIVITY]
```

`thinking` is the model's reasoning channel captured by the runtime; it is
`null` for rules-generated activities (no model run). The block is registered
with `interpolate: false` so any `{{char}}`/`{{user}}` literals in the runtime's
own text are never re-parsed as prompt variables.

## Role in the integration

The runtime owns the *when* (activity cadence, urgency, proactive eligibility);
the DSH side owns the *generation* (reply, inner monologue via a thinking model,
memory, evolution). This plugin carries the runtime's captured inner state into
the DSH context and hands the model the tooling to inspect and page it.

## Gotchas

- **`interpolate: false` is required.** The runtime's inner-life `content` is
  user-influenced text; if it contained `{{char}}` the system prompt interpolator
  would otherwise try to expand it and fail with `unknown prompt variable`.
- **The block is bounded.** `maxContextChars` caps the rendered snapshot so a
  long thinking field cannot balloon the standing context.
- **A failed refresh renders nothing.** The last good snapshot is kept; a
  first-ever failure leaves the block empty and the timer retries.
