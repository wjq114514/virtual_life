# Node protocol v1.0

All identifiers are nonempty strings (max 128 characters). All timestamps are UTC ISO 8601 strings. JSON uses snake_case. The runtime is a single-owner prototype. Administrative REST requires `Authorization: Bearer <configured token>`. The WebUI may remember that token in a server-signed HttpOnly session cookie instead: POST `/v1/session` with `{token}` issues one for `runtime.webui_session_days` (0 refuses to remember, 503 `session_disabled`), and DELETE `/v1/session` clears it. Those two endpoints are the only unauthenticated `/v1` routes; every other `/v1/*` route falls back to the cookie only when the request presents no bearer credential at all, so a wrong bearer credential is still rejected and never downgraded to a remembered session. The cookie is an HMAC over an expiry timestamp, holds no token, is HttpOnly and SameSite=Strict, and rotating the API token invalidates every session without any revocation table. Linux deployments issue separate, revocable, write-only reporter credentials as described below. Default local token is `local-dev-token`; bind to loopback for development.

## Linux distribution and reporter credentials

Administrative deployment endpoints are GET `/v1/deployments/linux/bundle`, POST `/v1/deployments/linux/host-key`, POST `/v1/deployments/linux/inspect`, POST `/v1/deployments/linux`, GET `/v1/deployments/linux/{command_id}`, and DELETE `/v1/deployments/linux/{command_id}/credential`. Deployment receipts persist separately from runtime commands; replay never reinstalls, and startup marks unfinished jobs interrupted. SSH secrets remain only in request/worker memory. Host-key SHA256 pinning precedes authentication. See [Linux deployment](linux-deployment.md) and OpenAPI for request and receipt fields.

A `vl_reporter_` credential is bound to one node and deployment, stored hashed, and cannot use GET, WebSocket, output leases, dialogue, memory, configuration or administrative deployment routes. It may POST its own registration with kind other, exactly its granted capabilities and device.device_id equal to its node_id, its own heartbeat, and observations where both data.subject_node_id and data.observer_node_id match itself. The default grant is `["device.state.report"]`. An installation with strict boolean `attention_enabled=true` additionally grants `attention.observe` and POST to its own `/nodes/{id}/attention`; an old credential never gains this permission implicitly. Existing nodes outside these two safe reporter capability sets, identities or Web-only ownership cannot be taken over. Each credential retains its original grant so old installations can restore their configuration during rollback. Authorization and revocation are rechecked before reconciliation/receipt replay in the same coordinator write transaction; successful heartbeat evidence commits with that action. Deployment completion requires evidence from the newly issued credential, not an older online snapshot. Revoking one deployment does not uninstall its program or revoke other deployment credentials.

The optional Linux collector uses local logind session hints and, only within the same X11 user/display, a finite application category. Unknown or unavailable probes stay unknown; meeting/sharing/focus are not inferred. Its previous collection diagnostics may appear in `context.attention_collection`, with their own sampled_at; heartbeat time does not refresh those diagnostics. See [Linux attention collection](linux-attention-collection.md). It never contributes text.display, creates another node, or enables companion proactivity.

## Nodes and graph

Node registration: `{node_id, label, kind, capabilities, device?}`. A node identifies a device, not a browser page. New device kinds are `desktop`, `android`, or `other`; `web` is retained only for existing historical nodes, and new web-kind registrations fail with 409 web_access_required. Optional device is `{device_id?, mac_addresses:[]}` and requires at least one stable identity. MACs are normalized/deduplicated unicast addresses (max 16). An identity already attached to another node fails with device_already_registered and identifies the reusable node; conflicting identity keys fail with device_identity_conflict. Capabilities are extensible strings; names include `avatar.render`, `text.display`, `audio.output`, `reading.context`, `vision.capture`, `device.state.report`, `device.state.observe`, and `attention.observe`.

Node snapshot adds `created_at`, `last_seen_at`, `online`, `context`, optional device metadata and default-empty accesses. Device metadata is `{device_id, mac_addresses, ip_addresses}`; accesses contains live `{access_id, kind:"web", last_seen_at, expires_at}`. Native heartbeat body is `{context: {...}}` (default empty); registration also refreshes liveness. Context contains device signals provided by the host; a heartbeat alone is not evidence of user attention.

Web uses POST `/nodes/access/web` with `{browser_id, access_id, label?, kind?, bind_node_id?}`, returning `{node, access_id, expires_at, resolution}`. Resolution is bound, browser_id, lan_ip, or created, in that priority order. Only a directly observed, recent, unique RFC1918/ULA address may match devices automatically; public/loopback/shared proxy addresses are not identity. Forwarded/X-Forwarded-For requests disable address inference. Browser bindings persist across sessions; ambiguous address matches fail with device_ambiguous rather than silently merge. MAC comes from a native host, never a browser network scan. See [device identity](device-identity.md) for fallback and legacy behavior.

Only one Web access is live per device; a competing page fails with device_access_busy. POST `/nodes/{node_id}/access/{access_id}/heartbeat` accepts `{context}` and refreshes that access for node_ttl_seconds; expired/released/mismatched access fails with node_access_expired. DELETE `/nodes/{node_id}/access/{access_id}` idempotently closes only that access. Reopening requires a new access_id. A valid repeated open refreshes TTL without adding a device/session/event. Web access contributes text.display only while live and never overwrites native capabilities/context. If only Web remains online, expired native capabilities are excluded. Read projections do not mutate storage, and access changes preserve migration reconciliation gates.

Native terminal renderers attach a separate presentation session: POST `/nodes/{node_id}/presentations` with `{presentation_id, kind:"terminal"}` returns `{node_id, presentation_id, kind, expires_at, ttl_seconds, server_time, opened_sequence}`. `opened_sequence` is the event head at attach time. POST `/nodes/{node_id}/presentations/{presentation_id}/heartbeat` refreshes only a live session; expired or released sessions cannot revive. DELETE releases that session idempotently. Sessions are stored in `native_presentations`, separate from Web accesses. At most one terminal presentation is live per node and a newer attach supersedes an older one. Web and native entry points are mutually exclusive in the same write transaction: opening either while the other is live on the node fails with 409 `presentation_busy`, so an old page or renderer cannot silently keep output authority. A presentation TTL never refreshes the node heartbeat and never grants owner, epoch or lease. Old 409 `device_access_busy` only covers a second Web page.

The lightweight device client uses `context.device_state` for timestamped state with subject and observer identity. Gateway reports register a separate node per configured target. `online` then means the reporting path is fresh, while endpoint reachability remains in that state envelope. This optional profile is documented in [device state tracking](device-state-tracking.md); the current server stores it as bounded JSON without typed normalization or session fencing. Heartbeats replace the full context and do not emit change events.

Graph returns `{nodes, edges}`. An edge is `{source_node_id, target_node_id, available, reason}`. The initial routing policy connects registered nodes owned by this runtime. A destination must be online and provide at least one output capability. Edges are computed from policy and capabilities; this boundary can later implement explicit authorization and topology.

## Companion state

Create: `{companion_id, name}`.

Snapshot: `{companion_id, name, revision, active_node_id, epoch, lease_expires_at, activity, interaction_mode, context, memory_refs, created_at, updated_at}`.

Defaults: revision=0, epoch=0, active_node_id=null, lease_expires_at=null, activity=`{type: idle}`, interaction_mode=`quiet`, context={}, memory_refs=[].

Context update: `{command_id, node_id, epoch, expected_revision, activity?, interaction_mode?, context?, memory_refs?}`. Mode is `quiet`, `chat`, `focus`, or `away`. Only the current node with a valid output lease and epoch can update. Updates increment revision. Context objects are merged at their top level. A prepared migration freezes companion context until committed or aborted.

## Travel between nodes

Prepare body: `{command_id, target_node_id, expected_revision, reason}` (`reason` defaults to `manual`). Only one prepared transfer per companion is permitted.

Transfer: `{transfer_id, companion_id, source_node_id, target_node_id, status, source_released, target_ready, base_revision, snapshot, reason, created_at, expires_at, committed_revision}`. Preparing travel to the current node fails with `same_node`; use lease renewal to resume at the same node.

Statuses: `prepared`, `committed`, `aborted`, `expired`. Preparing retains the source as active and stores the migration snapshot. The source output lease cannot be renewed during migration. Preparing publishes `transfer.prepared` to each relevant node with payload `{transfer, role}` where role is `source` or `target`.

Acknowledgment: `{node_id, phase, presentation_id?}` where phase is `ready` (target only) or `released` (source only). A native target that reports ready may bind a live presentation via optional `presentation_id`; the ready must come from an actually initialized renderer. `presentation_id` is rejected with phase=released and rejected with 422. Commit re-validates the bound presentation, so releasing or expiring it aborts the transfer with reason `target_presentation_invalid`. Clients that omit the field keep the legacy acknowledgment semantics. The host must actually stop its output before sending released, and initialize its renderer before ready. Commit requires target_ready and either source_released, no source, or expiry of the source lease. Recheck target liveness and the context revision at commit. Both acknowledgment orders must work. Released revokes the source's stored lease; if travel aborts, that source must acquire a fresh lease before resuming. A destination reconnecting after its heartbeat expired cannot reuse its earlier ready acknowledgment.

Commit increments companion epoch and revision once, changes active_node_id, issues a fresh lease, preserves activity/context/memory_refs, and publishes `companion.transferred` with payload `{transfer, companion}`. Terminal acknowledgments are idempotent. Abort or expiry leaves the source state in place and allows lease renewal again. Background reconciliation attempts pending commits and marks timed-out requests expired.

Lease renewal: `{node_id, epoch}`. Response: `{companion_id, node_id, epoch, expires_at, ttl_seconds, server_time}`. Source output must stop at lease expiration. The SDK gates output on node ownership, epoch, and a conservative monotonic deadline. Client clock uncertainty must fail closed. No migration can commit while an unreleased source lease remains valid.

Room follow: `POST /v1/companions/{companion_id}/follow` with `{command_id, target_node_id, expected_revision, reason?}`. This is a single-party authoritative migration: no target handshake. The lifeform flows to the room that just reported activity. The abandoned room becomes `room_state: "empty"` but keeps its capabilities and tools. Unlike a prepared transfer, `follow_room` bumps epoch and revision once, issues a fresh lease, and publishes `companion.room_followed` (`{companion, from_node_id, to_node_id, reason}`); following the room the companion is already in only refreshes the lease and emits `companion.room_visited`. A prepared two-party transfer wins: while one is pending, `follow_room` returns 409 `transfer_in_progress`. Auto-follow is enabled by `runtime.auto_follow_rooms` (default true) with `runtime.auto_follow_quiet_seconds` (default 15) suppressing repeat moves to the same room within the quiet window. User-initiated observations and attention samples with `user_present` are both treated as room activity.

## Idempotency and events

command_id is required for context update, transfer preparation, observations, memory writes, personality/profile-state writes, and dialogue. A replay of the same command returns its original result; reuse with another payload fails with `idempotency_conflict`. State version mismatches return 409. Validation failures are 422; missing resources 404; ownership/lease errors 409. Node-runtime, memory, personality, and dialogue commands have separate receipt tables. Personality IDs are unique across its operations and companions; dialogue IDs are unique across companions in the dialogue table.

Observation input: `{command_id, modality, data, user_initiated}`. Modality is an extensible string such as `reading`, `screen`, `vision`, `device_state`, or `operation`. This interface stores an event and metadata; real visual recognition is plugged in later. Vision requires the declared capability and user_initiated=true. Device clients submit observations at their observer node, with the observed target in `data.subject_node_id`. State and operation signals do not grant output ownership or automatically become long-term memories.

Event: `{event_id, sequence, protocol_version, type, companion_id, node_id, recipient_node_id, payload, occurred_at}`. Sequence is a durable monotonic cursor. Null recipient means broadcast. Event types include node.registered, node.observation, node.web_access_opened, node.web_access_released, node.auth_failed, node.disconnected, companion.created, companion.context_updated, companion.personality_updated, companion.personality_state_updated, companion.personality_evolution_proposed, companion.personality_evolution_accepted, companion.personality_evolution_rejected, companion.emotion_updated, companion.emotion_corrected, companion.inner_activity, companion.dialogue_completed, companion.dialogue_silent, companion.dialogue_discarded, companion.room_followed, companion.room_visited, transfer.prepared, companion.transferred, transfer.aborted, transfer.expired.

Canonical payloads: companion.created and companion.context_updated use `{companion}`; transfer.aborted and transfer.expired use `{transfer}`; transfer.prepared uses `{transfer, role}`; companion.transferred uses `{transfer, companion}`; companion.room_followed uses `{companion, from_node_id, to_node_id, reason}`; companion.room_visited uses `{companion, node_id}`. These are state notifications, not new output authorization. Nodes re-fetch current state and validate a fresh lease before resuming output after replay.

Event page: `{events, next_sequence}`. `next_sequence` is the last scanned event, including events filtered out for a specific node. Limit bounds the scanned rows; clients keep fetching while the cursor advances.

WebSocket `/v1/nodes/{node_id}/events`: first frame within 5 seconds must be `{type: auth, token, after_sequence: 0}`. After validation the server sends `{type: welcome, protocol_version: 1.0, node_id}` then frames `{type: event, event}` and `{type: cursor, sequence}`. Client ping `{type: ping}` receives `{type: pong}`. Credentials stay out of URLs. Reconnect with the last cursor; acknowledgments are safe to replay. Refusals close 4401 on bad auth, 4400 on an out-of-range cursor, 4404 on an unknown node, and are audited durably as `node.auth_failed` with `{code, stage: auth}`; every stream end is recorded as `node.disconnected` with `{}`, and Web access open/release as `node.web_access_opened` / `node.web_access_released` with the access id, so the event trail is the single source for connection diagnostics.

## Long-term memory and cost

Write: `{command_id, kind, content, source_event_id, source_node_id?, semantic_key?, tags?, importance?, confidence?, confirmed?, pinned?, retention_policy?, occurred_at?, episode_key?, valid_from?, valid_to?}`. Kinds: `episode`, `fact`, `preference`, `relationship`. importance and confidence are 0..1. Explicit saving remains available alongside automatic completed-dialogue consolidation. The source event must exist and belong to this companion; a supplied source_node_id must match its node. Unscoped node.observation events are not accepted; use companion-scoped evidence such as companion.context_updated or completed dialogue events. Long-term records have no automatic age deletion.

Memory records additionally include memory_id, companion_id, source_event_ids, active, revision (initially 1), supersedes_id, created_at and updated_at. occurred_at and validity timestamps are nullable RFC3339 values normalized to UTC; migration leaves unknown times null, and source event ingestion time is not assumed to be occurrence time. valid_from is inclusive and valid_to exclusive. Recall accepts only active, currently valid records. Episodes deduplicate by episode_key or, when absent, source_event_id and reject semantic_key; identical text from different events remains separate. Duplicate writes preserve omitted times; explicitly conflicting times fail, except an unknown episode occurrence time can be filled. Stable knowledge can merge normalized identical content and provenance; different content for an active semantic_key fails with 409 `memory_correction_required`. Explicitly associating duplicate active content with a different semantic_key fails with `semantic_key_conflict`.

POST `/v1/companions/{companion_id}/memories/{memory_id}/corrections`: `{command_id, expected_revision, content, source_event_id, source_node_id?, valid_from?, valid_to?, tags?, importance?, confidence?, confirmed?, pinned?, retention_policy?}`. Corrections apply to stable memories. Compare-and-swap checks the current revision, creates a replacement at revision 1 with supersedes_id pointing to the previous record, and retains the previous record as inactive with its revision incremented. Omitted valid_from defaults to now; omitted valid_to and metadata inherit existing values. The correction lower bound cannot precede the old lower bound or exceed now, and must precede its upper bound. PATCH `/v1/companions/{companion_id}/memories/{memory_id}`: `{command_id, expected_revision, tags?, importance?, confidence?, confirmed?, pinned?, retention_policy?}` requires at least one metadata field and edits it in place, incrementing revision. It can clear tags, lower confidence and revoke confirmation or pinning. Explicit null for metadata and validity fields is rejected. Clearing validity boundaries is not supported in this phase. Both return MemoryRecord and use durable idempotent receipts; revision conflicts return 409.

Memory list returns `{items, total, limit, offset}` and includes inactive audit records. DELETE removes the selected record, its retrieval index and provenance, and saves source-event tombstones to prevent future automatic re-extraction. Original command response snapshots, runtime events and automatic consolidation evidence remain for audit; replaying a deleted record's original command returns its historical result without recreating the record. Existing independent records are retained. This endpoint is not a full purge of historical content.

DELETE `/v1/companions/{companion_id}/memory?command_id=...` is the complete-forgetting protocol. In one transaction it clears the companion's memory records, provenance, term index, access state, usage receipts, automatic-memory queue/jobs/owned/evidence/derivatives/entities, embeddings, query cache, token ledger and memory command log, and writes source-event tombstones so a replay of an old dialogue event cannot re-consolidate already-forgotten content. The companion row, dialogue commands, events, personality, emotion, inner life, proactivity and device state belong to other owners and are not touched. The receipt stores no content; a replayed `command_id` is idempotent and cannot resurrect anything.

MemoryRecord has layer=`episodic` for episode and `semantic` for fact/preference/relationship. Recall: `{query, semantic_key?, layer?, token_budget: 800, max_items: 8}`. The optional layer restricts candidates before candidate limits; omission queries both. Episodic recall can include summaries, semantic recall can include graph relations. Response: `{items, summaries, relations, context_text, estimated_tokens, token_budget, truncated, retrieval_strategy, model_calls, retrieval, rerank?, embedding_usage?}`; semantic recall always includes a `rerank` diagnostic object (`enabled=false` when disabled), while ordinary recall reports `null`. Summaries contain `{summary_id, content, memory_versions, source_event_ids, created_at, job_id, kind: summary}`; relations contain `{relation_id, subject, predicate, object, confidence, memory_versions, source_event_ids, created_at, job_id, kind: relation}`. These are derived evidence views, not new MemoryKind values. The token estimator is explicitly approximate and pluggable. Budget applies to the formatted context_text; items, summaries and relations jointly correspond to that context and all count toward max_items. Local retrieval combines lexical matches, effective importance after bounded exponential decay, pinned status, summaries and bounded graph neighbors. At most one summary and one relation initially try up to 60% of the memory budget, then source records fill the remainder. Never include an item if it exceeds the remaining budget. Recall model_calls is 0.

Policy/stats/consolidation planning expose recall budgets, storage counts and a conservative batch/call policy. Default recall is 800 estimated tokens / 8 items, with limits of 4096 / 50. Default model quota is 10000 input+output tokens per UTC day across all companions in the runtime database, and 5000 per task. Dialogue, automatic consolidation, and non-local embedding indexing/query embeddings reserve and settle work through the same model-token ledger; `embedding.local=true` records usage in job receipts without charging the paid daily ledger. The legacy consolidation-plan remains a local plan for explicit episodes, separate from automatic dialogue jobs. See [memory design](memory.md) for accounting and extension boundaries.

## Automatic memory

With VIRTUAL_LIFE_AUTO_MEMORY_ENABLED=true (default), completed dialogue events and unique source-event queue entries commit in the same transaction. Silent/discarded replies and node observations are excluded. No automatic backfill of pre-upgrade history occurs. Default batch size is 4 turns, maximum oldest-event wait 60 seconds, poll interval 2 seconds and output limit 1200 tokens. Background consolidation and dialogue share the runtime token ledger. The foreground response's usage and model_calls describe only that reply.

The worker saves raw dialogue episodes, reserves a bounded extraction call, executes outside transactions, settles known usage, validates exact user evidence and rechecks source versions, then commits extracted facts/preferences/relationships, summary, graph relations and the terminal job receipt atomically. Assistant statements alone cannot support user facts. Automatic records start unconfirmed and unpinned, confidence at most 0.85. Automatic replacement is limited to owned, unchanged, unprotected records with newer evidence; conflicts with protected records are retained as needs_review and that batch's summary is withheld. Summaries and graph edges record source memory IDs/revisions; invalid dependencies are excluded before candidate limits. Automatically extracted stable records require at least one current valid evidence episode, including during final reference validation; independent manual records retain their own authority.

Routes under `/v1/companions/{companion_id}/memories`:

| Method / suffix | Contract |
| --- | --- |
| GET `/automation` | `{companion_id, enabled, provider_configured, batch_size, max_wait_seconds, output_token_limit, queue_states, job_states, token_quota}` |
| GET `/automation/jobs` | `{companion_id, items, total, limit, offset}`; jobs expose job_id/status/error_code/usage/source_event_ids/source_memory_versions and optional memory_versions/needs_review |
| POST `/automation/run` | Body `{force: true}`; processes one eligible pending batch, returns job result or `{status: idle}`; does not retry running/unknown/failed sources |
| GET `/summaries` | `{companion_id, items, total, limit, offset}`; effective summaries only |
| GET `/knowledge-graph` | `{companion_id, entities, relations, limit}`; entities are `{entity_id,name}` derived from live relation endpoints |

List limit is 1..200 (default 50), offset is nonnegative. Graph accepts query up to 2048 characters and limit 1..200. Jobs use completed/needs_review/local_only/deferred/failed/discarded/unknown terminal states; pending/running describe the queue/execution phase. Day-quota failures defer sources until the next UTC day. Oversized batches split by complete events; a single oversized event fails with source_exceeds_task_budget while retaining its raw episode. Known invalid extraction usage is settled; unknown execution keeps the reservation and never automatically resends the source.

The POSIX worker holds a cross-process file lock through dispatch and receipt. After a crash releases that lock, the next worker marks leftover running jobs unknown and permits later pending sources to progress. Non-POSIX execution returns 503 automatic_memory_worker_unsupported; disabling automation keeps explicit memory and inspection available. Disabled automation stops new enqueue and execution. Detailed schemas and configuration are in [automatic memory](automatic-memory.md).

## Semantic retrieval and canonical memory graph

Unified `[embedding]` configuration optionally enables a separate compatible embeddings provider; disabled by default. Existing and newly valid memories are indexed by memory ID, revision, content hash and vector space. Space includes endpoint, model, requested dimensions, extra parameters and an explicit version identity, excluding credentials. Known usage is settled even when vectors are rejected; unknown dispatch retains its reservation and is not automatically redispatched for the same version. Indexing and queries use POSIX execution locks and durable job receipts. SQLite stores normalized vectors and a bounded, expiring query cache; cosine scanning is limited to configured eligible candidates (default 5000), without ANN.

Ordinary recall and personality preview perform no external calls and never rerank. A cached query vector can participate in RRF fusion with local lexical/importance and graph candidates. POST `/v1/companions/{id}/memories/semantic-recall` uses MemoryRecall and returns RecallResult with `model_calls` 0 or 1, nullable `embedding_usage:{input_tokens,output_tokens}`, `retrieval:{semantic_cache_hit,semantic_candidates,vector_space}`, and optional `rerank:{enabled,applied,candidates,latency_ms,error_code}`. Reranking is disabled by default, applies only to the fused candidate head of this explicit endpoint, and fails open to the original order on timeout, transport error or a short circuit breaker; the interactive dialogue path does not rerank. GET `/memories/embeddings` reports enabled/configured/local, stored_vectors/indexed_records/pending_records/skipped_oversized_records, jobs, token_quota and rerank status. GET `/memories/embeddings/jobs` supports limit 1..200 and nonnegative offset. POST `/memories/embeddings/run` takes `{limit?:1..64}` and processes one batch. Endpoints are companion-scoped; model keys and original query text are not returned.

The memory graph canonicalizes names with NFKC, casefold and whitespace normalization. Relations aggregate independent currently valid source derivatives by canonical subject/predicate/object into stable edge IDs. Graph responses preserve original relation_ids and add edge_id, subject_entity_id, object_entity_id, sources, evidence_count, evidence_truncated and entity_versions. Each edge returns at most 64 source entries and bounded references; truncated evidence does not count as new knowledge. Source revisions, validity, companion scope and tombstones are filtered before candidate limits. Traversal only returns existing supported edges and does not assert inferred facts.

GET `/memories/knowledge-graph` accepts query (<=2048), limit (1..200), optional entity_id and hops (1..3, constrained by configured max_hops, default 2). Response adds hops, and entities contain `{entity_id,name,aliases,revision,updated_at,source_event_ids}`. Search first selects related live candidates and then expands endpoints under a cumulative candidate cap. Explicit entity lookup can return metadata without live relations. PUT `/memories/knowledge-graph/entities/{entity_id}/aliases` takes `{command_id,expected_revision,aliases,source_event_ids?:[]}` and replaces aliases atomically with an idempotent receipt. Strings are trimmed and 1..128 characters; arrays have at most 64 entries, and revision is 1..2^63-2. Aliases must be unique within a companion and cannot silently merge existing canonical entities. Optional source events must belong to the companion.

Actual dialogue can prepare a query vector after local permission and participation checks when embedding.dialogue_recall is enabled. Query failure falls back to local retrieval. Original DialogueResult model_calls/usage still describe text generation, while nullable memory_retrieval separately records query model_calls/cache_hit/usage and optional degraded/error_code. The final prompt records graph entity_versions as well as memory_versions; both are revalidated before text dispatch and before output commit, so changing aliases invalidates obsolete graph interpretation. Details: [vector memory and graph](vector-memory-and-graph.md).

## Personality and short-term state

Personality is owned by `companion_id` on the main server and shared across nodes. A definition contains description, personality text, values, boundaries, traits, speech_style, scenario, first_message, example_dialogue, and alternate_greetings. Traits warmth/curiosity/assertiveness/playfulness/initiative are finite 0..1 configuration values. Speech length is brief/balanced/detailed. Native inputs reject unknown fields and have the length and bounded-JSON limits in [the personality contract](personality.md).

Profile snapshot: `{companion_id, revision, configured, definition, source_card, import_warnings, created_at, updated_at}`. GET of an unconfigured profile returns default definition, revision=0, configured=false, null source/timestamps, without a write. POST replacement: `{command_id, expected_revision, definition}`. First explicit write is revision=1. Profile and state revisions are independent of the companion activity revision and epoch. Writes use owner Bearer authentication without requiring an output lease. State, receipt and broadcast event commit atomically.

V2 JSON import: `{command_id, expected_revision, card}` with spec `chara_card_v2`, spec_version `2.0`, and the six base strings in data. It maps description/personality/scenario/first_mes/mes_example/alternate_greetings and archives the complete original JSON, including unknown extensions. Existing companion name and identity remain authoritative. The import is a subset: prompt overrides, lorebook, scripts and extensions are never activated. Native edits retain the source archive, which is not an export of the latest definition. Warnings are card_name_differs, system_prompt_ignored, post_history_instructions_ignored, character_book_ignored, extensions_ignored.

State input: `{command_id, expected_revision, state, ttl_seconds: 1800, source_event_id?}`. State is `{mood, energy, social_battery}`; mood is calm/cheerful/curious/concerned/tired, and numeric values are finite 0..1. TTL is 30..86400 seconds. Defaults are calm/0.8/0.7. A supplied source event must exist and must not belong to another companion; events without a companion are permitted sources. Cross-companion sources fail with 409 source_event_conflict.

State snapshot: `{companion_id, revision, state, recorded_state, expired, source_event_id, expires_at, updated_at}`. Unconfigured state returns revision=0, recorded_state=null, expired=false and null source/timestamps. After expiry, effective state returns defaults while recorded_state, source and revision remain. Reads and expiry emit no writes or events. Replaying an old command returns its original receipt, including its historical state and expiry; GET again for current effective state.

Broadcast notifications use `companion.personality_updated` with `{personality_revision}` and `companion.personality_state_updated` with `{state_revision, expires_at}`. The outer event carries companion_id. Clients re-fetch current profile/state rather than treating replayed notifications as authoritative current snapshots.

`GET /v1/companions/{companion_id}/personality/history` returns a read-only, revision-ordered definition timeline with `before_revision`/`limit` paging; databases written before this feature synthesize one `pre_migration` entry. `GET/POST .../personality/proposals[/{proposal_id}][/refresh|/accept|/reject]` implement owner-gated evolution: `refresh` is idempotent and generates bounded proposals from local evidence only (`model_calls: 0`; `use_model=true` returns 422 in this phase), `accept`/`reject` are CAS on both the profile and proposal revision and return the proposal with the current profile. Replaces are lazy: reads mark proposals `expired` or `superseded`. Evidence reuses the same `memory_versions` and correction-revision discipline as resonance; invalid evidence returns 409 `evidence_invalidated`. Events are `companion.personality_evolution_proposed|accepted|rejected`; acceptance also emits `companion.personality_updated`. Contracts and thresholds: [personality evolution plan](personality-evolution-plan.md).

## Budgeted personality preview

POST preview: `{user_message, user_name: "你", user_initiated: true, audio_requested: false, history: [], token_budget: 3072, memory_token_budget: 800, max_memory_items: 8}`. History has at most 20 user/assistant entries, each `{role, content}`; current and historical messages are trimmed and must hold 1 to 4096 characters each, user_name at most 128, and the complete request is bounded JSON. Only literal `{{char}}`/`{{user}}` substitutions are performed, in one pass. Unknown macros stay text.

Prompt order is identity/personality, mode/effective state, optional activity/context facts, optional memory, optional style examples, recent contiguous history, current message. Data and style blocks use JSON escaping and explicit labels. Core/state/current message must fit intact or fail with 422. Default personality policy caps the core at 1200 and complete prompt at 4096 estimated tokens. Recall uses at most the first 2048 characters of the current message as query; the full message is retained in the prompt. Both the formatted memory block and the complete final context include headings and separators in their budgets. Raw source cards never enter the prompt.

Response: `{companion_id, personality_revision, state_revision, companion_revision, epoch, active_node_id, context_text, estimated_tokens, token_budget, truncated, omitted_sections, memory_refs, memory_versions, participation, model_calls: 0}`. Memory refs are the deduplicated union of included records and records supporting included summaries/relations. Their full source text need not appear in MEMORY_DATA. memory_versions contains `{memory_id, revision}` for that union; MEMORY_DATA includes record version/time metadata and derived provenance. Dialogue revalidates these actual references before provider dispatch and before committing output, including active status and validity times. Unrelated new records do not invalidate a reply. omitted_sections values are activity_context/memories/style_examples/history. truncated describes omitted supplied or recalled prompt material, not whether the full memory database was recalled. Preview performs local recall, without a model reservation, provider call or memory write.

Participation: `{respond, allow_audio, presence, max_reply_sentences, reason}`. The server computes output conditions from current active node, epoch, online/output capabilities and unexpired lease. Preview does not extend leases. Without output conditions, or in away, respond=false. Explicit interaction can respond; unsolicited quiet/focus stays silent. Unsolicited chat requires initiative>=0.65, energy>=0.35 and social_battery>=0.25. Audio additionally requires user_initiated, audio_requested and active node audio.output.

Presence values are none/quiet/attentive/curious/concerned. Reasons are output_not_permitted/companion_away/user_requested/waiting_for_user/initiative_below_threshold/energy_below_threshold/social_battery_below_threshold/chat_participation. Reply sentence limits are 2/4/8 for brief/balanced/detailed, at most 2 in focus, and 0 when silent. These are review suggestions, not output authorization. The dialogue runner rechecks versions, epoch, current node and fresh lease before returning new output.

## User-initiated text dialogue

POST `/v1/companions/{companion_id}/dialogue` accepts the personality-preview fields plus `{command_id, node_id, epoch, expected_revision, max_output_tokens: 512}`. Only user_initiated=true and audio_requested=false are accepted. Output limit is 1..1024; history, message lengths, prompt budgets and complete request bounds follow preview. Before generation the server requires the current owner, matching epoch and companion revision, an online text.display node, an unexpired lease, and no pending transfer. No configured provider returns 503 `model_not_configured`.

Response: `{command_id, companion_id, status, assistant_message, source_event_id, preview, model, usage, estimated_input_tokens, model_calls, created_at, scene?}`. Status is completed/silent/discarded. usage is `{input_tokens, output_tokens}` or null. Silent performs zero calls. Discarded accounts executed usage but omits assistant_message and source_event_id. After generation a single write transaction rechecks owner, epoch, lease, companion revision, node availability, pending transfer, personality revision, effective state and actual memory references, then saves the terminal receipt and event atomically. Profile/state changes, state expiry, or changed/deleted/expired referenced memories discard output. Clients still recheck fresh authority before rendering.

`scene` is optional diagnostic metadata from the local scene classifier and is absent or null when the classifier is off. It contains `{mode, label, confidence, scores, reason, threshold, routed_model, classifier_version, backend, latency_ms, error_code}`. `mode` is `observe` or `route`; `route` may set `routed_model` to the model selected for this turn. `observe` never changes `model`, prompt content, streaming drafts or final output. Classifier failure, timeout, missing optional dependencies or an unavailable artifact yields `label=null` and an `error_code` while dialogue proceeds normally. The metadata never grants output authority and is stored in the durable receipt; it contains no raw user text.

GET `/v1/companions/{companion_id}/dialogue/{command_id}` reads the historical receipt without executing the provider. A pending command returns 409 `dialogue_in_progress`; a missing command returns 404. Same-ID POST returns its terminal result or stored error; changed requests fail with `idempotency_conflict`. Commands are durably claimed before execution. Network calls run outside transactions; failures remain failed, and crash-left pending commands are never automatically retried. Historical receipts and events do not grant new output rights.

## Externally generated dialogue

`generator: "internal"` is the default and keeps the local provider path above. `generator: "external"` parks the turn at the dispatch gate instead of calling a model: the response carries `status: "awaiting_generation"` with the fully assembled prompt in `preview.context_text`, the same `estimated_input_tokens`, `preview.memory_refs`/`memory_versions`, `preview.participation` and `emotion` the provider would have received, and no `assistant_message`. The model-token reservation stays held and the command row is `awaiting_generation`; GET returns that receipt until it is committed.

External generation never touches the local provider. When no provider is configured (`model_base_url`/`model_name` empty), `external` is still allowed — VL degrades to a pure data plane and returns `awaiting_generation` — while `internal` returns 503 `model_not_configured` (VL does not fabricate a reply).

The proactive output channel (`/proactivity/output` and `/proactivity/native-output`) reflects the DSH-owned path: a parked proactive turn surfaces as an item with `status: "awaiting_generation"`, `assistant_message: null`, `reason_text` (the urge, truncated to 512 chars), `context_text` (the assembled prompt), and `command_id`/`candidate_id`. The consumer generates the reply and commits it via `POST /dialogue/{command_id}/commit`; the runtime resolves the candidate to `completed` through the `proactive_finalized` callback. Each candidate is exposed once (deduplicated by `candidate_id`), so the same urge never replays.

POST `/v1/companions/{companion_id}/dialogue/{command_id}/commit` accepts `{assistant_message, model?, usage?: {input_tokens, output_tokens}, tool_trace?: [{...}]}` and finishes the parked turn. It rechecks owner, epoch, lease, node availability, pending transfer, personality revision, effective state and actual memory references exactly as the local path does, then writes the same receipt, `companion.dialogue_completed` event, emotion appraisal, automatic-memory queue entry and memory log. `tool_trace` is recorded for audit only and never gates output. Speaker-label normalisation applies to the committed text.

Commit is idempotent by `command_id`: a committed or failed command replays its stored receipt or error instead of writing a second turn. Committing a command that is not parked returns its existing terminal result; a missing command returns 404. `usage` settles the reservation; omitting it releases the reservation because no local model was billed. A parked turn expires after 300 seconds, releasing the reservation and settling the command as `discarded` with `dialogue_commit_expired`; the retry then replays that receipt. Context or reference changes between park and commit discard the turn with `dialogue_context_changed`.

Input estimates include provider instructions and message-wrapper overhead. Reserve estimated input plus maximum output first; recheck authority before dispatch, then settle valid actual usage even if text parsing fails or output is discarded. Missing usage and ambiguous transport failures retain the reservation. Only known failures before dispatch release it. Estimates and quotas count tokens, not monetary charges.

`companion.dialogue_completed` payload is `{command_id, user_message, assistant_message, memory_refs, memory_versions, usage}`. Its event ID can be used for an explicit memory write and is queued for automatic memory when enabled. Silent/discarded events contain only `{command_id, status}` and omit assistant text. Dialogue preserves stable personality and legacy manually expiring state; accepted user events update the independent continuous affect layer. With model tools enabled, a turn may execute bounded tools before committing its final reply; it still never starts autonomous turns. Background memory extraction is separate from foreground output authority.

Style examples render as `name: utterance` lines and a model may continue that layout into its own answer. The provider instructions forbid a speaker name or role label, and before the receipt is written a leading label matching the companion's own name is removed, in the explicit colon form only (`随: `, `随： `). A bare leading name, a label naming anyone else, an inner occurrence, or a label with nothing after it are all preserved. Normalisation happens before `assistant_message` is stored, so events and receipt replay expose the same text the client received. `assistant_message` remains an unbounded plain string; this changes no wire field.

The optional CompatibleChatProvider supports streaming HTTP Chat Completions for the streaming route, and non-streaming completion for the original JSON route and background tasks, without redirects or automatic retries. Both paths bound upstream responses and require valid usage. Server settings configure base URL, model, secret, HTTP timeout and bounded extra JSON; execution/budget fields, including stream_options, cannot be overridden. `/v1/capabilities` reports `{dialogue: {configured, model, max_output_tokens, autonomous_dialogue, streaming, provider_streaming}}` without secrets. streaming advertises the SSE endpoint; provider_streaming distinguishes actual incremental generation from a complete-only provider's one-draft fallback. See [WebUI](webui.md) for variables and browser-node behavior. `/ui/` serves static same-origin assets; `/` redirects there. UI routes are excluded from OpenAPI.

### Model tools, proactive scheduling and delegation

Tools are opt-in through `[tools].enabled=true`; disabled requests and receipts are unchanged. When enabled, the model receives a bounded registry of at most 16 available schemas: the built-in `schedule_next_proactive`, `schedule_task`, `list_scheduled_tasks`, `cancel_scheduled_task`, `delegate_task` when delegation is enabled, and trusted `module:factory` plugins configured through `[tools].plugins` or injected at app construction. A turn allows one tool round and `max_calls_per_turn` calls (1..2), then a final provider call without tools. Tool results are untrusted process output and never override personality, boundaries or output authority. Memory extraction, personality evolution and screen vision do not receive tools. The plugin contract is documented in [model tools](tools.md).

`schedule_next_proactive` accepts `{delay_seconds, reason?}` within `[proactive_schedule]` bounds (default 600..86400 seconds). A schedule is durable, replaces any pending schedule for the companion and emits `companion.proactive_scheduled`. When due, it is converted to a normal proactive candidate only if no user dialogue occurred within `min_idle_seconds` (default 3600); it then passes the same attention, personality, budget, lease and cooldown gates as every other candidate. A failed gate suppresses that candidate and keeps the schedule's terminal `fired` state. A new user dialogue command cancels a pending schedule before generation and emits `companion.proactive_schedule_cancelled`. `GET /v1/companions/{id}/proactivity/schedule` returns the current or last terminal schedule.

`schedule_task` accepts `{title, details?, delay_seconds}` and creates a durable planned-task reminder. The delay is 60..2592000 seconds, the title is at most 200 characters and details at most 2000; `command_id:tool_call_id` makes creation idempotent. Multiple tasks may be pending independently of `schedule_next_proactive`. When due, a task becomes a proactive candidate with `origin=task`; it passes the same quiet, attention, personality, budget, lease and cooldown gates. A task with no active node or no quiet delivery window becomes `missed`; a suppressed candidate becomes `suppressed`; otherwise terminal status follows the dialogue receipt. Tasks only remind and never execute external work. `GET /v1/companions/{id}/scheduled-tasks` lists tasks and `DELETE /v1/companions/{id}/scheduled-tasks/{task_id}` cancels a pending or queued task. The list accepts `status=active|pending|queued|completed|silent|suppressed|discarded|failed|cancelled|missed`; `active` means pending or queued. `list_scheduled_tasks` exposes the same status filter with a limit of 1..20, and `cancel_scheduled_task` accepts exactly one of `task_id` or `title`. Titles match case-insensitively and exactly; multiple active matches return `scheduled_task_ambiguous` rather than choosing one. Creation, cancellation and missed delivery emit `companion.scheduled_task_created`, `companion.scheduled_task_cancelled` and `companion.scheduled_task_missed`.

`delegate_task` accepts `{instruction, agent?}` and enqueues work for a separately configured external agent; the model does not wait for it. The default subprocess executor uses `[delegation].command`, an argv array without a shell containing exactly one `{prompt}` placeholder; the instruction is substituted literally. Children inherit only `env_allowlist` names, run in `working_dir`, and are killed on `timeout_seconds` or when output reaches `max_output_chars`. Code embedding may inject another `DelegationExecutor`; queueing, idempotency, terminal receipts and events remain owned by `DelegationService`. A single durable worker claims tasks, so replay never dispatches a task twice. Terminal output is stored in `delegated_tasks` and announced with `companion.delegation_completed`; `GET /v1/companions/{id}/delegations` lists recent tasks.

`GET /v1/companions/{id}/tools?purpose=dialogue|proactive` reports registered descriptors with `name`, `description`, `parameters`, `purposes`, `source` and per-purpose `available`; unavailable plugins remain visible for diagnostics but are omitted from model requests. Tool side effects commit before final text and carry `command_id:tool_call_id` idempotency; they are not rolled back if the final reply is later discarded. A rejected over-limit tool round fails before executing any call. Streaming emits a `tools` status while executing and defers draft deltas until the final provider round.

### Streaming transport

POST `/v1/companions/{companion_id}/dialogue/stream` accepts the identical DialogueRequest and Bearer authentication, and shares command fingerprints, reservations, accepted events and durable receipts with JSON dialogue. HTTP authentication/body failures use ordinary error responses. Once streaming starts, semantic failures use an SSE error event with `{command_id, companion_id, error: {status_code, code, message}}`; HTTP 200 alone does not mean success. Response headers include `Content-Type: text/event-stream`, `Cache-Control: no-store`, and `X-Accel-Buffering: no`.

Frames are UTF-8 `event: <type>\ndata: <JSON>\n\n`; comment pings are sent after 10 idle seconds. All data events carry command_id and companion_id. Types are status (stage preparing/generating/tools/discarded, optional accepted_event_id/discard_reason), snapshot (generation preview and emotion), delta (sequence starting at 1 and cumulative, speaker-normalized text), result (the existing DialogueResult, committed before delivery), and error (above). Replace draft text on each delta; do not append it. Silent and completed-command replay may contain no snapshot or delta. One result or error terminates the subscription; EOF without either is ambiguous. There are no SSE IDs, resume cursor, or automatic generation retries. A duplicate pending command produces dialogue_in_progress, while a completed command replays only result without invoking the provider.

Every nonempty provider chunk revalidates output authority, context versions, and actual memory/entity references. Invalidation emits discarded, suppresses further drafts and keeps the final receipt discarded even if conditions recover. Final commitment still uses the ordinary atomic gate. Drafts are provisional: they do not enter stored conversation events, automatic memory or history. Clients check current output rights before each display, remove drafts on lost authority, hidden/disconnected pages or companion switch, and independently validate final output. Previously seen text cannot be revoked retroactively.

Disconnect detaches the subscriber while server execution drains usage and commits its durable receipt; stopping display is not cancellation. Recover through GET of the original command receipt, without dispatching a replacement command. The queue is bounded; a subscriber blocking delivery for over 2 seconds is detached. Draft text is capped at 65536 UTF-8 bytes and cumulative outgoing delta frames at 768 KiB; exhausting the latter suppresses drafts while retaining full final output and usage. See [streaming dialogue](dialogue-streaming.md) for provider framing, client behavior and verification.

## HTTP routes

GET /healthz
GET /v1/capabilities
POST, GET /v1/nodes
POST /v1/nodes/access/web
POST /v1/nodes/{node_id}/access/{access_id}/heartbeat
DELETE /v1/nodes/{node_id}/access/{access_id}
POST /v1/nodes/{node_id}/presentations
POST /v1/nodes/{node_id}/presentations/{presentation_id}/heartbeat
DELETE /v1/nodes/{node_id}/presentations/{presentation_id}
GET /v1/nodes/{node_id}
POST /v1/nodes/{node_id}/heartbeat
POST /v1/nodes/{node_id}/observations
POST /v1/nodes/{node_id}/screen/sessions
GET /v1/nodes/{node_id}/screen/sessions/{session_id}
POST /v1/nodes/{node_id}/screen/sessions/{session_id}/stop
POST /v1/nodes/{node_id}/screen/sessions/{session_id}/purge
POST /v1/nodes/{node_id}/screen/uploads
PUT /v1/nodes/{node_id}/screen/uploads/{upload_id}/content
POST /v1/nodes/{node_id}/screen/frames
GET /v1/nodes/{node_id}/screen/frames/{frame_id}
GET /v1/nodes/{node_id}/screen/analyses/{analysis_id}
GET /v1/nodes/{node_id}/screen/state?companion_id=...&display_id=...
GET /v1/graph
POST, GET /v1/companions
GET /v1/companions/{companion_id}
POST /v1/companions/{companion_id}/context
GET, POST /v1/companions/{companion_id}/personality
POST /v1/companions/{companion_id}/personality/import
GET, POST /v1/companions/{companion_id}/personality/state
POST /v1/companions/{companion_id}/personality/preview
GET /v1/companions/{companion_id}/personality/history
GET /v1/companions/{companion_id}/personality/proposals
POST /v1/companions/{companion_id}/personality/proposals/refresh
GET /v1/companions/{companion_id}/personality/proposals/{proposal_id}
POST /v1/companions/{companion_id}/personality/proposals/{proposal_id}/accept
POST /v1/companions/{companion_id}/personality/proposals/{proposal_id}/reject
GET /v1/companions/{companion_id}/emotion
GET /v1/companions/{companion_id}/emotion/experiences
POST /v1/companions/{companion_id}/emotion/experiences/{event_id}/invalidate
POST /v1/companions/{companion_id}/emotion/experiences/{event_id}/correct
POST /v1/companions/{companion_id}/dialogue
POST /v1/companions/{companion_id}/dialogue/stream
GET /v1/companions/{companion_id}/dialogue/history
GET /v1/companions/{companion_id}/dialogue/{command_id}
GET /v1/companions/{companion_id}/proactivity/native-output
GET /v1/companions/{companion_id}/proactivity/schedule
GET /v1/companions/{companion_id}/tools
GET /v1/companions/{companion_id}/delegations
POST /v1/companions/{companion_id}/transfers
GET /v1/transfers/{transfer_id}
POST /v1/transfers/{transfer_id}/ack
POST /v1/transfers/{transfer_id}/abort
POST /v1/companions/{companion_id}/follow
POST /v1/companions/{companion_id}/lease
GET /v1/events?after_sequence=0&node_id=...&limit=100
POST, GET /v1/companions/{companion_id}/memories
POST /v1/companions/{companion_id}/memories/recall
POST /v1/companions/{companion_id}/memories/semantic-recall
GET /v1/companions/{companion_id}/memories/embeddings
GET /v1/companions/{companion_id}/memories/embeddings/jobs
POST /v1/companions/{companion_id}/memories/embeddings/run
GET /v1/companions/{companion_id}/memories/knowledge-graph
PUT /v1/companions/{companion_id}/memories/knowledge-graph/entities/{entity_id}/aliases
GET /v1/companions/{companion_id}/memories/stats
GET /v1/companions/{companion_id}/memories/consolidation-plan
PATCH, DELETE /v1/companions/{companion_id}/memories/{memory_id}
POST /v1/companions/{companion_id}/memories/{memory_id}/corrections
DELETE /v1/companions/{companion_id}/memory?command_id=...
GET /v1/memory/policy
GET /v1/dist/manifest.json
GET /v1/dist/packages/{wheel}
POST /v1/dist/packages

## Client distribution

`vl setup` pulls a published Linux desktop client from these endpoints. Downloads need the regular bearer token; uploads additionally require `[dist] upload_secret`, so a leaked `api_token` cannot replace the client later seats download. Filenames must match `virtual_life_linux-<version>-py3-none-any.whl`. The manifest is derived from the `[dist] directory` contents and cached per `(mtime, size)`, so publishing is an upload with no restart. `default_version` is the highest published version, where a final release outranks its own prereleases. The client re-hashes the download and aborts on a sha256 mismatch. Schema and client behaviour: [Linux client contract](linux-client-contract.md#10-客户端分发通道).

## Screen vision

Admin-token-only, explicit user session. The observer node declares `vision.capture`; in this version `subject_node_id` must equal `observer_node_id`. Clients create a session, register an upload (`sha256/byte_length/width/height/media_type`), stream the raw `image/png`/`image/jpeg` bytes to `PUT .../uploads/{upload_id}/content`, then submit a frame whose `input` is a `kind`-discriminated union of `image` (bound `upload_id`), `reference` (`reference_frame_id`, `reason=client_filtered`), or `unavailable` (`locked/permission_denied/user_paused/capture_failed/unknown`). Frames may carry optional `client_features`, `host_metadata`, and up to 32 `redacted_regions`.

In the default `two_stage` mode, the durable worker normalizes images, re-verifies pixel identity for exact reuse, and calls a separate OpenAI-compatible vision model; results are bounded structured `ScreenAnalysis` records exposed read-only. In `single_pass` mode no visual worker or `ScreenAnalysis` is created: frames remain `accepted`, and the latest fresh image is attached to the `[model]` dialogue request, which must support OpenAI `image_url` data URIs. Job states are `queued/running -> succeeded|reused|partial|failed|unknown` plus `superseded/expired/cancelled/discarded`. `ScreenState` distinguishes latest observation from latest analysis and carries `pending_newer_frame` and `barrier_sequence`; `unavailable` lowers `observable` and fences earlier results. Reporter credentials are rejected with 403 on these routes before any body parsing. Commands are idempotent by `command_id`; conflicting payloads return 409. `GET /v1/capabilities` adds a `screen_vision` object with effective limits. Full contract, error codes, budget coupling and unverified items: [Screen vision](screen-vision.md).

## Continuous voice sessions

The native host owns microphone capture, local streaming ASR, VAD and final-turn
decisions. It sends final text, never microphone PCM, to the server. The server
reuses the existing dialogue, budget and provenance paths, synthesizes bounded
speech segments and exposes private PCM downloads. Screen understanding uses
the existing server vision APIs; screenshot capture stays on the device.

Voice sessions require the active owner, epoch, revision, lease and native
presentation. Android presentations use `kind=android`; reporter credentials
cannot access voice routes. A session heartbeat cannot revive a stopped or
expired session. A generation fence invalidates queued synthesis, published
segments, downloads and playback reports together. Clients stop locally before
performing network cancellation and never resume a revoked generation.

Turns, interrupts and playback reports use durable command IDs. Explicit
interruption preserves already incurred model usage and immutable completed
dialogue receipts. Restart expires live sessions and never redispatches an
unknown in-flight call. SSE is bounded and reports cursor gaps, requiring a
fresh snapshot rather than replaying old audio. See [voice wire contract](voice-contract.md),
[configuration](configuration.md) and [implementation evidence](realtime-voice-implementation-report.md).

## Python module integration contract

Coordinator(database_path, node_ttl_seconds=60, lease_ttl_seconds=10, transfer_ttl_seconds=30, clock=None, routing_policy=None, auto_follow_rooms=True, auto_follow_quiet_seconds=15). A routing policy is `(source_snapshot_or_none, target_snapshot) -> (available, reason)`. It can narrow allowed routes; target liveness and output capability remain mandatory. The graph, preparation and commit use the same policy. Synchronous methods take JSON-like dictionaries and return JSON-serializable values: register_node(payload), list_nodes(), get_node(node_id), heartbeat(node_id,payload), graph(), create_companion(payload), list_companions(), get_companion(companion_id), patch_context(companion_id,payload), prepare_transfer(companion_id,payload), get_transfer(transfer_id), ack_transfer(transfer_id,payload), abort_transfer(transfer_id), renew_lease(companion_id,payload), follow_room(companion_id,payload), observe(node_id,payload), events(after_sequence=0,node_id=None,limit=100), reconcile(). list_nodes and list_companions return arrays; snapshots, graph and event pages return dictionaries.

DomainError(status_code, code, message) is exported by coordinator.py. Validation uses Pydantic request classes exported from models.py: NodeRegister, NodeHeartbeat, CompanionCreate, ContextUpdate, TransferPrepare, TransferAck, LeaseRenew, ObservationCreate. All identifier/version/capability/payload bounds are validated. Event and command writes must be in the same SQLite transaction as state changes.

MemoryService(database_path, policy=None, clock=None, *, token_estimator=None) has sync methods write(companion_id,payload), list(companion_id,limit=50,offset=0), recall(companion_id,payload), correct(companion_id,memory_id,payload), update_metadata(companion_id,memory_id,payload), delete(companion_id,memory_id), stats(companion_id), policy_snapshot(), plan_consolidation(companion_id). It owns its own tables in the same database; connections/transactions are independent of coordinator internals. Exports MemoryWrite, MemoryCorrection, MemoryMetadataUpdate, MemoryRecall, MemoryPolicy and MemoryError(status_code,code,message). API checks companion existence before memory operations.

Its persistent ledger methods are reserve_model_tokens(companion_id,payload), settle_model_tokens(companion_id,task_id,payload), release_model_tokens(companion_id,task_id), and budget_snapshot(companion_id,day=None). Reservation payload is `{task_id, purpose, input_tokens, output_tokens}`; settlement is `{input_tokens, output_tokens}`. Task IDs are globally unique and retries preserve the original reservation day. Actual usage overruns and late settlement after release are recorded. The ledger provides accounting; DialogueService uses a separate persistent command claim for execution coordination.

PersonalityService(database_path, clock=None) owns profile, state and command tables through SQLiteStore. It exports get(companion_id), update(companion_id,payload), import_card(companion_id,payload), get_state(companion_id), update_state(companion_id,payload), typed request/response models and PersonalityError(status_code,code,message). Methods accept JSON-like dictionaries and return JSON-serializable dictionaries. Companion existence is checked by the service.

PersonalityPromptBuilder(memory_service, policy=None, token_estimator=None) exports preview(companion_snapshot, personality_snapshot, state_snapshot, payload, *, output_permitted=False, host_allows_audio=False). Transport alone supplies output conditions. PersonalityPolicy, PersonalityPreview, PersonalityPreviewResult and ParticipationPreview are exported by personality_prompt.py. The builder is local and does not own node output rights.

DialogueService(coordinator, personality_service, memory_service, prompt_builder, *, provider=None, model_name="", clock=None) requires one shared durable database and exports send(companion_id,payload), get_result(companion_id,command_id), DialogueRequest, DialogueResult, and DialogueError(status_code,code,message). It owns dialogue_commands; event writes use the coordinator's transaction store. An optional `tool_runtime` attribute accepts a ToolRegistry and enables the bounded tool round. CompatibleChatProvider(base_url, api_key="", *, timeout_seconds=60, extra_body=None) implements ModelProvider.complete and stream; embedding remains unimplemented.

create_app(settings=None, *, routing_policy=None, memory_policy=None, personality_policy=None, token_estimator=None, model_provider=None, delegation_executor=None, tool_plugins=None) injects policies, a provider, an optional delegation backend and optional prebuilt tool plugins into the HTTP runtime. Configured settings instantiate the default compatible provider and subprocess delegation executor unless one is injected. app.state exposes coordinator, memory, personality, personality_prompt_builder, dialogue, delegation, tools and settings; `tools` is the `ToolRegistry` when enabled and null otherwise. Model execution is disabled until base URL and model are configured; memory retrieval and preview remain local.

All modules implement these contracts and can add internal typed models. Any contract change must be shared with the parent and other workers before clients use it.

### Memory retention and reinforcement

`retention_policy` is `auto` (write default), `permanent`, `stable`, `significant_episode`, or `routine_episode`. Correction and metadata requests may omit it to inherit, but reject explicit null. MemoryRecord exposes both `retention_policy` and computed `effective_retention_policy`; recall items additionally expose `decay_factor` and `effective_importance`. Pinned records are permanent. Auto recognizes permanent birthday facts only with supporting source evidence and known normalized semantic-key slot suffixes (`birthday`, `birth_date`, `date_of_birth`, `生日`, `出生日期`); other semantic records are stable and episodes routine. Explicit policies override inference.

Decay uses `D = floor + (1-floor) * 2^(-age_days/half_life_days)` and salience `importance * D`; it changes neither stored importance, truth, confidence, revision nor validity. Permanent records have D=1. Relevant supporting memories actually used in a successful completed dialogue's final context may reset the anchor once per UTC day; recall/preview and failed, silent or discarded dialogues do not reinforce. Corrections start a new record anchor. Disabling decay restores the existing recency ranking.

Optional `MemoryRecall.semantic_key` performs exact normalized slot lookup independent of ordinary candidate caps, retaining source support, validity, layer and output budgets. A separate bounded permanent candidate pool protects salience but does not guarantee prompt inclusion. See [memory design](memory.md) and [configuration](configuration.md).

Semantic ranking adds a bounded salience prior controlled by `memory.decay.semantic_salience_weight` (default 0.05), without scaling the entire relevance score or changing the raw cosine threshold. Relevant permanent source records are attempted before derived summaries/relations, subject to the final prompt budget. Birthday protection adds no scheduled reminders.

### 记忆诊断日志

`GET /v1/companions/{companion_id}/memories/logs` 使用与其余 `/v1` 接口相同的 Bearer 鉴权，先校验伙伴存在；不同伙伴的日志不可混读。该只读接口返回独立的持久诊断记录，不会触发召回、强化或模型调用。

查询参数 `after_sequence` 默认为 0，只读取严格大于该值的记录；`limit` 默认 100，范围 1–500。可选 `operation`、`status`、`command_id` 均为精确筛选；非法操作或状态返回 422。结果按 sequence 升序，包含 `items`、`next_sequence`、`has_more`；继续读取时保留相同筛选并传回 `next_sequence`。筛选改变后从 0 开始。序号跨伙伴共享，允许有空洞。

每项包含 `sequence`、`log_id`、`companion_id`、`operation`、`status`、`command_id`、`job_id`、`duration_ms`、`created_at` 和有界 `details`。诊断经过字段白名单、敏感字符串清理和长度限制，保留来源版本、预算、召回选择、用量和失败代码等信息；不替代原始事件、命令回执或记忆 provenance。记录可随诊断保留策略被裁剪，游标不承诺永久完整审计。

### Continuous affect and experience corrections

See [emotion contracts](emotion.md) for full fields, bounds and compatibility. New optional fields are `emotion` on PersonalityPreviewResult, and `accepted_event_id` plus `emotion` on DialogueResult. Old receipts omit these fields. Preview emotion is the generation snapshot; result emotion is the final snapshot after successful reply effort. `state_revision` retains its legacy meaning.

A fresh dialogue validates node/epoch/revision/lease before claiming the command, then atomically commits the claim, `companion.dialogue_received` (`{command_id, user_message}`) and local affect appraisal. Provider failures retain this accepted observation. Receipt replay adds no observation. Foreground dialogue remains outside transactions; with tools disabled it makes one model call, and with tools enabled at most two (one tool round followed by the final reply).

DialogueRequest additionally accepts optional access_id or presentation_id (mutually exclusive; supplying both is 422). When supplied, their live binding to node_id is checked at acceptance, dispatch, every streamed chunk and output commit; replacing a page access or native presentation on the same device cannot authorize an older reply. Executed usage is still settled if the channel expires/releases during generation. Omitting both preserves native request behavior and the legacy command fingerprint. These channel IDs do not grant device ownership or replace epoch/lease gates. Proactive native output is read from GET `/companions/{id}/proactivity/native-output?node_id&presentation_id&epoch&after_sequence`, which uses its own response type (no access_id) and only returns output produced after the session's `opened_sequence`. Completed user dialogue can be reviewed with GET `/companions/{id}/dialogue/history?before_sequence&limit` (newest first, exclusive cursor); history grants no present-output rights and triggers no model or memory write.

Affect snapshots project a bounded ledger with server-time recovery. Durable emotion `revision` and source-sensitive `context_version` fence concurrent changes; normal recovery does not change context_version. Dispatch and final output check this fingerprint alongside existing source, profile, legacy expiry, owner and lease gates. New accepted turns invalidate older replies. Executed usage is settled for discarded output. Successful completion links its observation to the completed event and commits effort, affect events and the terminal receipt together.

`companion.emotion_updated` payload is `{emotion_revision, source_event_id}`: a refresh hint, not an output permit. Clients should also refresh after correcting/deleting source memories. Numerical state and evaluated_at can change without a revision bump. Hosts still check output authority before rendering.

GET `/emotion/experiences` uses `after_sequence>=0`, `limit=1..100` and returns ascending `items`, `next_sequence`, `has_more`. POST `/emotion/experiences/{event_id}/invalidate` accepts `{command_id, expected_revision}` and atomically writes clarification, version, event and idempotent receipt. Changed reused commands and stale revisions return 409; cross-companion experience IDs return 404. Exact memory versions support resonance; correction, expiry, deletion and clarification retract invalid influence.

POST `/emotion/experiences/{event_id}/correct` adds `kind=direction|intensity|attribution` to command_id and expected_revision. Direction removes emotional influence without reversing it; intensity cumulatively shrinks current influence by retained_weight (default 0.5, range 0..1); attribution requires attribution=self|other|situation|unspecified and halves the current control increment and agency/control interpretation. Attribution preserves valence/arousal. All three retain completed reply effort. Kind-inapplicable fields, including explicit null, and unknown fields are rejected. Invalidated/forgotten experiences reject correction with 409. Original appraisal evidence is preserved separately from cumulative corrections; the operation, event, revision and receipt commit atomically.

Experience fields add optional appraisal (six dimensions), appraisal_version (legacy_v1 for old rows), uncertainty, attribution, original_delta and correction. Status adds corrected. Delta is the corrected contribution; correction records the latest kind, retained_weight, attribution, revision and corrected_at. Forgotten sources hide these details. `companion.emotion_corrected` records source_event_id, correction and cumulative weights, followed by the existing refresh event. Resonance records source correction versions; correcting a source retracts cached resonance, and new resonance uses corrected influence. Corrections fence foreground and proactive output through context_version.

Optional emotion policy fields valence_half_life_seconds, arousal_half_life_seconds, control_half_life_seconds and resource_half_life_seconds are null by default, falling back to half_life_seconds. Non-null values are integers 60..7200. Resources share one override; quiet-request lifetime retains the shared half-life. Snapshots add optional/null recovery_half_lives with resolved valence/arousal/control/energy/social_battery values. Recovery remains deterministic and read-only, with no context-version changes caused by elapsed time.

### Attention and proactive participation

See [attention and proactivity contracts](proactivity.md) for fields, defaults and limits.
POST `/nodes/{node_id}/attention` accepts `{command_id, sample}` and requires
`attention.observe`. GET returns `{node_id, source_event_id, sample, expires_at, stale}`.
Samples are advisory, timezone-aware, sequenced within a host session and TTL bounded;
retired sessions and old samples cannot overwrite current attention. Heartbeats do not
clear the separate attention snapshot. Significant changes emit `node.observation`
with `modality="ambient"`, `data.attention=sample`, `user_initiated=false`.

GET `/nodes/{node_id}/focus` projects persistent host-advisory focus state, protection,
bounded duration, lifetime completed count and the most recent 20 confirmed segments.
The first safe focused sample protects immediately; 60 seconds of continuous valid
focused samples confirm a segment. Ordinary unfocused samples need a 30-second release
period; a safe explicit task_completed/break_started boundary can end it immediately.
Unknown signals, expired gaps and retired sessions are not continuity evidence. Reads
never write or trigger a model. Focus updates commit with the attention command;
they do not change interaction_mode or generate proactive candidates by themselves.

GET/POST `/companions/{id}/proactivity` reads/replaces a separately versioned policy.
Update body is `{command_id, expected_revision, policy}`, with CAS and idempotent receipts.
Default enabled=false; chat mode and existing personality participation remain required.
Only task_completed/break_started/user_returned boundaries create candidates. Candidate
state and source event commit atomically with attention and its command receipt.
GET `/companions/{id}/proactivity/decisions?after_sequence=0&limit=50` returns
`{decisions, next_sequence}`, with limit 1..200. Sequence orders candidate creation;
refresh previous pages for mutable status or follow `companion.proactivity_decided`
events (`candidate_id, source_event_id, status, reason, policy_revision`). Policy writes
emit `companion.proactivity_policy_updated` with policy_revision.
Optional `latest=true` selects the latest N matching decisions while retaining ascending
response order; default false preserves forward pagination. This cursor orders candidates,
not events, and is separate from the output cursor below.

Proactive text uses the existing quota ledger (purpose=proactive), at most one model call,
and checks current attention, policy, explicit silence, accepted user turns, node, epoch,
lease, source versions and transfer state before dispatch and final commit. Unknown
signals suppress output. Attempts count toward UTC daily limits and cooldown even if
failed or discarded. Unknown executions are not retried. Proactive events are
`companion.proactive_received/completed/silent/discarded`; completed is directed to its
node and contains command_id, candidate_id, epoch, companion_revision, assistant_message,
usage, memory_refs and memory_versions, without user_message. These events do not enter
user affect appraisal, automatic memory or memory reinforcement.

GET `/companions/{id}/dialogue/{command_id}` also reads proactive receipts. DialogueResult
adds optional discard_reason, null for successful output. accepted_event_id may identify
proactive_received and must not be assumed to represent a user statement. Historical
events/receipts grant no output authority; hosts must recheck current policy, attention,
expiry, context and fresh lease before display. POST `/dialogue` remains user-only.

GET `/companions/{id}/proactivity/output` takes required node_id/access_id/epoch and optional
after_sequence. A missing cursor returns empty items and the current global event cursor,
establishing a fresh subscription without replay. Subsequent bounded pages contain
`{sequence,event_id,command_id,assistant_message,created_at}` plus next_sequence and the
companion_id/node_id/access_id/epoch/companion_revision envelope. The route requires a
live Web access and output lease, is not cacheable, invokes no model, and rechecks policy,
attention, focus protection, candidate expiry, user activity and all referenced context.
It skips output predating the current access. An intervening unsafe attention state or
replacement boundary permanently invalidates the old remark, even if the latest state
later becomes safe again. Skipped events advance the cursor; unknown/old remarks are not
held for future delivery. Clients discard late responses after stop, hide, transfer,
partner/access changes or new foreground input, and deduplicate by event_id. Historical
receipt reads remain available separately and do not bypass these live checks.
