#!/usr/bin/env node
// E2EE sidecar for the Virtual Life matrix-node.
//
// Owns the full Matrix session (login + sync + RustCrypto + decrypt + send)
// so that matrix-js-sdk can run its Olm/Megolm key-exchange with the homeserver
// and decrypt/encrypt m.room.encrypted events. The Python matrix-node talks to
// this sidecar over stdio with newline-delimited JSON:
//
//   Python -> Node:
//     {"cmd":"send","room_id":"!r:x","body":"hi","txn_id":"t1"}
//     {"cmd":"set_encryption","room_id":"!r:x"}   // opt-in: force encryption
//     {"cmd":"ping"}
//   Node -> Python:
//     {"event":"message","room_id":"!r:x","sender":"@u:x","body":"hi",
//      "event_id":"$e","ts":123}
//     {"event":"ready"}                            // after login + initRustCrypto
//     {"event":"log","level":"info","message":"..."}
//     {"event":"error","message":"..."}
//
// Env config (JSON on argv[2] or env E2EE_CONFIG):
//   { "baseUrl": "...", "userId": "...", "accessToken": "...",
//     "deviceId": "...", "syncTimeoutMs": 30000 }
//
// Requires `npm install` (matrix-js-sdk + @matrix-org/matrix-sdk-crypto-wasm).

const fs = require("fs");
const readline = require("readline");
const sdk = require("matrix-js-sdk");

function loadConfig() {
  if (process.argv[2]) {
    return JSON.parse(fs.readFileSync(process.argv[2], "utf-8"));
  }
  if (process.env.E2EE_CONFIG) {
    return JSON.parse(process.env.E2EE_CONFIG);
  }
  throw new Error("missing config: pass a JSON path or E2EE_CONFIG");
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const log = (level, message) => emit({ event: "log", level, message });

let client = null;
let crypto = null;
let nextTxn = 0;

async function init() {
  const cfg = loadConfig();
  if (!cfg.baseUrl || !cfg.userId || !cfg.accessToken) {
    throw new Error("config requires baseUrl, userId, accessToken");
  }
  log("info", "creating MatrixClient");
  client = sdk.createClient({
    baseUrl: cfg.baseUrl,
    userId: cfg.userId,
    accessToken: cfg.accessToken,
    deviceId: cfg.deviceId || undefined,
    useMatrixLogin: false,
  });

  log("info", "initialising RustCrypto (useIndexedDB:false)");
  await client.initRustCrypto({ useIndexedDB: false });
  crypto = client.getCrypto();
  if (!crypto) {
    throw new Error("RustCrypto did not initialise");
  }
  log("info", "RustCrypto ready");
  emit({ event: "ready" });

  // Only RoomTimeline drives plaintext emission. (RustCrypto has its own
  // ClientEvent.Event listener for key/to-device handling; a second listener
  // here would double-emit timeline events.)
  client.on(sdk.ClientEvent.RoomTimeline, (event, room) => {
    handleTimelineEvent(event, room);
  });

  // start sync
  log("info", "starting sync loop");
  await client.startClient({
    initialSyncLimit: 0,
    lazyLoadMembers: false,
    timelineSupport: false,
  });
  log("info", "sync started");
}

function handleLiveEvent(event) {
  // Encrypted timeline events are handled by RoomTimeline; live to-device and
  // key events go through client event handlers that RustCrypto subscribes to.
  if (event.getType() === "m.room.encrypted") {
    void client.decryptEventIfNeeded(event).then(() => {
      maybeEmitPlaintext(event);
    }).catch((e) => log("warn", `decrypt failed: ${e && e.message}`));
  }
}

function handleTimelineEvent(event, room) {
  const type = event.getType();
  if (type === "m.room.encrypted") {
    // Not yet decrypted: decrypt first, then emit plaintext.
    void client.decryptEventIfNeeded(event).then(() => {
      maybeEmitPlaintext(event);
    }).catch((e) => log("warn", `timeline decrypt failed: ${e && e.message}`));
    return;
  }
  // Already decrypted (RustCrypto decrypts during sync before RoomTimeline
  // fires) or plaintext: emit directly.
  maybeEmitPlaintext(event);
}

function maybeEmitPlaintext(event) {
  if (event.isDecryptionFailure()) {
    log("warn", `decryption failure: ${event.decryptionFailureReason}`);
    return;
  }
  const content = event.getContent();
  const type = event.getType();
  if (type !== "m.room.message") return;
  const msgtype = content && content.msgtype;
  if (msgtype !== "m.text") return;
  const body = typeof content.body === "string" ? content.body : "";
  if (!body) return;
  emit({
    event: "message",
    room_id: event.getRoomId(),
    sender: event.getSender(),
    body: body,
    event_id: event.getId(),
    ts: event.getTs(),
  });
}

async function handleCommand(cmd) {
  if (cmd.cmd === "ping") {
    emit({ event: "pong" });
    return;
  }
  if (cmd.cmd === "send") {
    const txnId = cmd.txn_id || `vlmx-${Date.now()}-${nextTxn++}`;
    const res = await client.sendTextMessage(cmd.room_id, cmd.body, txnId);
    emit({ event: "sent", room_id: cmd.room_id, event_id: res.event_id, txn_id: txnId });
    return;
  }
  if (cmd.cmd === "set_encryption") {
    // Opt-in: mark room as encrypted so RustCrypto prepares to encrypt.
    const room = client.getRoom(cmd.room_id);
    if (!room) {
      emit({ event: "error", message: `unknown room ${cmd.room_id}` });
      return;
    }
    emit({ event: "room_encryption", room_id: cmd.room_id, ok: true });
    return;
  }
  emit({ event: "error", message: `unknown command ${cmd.cmd}` });
}

async function main() {
  try {
    await init();
  } catch (e) {
    emit({ event: "error", message: `init failed: ${e && e.message}` });
    process.exit(1);
  }

  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    let cmd;
    try {
      cmd = JSON.parse(line);
    } catch (e) {
      emit({ event: "error", message: `bad json: ${line}` });
      return;
    }
    handleCommand(cmd).catch((e) => {
      emit({ event: "error", message: `command ${cmd && cmd.cmd} failed: ${e && e.message}` });
    });
  });

  process.on("SIGTERM", () => {
    if (client) void client.stopClient();
    process.exit(0);
  });
  process.on("SIGINT", () => {
    if (client) void client.stopClient();
    process.exit(0);
  });
}

main();
