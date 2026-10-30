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
  if (!cfg.baseUrl || !cfg.userId) {
    throw new Error("config requires baseUrl, userId");
  }
  log("info", "creating MatrixClient");
  const clientOpts = {
    baseUrl: cfg.baseUrl,
    userId: cfg.userId,
    deviceId: cfg.deviceId || undefined,
    useMatrixLogin: false,
  };
  // Prefer an explicit access token. With password auth (no token), do a
  // password login first so the sidecar owns a valid session + device.
  if (cfg.accessToken) {
    clientOpts.accessToken = cfg.accessToken;
  }
  client = sdk.createClient(clientOpts);
  if (!cfg.accessToken) {
    if (!cfg.password) {
      throw new Error("config requires accessToken or password");
    }
    log("info", "logging in with password");
    const loginRes = await client.login("m.login.password", {
      identifier: { type: "m.id.user", user: cfg.userId },
      password: cfg.password,
      initial_device_display_name: cfg.deviceName || "virtual-life-matrix-node",
    });
    log("info", `logged in as ${loginRes.user_id} device=${loginRes.device_id}`);
    // client.login() only sets http accessToken + credentials; it does NOT set
    // this.deviceId, so initRustCrypto would fail with "unknown deviceId".
    client.deviceId = loginRes.device_id;
  }

  log("info", "initialising RustCrypto (useIndexedDB:false)");
  // In Node there is no native IndexedDB, and wasm-bindgen's IndexedDB bindings
  // cannot accept a polyfill (fake-indexeddb) — "Dynamic cast failed". So the
  // crypto store is in-memory: the sidecar must run as ONE long-lived process
  // per client session (the Python matrix-node spawns it once and keeps it
  // alive). Restarting a sidecar with the SAME deviceId would regenerate
  // one-time keys and collide with already-uploaded ones (400 "...already
  // exists"); a fresh deviceId per password login avoids this entirely.
  await client.initRustCrypto({ useIndexedDB: false });
  crypto = client.getCrypto();
  if (!crypto) {
    throw new Error("RustCrypto did not initialise");
  }
  log("info", "RustCrypto ready");

  // Every password login yields a brand-new device_id that is NOT yet
  // cross-signed by the account owner. Recipients would otherwise flag it as
  // "encrypted by a device that has not been verified by its owner". The crypto
  // store is in-memory (see useIndexedDB:false above), so on a fresh sidecar the
  // Olm machine has no cross-signing keys: bootstrapCrossSigning falls through
  // to resetCrossSigning, which creates a new master/self-signing/user-signing
  // identity, uploads the public keys, and signs THIS device with the
  // self-signing key. The UIA callback below supplies the account password
  // (contract: makeRequest(null) triggers 401+session, then retry with the
  // password + session). This keeps the current device owner-verified.
  await bootstrapCrossSigning(cfg);
  log("info", "cross-signing bootstrap complete");

  // Wire cross-verification: this sidecar can both request verification from our
  // other (trusted) devices and auto-accept verification requests coming from
  // them. When the SAS emoji/decimal are ready on either side we surface them to
  // the Python matrix-node, which relays them to the user for confirmation.
  setupVerification(cfg);

  emit({ event: "ready" });

  // Only RoomTimeline drives plaintext emission. (RustCrypto has its own
  // ClientEvent.Event listener for key/to-device handling; a second listener
  // here would double-emit timeline events.)
  client.on(sdk.ClientEvent.RoomTimeline, (event, room) => {
    handleTimelineEvent(event, room);
  });

  // Events that arrive encrypted and decrypt *after* RoomTimeline fired (the
  // megolm key arrives later via to-device) emit Event.decrypted on success.
  // Surface those as plaintext messages too.
  client.on(sdk.MatrixEventEvent.Decrypted, (event) => {
    if (event.getType() === "m.room.message") {
      maybeEmitPlaintext(event);
    }
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

async function bootstrapCrossSigning(cfg) {
  if (!crypto) return;
  // The sidecar resets cross-signing on every fresh login (in-memory store, no
  // 4S to import from), so only ONE sidecar per account should run with
  // bootstrap enabled — the one whose SENT messages must be owner-verified.
  // A receiver sidecar must set bootstrapCrossSigning:false, otherwise its own
  // reset would invalidate the sender's identity. Default: enabled.
  //
  // Caveat: because the reset creates a fresh master identity each login,
  // only the device from the MOST RECENT bootstrap stays owner-signed; any
  // devices bootstrapped earlier (or receivers) are signed by a superseded
  // self-signing key. For a multi-device account that needs a stable identity,
  // production should configure a persistent crypto store (IndexedDB, or a
  // Node-compatible store) and import the 4S recovery keys instead.
  if (cfg.bootstrapCrossSigning === false) {
    log("info", "cross-signing bootstrap disabled by config");
    return;
  }
  // If the account already has cross-signing keys (e.g. a previous bootstrap),
  // bootstrapCrossSigning will detect them via crossSigningStatus and, with the
  // in-memory store, do nothing meaningful for a NEW device (it can't import the
  // private keys from 4S). Signing the new device then requires the owner's
  // self-signing key, which we don't have. To keep the new device verifiable we
  // therefore reset (create a fresh identity) and re-sign. This is acceptable for
  // a test/dev homeserver; production would import the 4S recovery keys instead.
  if (!cfg.password) {
    log("warn", "no password for cross-signing bootstrap; skipping (device may be unverified)");
    return;
  }
  const userId = cfg.userId;
  const password = cfg.password;
  const authUploadDeviceSigningKeys = async (makeRequest) => {
    let session;
    try {
      await makeRequest(null);
    } catch (e) {
      session = e && e.data && e.data.session;
      if (!session) throw e;
    }
    await makeRequest({
      type: "m.login.password",
      identifier: { type: "m.id.user", user: userId },
      password: password,
      session: session,
    });
  };
  try {
    await crypto.bootstrapCrossSigning({
      setupNewCrossSigning: true,
      authUploadDeviceSigningKeys,
    });
    log("info", "bootstrapCrossSigning: setupNewCrossSigning ok");
  } catch (e) {
    log("warn", `bootstrapCrossSigning failed: ${e && e.message}`);
  }
}

// --- Cross-verification (SAS) -------------------------------------------
//
// A fresh device is only owner-verified after it has been cross-signed. This
// sidecar bootstraps its own cross-signing identity, but to get a *second* (or
// any future) device verified the account owner must confirm on an already
// trusted device. We support the full SAS flow on both sides:
//
//   * requestOwnUserVerification() — ask our other devices to verify this one.
//   * VerificationRequestReceived listener — auto-accept a request that one of
//     our trusted devices sends to us.
//   * VerifierEvent.ShowSas — surface the emoji/decimal to Python so the user
//     can compare, then confirm() or mismatch().
//
// New stdout event types:
//   {"event":"verification","state":"request_sent","transaction_id":...}
//   {"event":"verification","state":"sas","transaction_id":...,"emoji":[[..]..],"decimal":[..]}
//   {"event":"verification","state":"done","transaction_id":...}
//   {"event":"verification","state":"cancelled","transaction_id":...,"reason":...}
// New stdin command:
//   {"cmd":"verification","action":"confirm","transaction_id":...}
//   {"cmd":"verification","action":"mismatch","transaction_id":...}
//   {"cmd":"verification","action":"cancel","transaction_id":...}

let activeVerification = null;

function setupVerification(cfg) {
  if (!crypto) return;
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");

  // Always listen for incoming verification requests and auto-accept them. This
  // is the half that makes "open your trusted device and it just works": a
  // trusted device holding the cross-signing identity receives the request and
  // drives the SAS; a brand-new device receives it, accepts, and is verified.
  client.on(cryptoApi.CryptoEvent.VerificationRequestReceived, (request) => {
    handleIncomingVerification(request);
  });

  // Optionally drive the flow at startup: request that our OTHER (trusted)
  // devices verify THIS one. Only meaningful on a device with a cross-signing
  // identity (getOwnIdentity() present) — i.e. a freshly bootstrapped one.
  // A listener-only sidecar sets verification:false.
  if (cfg.verification === "auto" || cfg.autoVerify) {
    void requestOwnVerification();
  } else {
    log("info", "cross-verification listen-only (no auto request)");
  }
}

async function requestOwnVerification() {
  try {
    const req = await crypto.requestOwnUserVerification();
    log("info", `requestOwnUserVerification sent txn=${req.transactionId}`);
    wireVerificationRequest(req);
  } catch (e) {
    log("warn", `requestOwnUserVerification failed: ${e && e.message}`);
  }
}

function handleIncomingVerification(request) {
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");
  log("info", `incoming verification request from ${request.otherUserId} phase=${request.phase}`);
  wireVerificationRequest(request);
}

function wireVerificationRequest(request) {
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");
  // The homeserver echoes our own outgoing m.key.verification.request back to us,
  // so the SAME rust request can arrive both from requestOwnUserVerification()
  // and from CryptoEvent.VerificationRequestReceived. Each path wraps it in a
  // DIFFERENT JS RustVerificationRequest object, but they share one transactionId
  // and one underlying rust request. Dedupe by transactionId, otherwise the
  // "keep only one active flow" guard below would cancel our own request.
  if (activeVerification && request.transactionId && activeVerification.transactionId === request.transactionId) {
    log("info", `verification txn=${request.transactionId} duplicate wrapper ignored`);
    return;
  }
  // Keep only one active SAS flow at a time.
  if (activeVerification) {
    void activeVerification.cancel({ reason: "User declined" });
  }
  activeVerification = request;

  const onChange = () => {
    const phase = request.phase;
    log("info", `verification txn=${request.transactionId} phase=${phase}`);
    if (phase === cryptoApi.VerificationPhase.Done) {
      emit({ event: "verification", state: "done", transaction_id: request.transactionId });
      activeVerification = null;
      return;
    }
    if (phase === cryptoApi.VerificationPhase.Cancelled) {
      emit({
        event: "verification",
        state: "cancelled",
        transaction_id: request.transactionId,
        reason: request.cancellationCode || "unknown",
      });
      activeVerification = null;
      return;
    }
    // Responder side: we received the request, so send `ready` (accept).
    if (phase === cryptoApi.VerificationPhase.Requested && !request.initiatedByMe) {
      void acceptRequest(request);
      return;
    }
    // Initiator side: once the other side sent `ready`, we send `start`
    // (creates our verifier) and drive the SAS.
    if (phase === cryptoApi.VerificationPhase.Ready && request.initiatedByMe) {
      void startSas(request);
      return;
    }
    // Both sides: once the verifier exists (Started / Transitioned), attach it.
    // The responder sends `accept` via verify(); the initiator's verify() is a
    // no-op send (its accept was already sent by startVerification).
    if (request.verifier && !request.verifier._attached) {
      attachVerifier(request, request.verifier);
    }
  };

  request.on(cryptoApi.VerificationRequestEvent.Change, onChange);
  // If the request already advanced past Ready before we attached (rare), run once.
  onChange();
}

async function acceptRequest(request) {
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");
  try {
    if (request.phase === cryptoApi.VerificationPhase.Requested) {
      await request.accept();
    }
  } catch (e) {
    log("warn", `acceptRequest failed: ${e && e.message}`);
  }
}

async function startSas(request) {
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");
  try {
    if (request.phase === cryptoApi.VerificationPhase.Ready) {
      const verifier = await request.startVerification("m.sas.v1");
      if (verifier && !verifier._attached) {
        attachVerifier(request, verifier);
      }
    }
  } catch (e) {
    log("warn", `startSas failed: ${e && e.message}`);
  }
}

function attachVerifier(request, verifier) {
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");
  verifier._attached = true;
  verifier.on(cryptoApi.VerifierEvent.ShowSas, (callbacks) => {
    // Surface the SAS emoji/decimal to the user (via Python) for comparison.
    emit({
      event: "verification",
      state: "sas",
      transaction_id: request.transactionId,
      emoji: callbacks.sas.emoji || [],
      decimal: callbacks.sas.decimal || [],
    });
    // Keep the callbacks so a later confirm/mismatch/cancel command can act on them.
    verifier._sasCallbacks = callbacks;
  });
  verifier.on(cryptoApi.VerifierEvent.Cancel, () => {
    emit({
      event: "verification",
      state: "cancelled",
      transaction_id: request.transactionId,
      reason: "cancel",
    });
  });
  // verify() resolves on completion; a rejection means cancelled/timed out.
  verifier
    .verify()
    .then(() => {
      emit({ event: "verification", state: "done", transaction_id: request.transactionId });
      activeVerification = null;
    })
    .catch((e) => {
      log("warn", `verification ended: ${e && e.message}`);
    });
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

async function waitForEncryption(roomId, timeoutMs) {
  // The room's m.room.encryption state event only reaches the client after the
  // initial sync (or a sync that includes the room). If we send before that,
  // sendTextMessage falls back to PLAINTEXT m.room.message. Poll until the room
  // is known to be encrypted (or timeout), so sends stay encrypted.
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if (client.isRoomEncrypted(roomId)) return true;
    } catch (e) {
      // room state not loaded yet
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function handleCommand(cmd) {
  if (cmd.cmd === "ping") {
    emit({ event: "pong" });
    return;
  }
  if (cmd.cmd === "send") {
    const txnId = cmd.txn_id || `vlmx-${Date.now()}-${nextTxn++}`;
    // Wait for the room to be confirmed encrypted (state event loaded) before
    // sending, otherwise the message goes out as plaintext.
    const enc = await waitForEncryption(cmd.room_id, 15000);
    if (!enc) {
      emit({ event: "error", message: `room ${cmd.room_id} not encrypted after 15s; refusing plaintext send` });
      return;
    }
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
  if (cmd.cmd === "verification") {
    await handleVerificationCommand(cmd);
    return;
  }
  emit({ event: "error", message: `unknown command ${cmd.cmd}` });
}

async function handleVerificationCommand(cmd) {
  const cryptoApi = require("matrix-js-sdk/lib/crypto-api");
  if (cmd.action === "request_device") {
    // Trusted-device-driven flow: request verification toward a specific device
    // (e.g. a brand-new one that must be owner-verified).
    try {
      const req = await crypto.requestDeviceVerification(cmd.user_id, cmd.device_id);
      log("info", `requestDeviceVerification sent txn=${req.transactionId}`);
      wireVerificationRequest(req);
      emit({ event: "verification", state: "request_sent", transaction_id: req.transactionId });
    } catch (e) {
      emit({ event: "error", message: `requestDeviceVerification failed: ${e && e.message}` });
    }
    return;
  }
  if (cmd.action === "confirm") {
    const callbacks = activeVerification && activeVerification.verifier && activeVerification.verifier._sasCallbacks;
    if (!callbacks) {
      emit({ event: "error", message: "no active SAS to confirm" });
      return;
    }
    try {
      await callbacks.confirm();
      emit({ event: "verification", state: "confirm_sent", transaction_id: cmd.transaction_id });
    } catch (e) {
      emit({ event: "error", message: `sas confirm failed: ${e && e.message}` });
    }
    return;
  }
  if (cmd.action === "mismatch") {
    const callbacks = activeVerification && activeVerification.verifier && activeVerification.verifier._sasCallbacks;
    if (!callbacks) {
      emit({ event: "error", message: "no active SAS to mismatch" });
      return;
    }
    callbacks.mismatch();
    emit({ event: "verification", state: "mismatch_sent", transaction_id: cmd.transaction_id });
    return;
  }
  if (cmd.action === "cancel") {
    if (activeVerification) {
      await activeVerification.cancel({ reason: "User declined" });
    }
    emit({ event: "verification", state: "cancelled", transaction_id: cmd.transaction_id });
    return;
  }
  emit({ event: "error", message: `unknown verification action ${cmd.action}` });
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
