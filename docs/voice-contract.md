# Voice Wire Contract

This document describes the implemented HTTP contract for voice sessions. The
native client captures audio and runs local ASR; requests to this API contain
the resulting text and typed ASR metadata, never microphone PCM.

The route prefix is `/v1/nodes/{node_id}/voice`. Requests use the normal
authenticated node-management credential. Reporter credentials receive `403`
on voice routes. JSON uses `snake_case`; request models reject unknown fields
and coercion of values to other types.

## Request Validation

Every voice `POST` body is limited to 24 KiB of actual bytes, counted while the
body is read and before JSON deserialization. Exactly 24 KiB is accepted; a
larger body returns `413 request_too_large`, even if `Content-Length` is absent
or understates the body. This limit is local to voice `POST` routes.

Integer fields reject booleans. Numeric duration fields reject booleans,
NaN, and infinities. Timestamp fields are RFC 3339 strings with an explicit
timezone (`Z` or `+/-HH:MM`); naive timestamps and non-string date values are
invalid. Response timestamps are published as OpenAPI `date-time` strings.

## Routes

| Method and path | Status | Response |
| --- | ---: | --- |
| `POST /sessions` | 201 | `VoiceSessionSnapshot` |
| `GET /sessions/{session_id}` | 200 | `VoiceSessionSnapshot` |
| `POST /sessions/{session_id}/heartbeat` | 200 | `VoiceSessionSnapshot` |
| `POST /sessions/{session_id}/stop` | 200 | `VoiceSessionSnapshot` |
| `POST /sessions/{session_id}/turns` | 202 | `VoiceTurnReceipt` |
| `GET /sessions/{session_id}/turns/{turn_id}` | 200 | `VoiceTurnSnapshot` |
| `GET /sessions/{session_id}/turns/{turn_id}/events` | 200 | `text/event-stream` |
| `GET /sessions/{session_id}/turns/{turn_id}/segments/{sequence}/audio` | 200 | Audio bytes |
| `POST /sessions/{session_id}/interrupt` | 200 | `VoiceInterruptReceipt` |
| `POST /sessions/{session_id}/playback` | 200 | `VoicePlaybackReceipt` |

The response models and event payloads retain the existing wire field names.
The OpenAPI document declares JSON response models, the SSE media type, and a
binary audio response.

## Sessions and Turns

Create a session with `command_id`, `companion_id`, `epoch`,
`expected_revision`, and `presentation_id`. `mode` is `continuous`,
`push_to_talk`, or `text`. Text sessions omit `asr`; ASR sessions require
`asr: {engine, model_id, language}`. `output` contains `audio_enabled` and
`voice_id`. `user_initiated` is `true`.

The session response includes its `session_id`, `session_generation`,
`output_generation`, mode, status, ASR and audio settings, timestamps, expiry
deadlines, heartbeat/idle limits, and the effective `limits` object. All
timestamps are timezone-aware date-time strings.

Turn submission requires `command_id`, `turn_id`, `session_generation`,
`expected_output_generation`, `epoch`, `expected_revision`, `text`,
`end_reason`, and a discriminated `input` object. Text is trimmed, must remain
nonempty, and is bounded by the configured input limit (at most 4096
characters). `screen_reference` and `continuation_of_turn_id` are optional.

Text input is `{kind: "text", mode: "manual"}` and requires
`end_reason: "manual"`. ASR input is `{kind: "asr", started_at, ended_at,
audio_duration_ms, asr_model_id, language, discontinuity}`. ASR timestamps must
be ordered, timezone-aware, and no more than 45 seconds apart; the declared
duration is an integer from 0 through 45000 milliseconds. The ASR model ID and
language must match the session configuration. Discontinuous input is rejected;
new ASR input older than the configured live window returns `voice_input_stale`.
Text sessions accept text input only.

`VoiceTurnReceipt` includes the turn/command/session/companion IDs,
`session_generation`, `output_generation`, status, event sequence, audio and
speech availability, and creation time. `VoiceTurnSnapshot` reports current
status, assistant text, model/usage when available, speech status, segment
count, sequence, and creation time.

Heartbeat and stop bodies contain `command_id`. Interrupt requests contain
`command_id`, `session_generation`, `target_turn_id`,
`expected_output_generation`, and a reason (`user_speech`, `stop_button`,
`text_input`, or `session_stop`). Playback reports contain `command_id`,
`turn_id`, `output_generation`, `segment_sequence`, `played_samples`, and a
status (`started`, `progress`, `completed`, `interrupted`, or `failed`).

## SSE Cursor and Replay

Subscribe with `GET .../events?after_sequence=N`, where `N` is a nonnegative
integer. Before response headers begin, the server checks that the session and
turn exist and reads the current turn snapshot and retained events. Unknown
turns therefore return an ordinary HTTP error rather than an SSE stream.

Frames use UTF-8 `event: <kind>` and `data: <JSON>` lines separated by a blank
line. Data frames carry `session_id`, `session_generation`, `turn_id`,
`output_generation`, and the per-turn `sequence`. The first frame is a
`snapshot` with those identity fields, the current `turn`, `sequence`, `cursor`,
and `replay_gap`.

If the requested cursor predates retained events, is ahead of the latest
sequence, or retained events reveal a missing sequence, the server sets
`replay_gap: true`, advances the snapshot cursor to the latest sequence, and
does not claim to replay missing events. Otherwise it emits retained events
after the requested sequence. A client reconnects with its last processed
sequence and treats a replay gap as a request to replace local state from the
snapshot; it must not infer that audio from the gap was delivered or played.

The retained event ring is bounded by `voice.event_buffer_size` (default 256).
While a turn is active, the server polls for events and sends a comment ping
after 10 seconds without a frame. A terminal turn ends with `voice_end`. Event
sequence is per turn; audio segments use their separate `segment_sequence`.
The initial state snapshot describes the latest state, so its sequence may be
greater than the replay events immediately following it. Without `replay_gap`,
clients keep their replay cursor until those ordinary events are processed.

## Audio Download

Download a segment using the authenticated audio route. The server checks the
node, session, turn, segment, current generation/output gate, cancellation
state, expiry, and file metadata before returning headers. Audio is streamed in
chunks of at most 8192 bytes; the service rechecks those gates before reading
each chunk. A revoked or expired segment cannot continue to be read from disk.

The response includes `Content-Length`, `Cache-Control: no-store`,
`X-Audio-SHA256`, `X-Audio-Sample-Rate`, `X-Audio-Channels`, and
`X-Audio-Sample-Count`. The body media type comes from segment metadata. The
declared byte length is derived from sample count, channel count, and 16-bit
samples. A client should treat a short body or a stream interrupted by a gate
change as incomplete and must not play bytes from an obsolete generation.

## Configuration Validation

The `[voice]` and `[speech]` configuration parsers reject unknown keys. Boolean
options require actual booleans; integer options require actual integers;
positive duration options must be finite numbers greater than zero. Voice
heartbeat duration must be less than the session TTL. Speech provider,
concurrency, sample-rate, and segment limits are checked against their
configured bounds.
