/* 虚拟生命 · 实时日志与流程追踪（AstrBot 风格）
 *
 * 只保留两件事：左侧「实时日志」尾随 /v1/flow/logs 的内存环形缓冲，
 * 右侧「流程追踪」尾随 /v1/events 的运行事件。无框架、无构建、无外部
 * 资源。令牌只走同源 API；勾选记住时经 /v1/session 换成 HttpOnly Cookie。
 */

"use strict";

const $ = (id) => document.getElementById(id);
const API_BASE = "/v1";
const LOG_POLL_MS = 1200;
const EVENT_POLL_MS = 1500;
const MAX_LOG_ROWS = 800;
const MAX_FLOW_ROWS = 400;

const session = {
  token: "",
  connected: false,
  connecting: false,
  logCursor: 0,
  logRows: [],
  logLevel: "ALL",
  logFollow: true,
  logRunning: false,
  flowCursor: 0,
  flowRows: [],
  flowPaused: false,
  flowRunning: false,
  firstFlow: true,
};

// ------------------------------------------------------------ API helpers

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  const body = options.body;
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const token = session.token || "";
  if (token) headers.Authorization = `Bearer ${token}`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs || 20000);
  try {
    const response = await fetch(API_BASE + path, {
      method: options.method || (body !== undefined ? "POST" : "GET"),
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const message =
        payload?.error?.message ||
        payload?.detail?.[0]?.msg ||
        `HTTP ${response.status}`;
      throw new Error(message);
    }
    return payload;
  } finally {
    clearTimeout(timeout);
  }
}

// ------------------------------------------------------------ Connection

function setConnection(on, label) {
  session.connected = on;
  $("connection").textContent = label || (on ? "已连接" : "未连接");
  const dot = document.querySelector(".status-dot");
  dot.classList.toggle("on", on);
  dot.classList.toggle("off", !on);
  if (!on) {
    $("logs-status").textContent = "未连接";
    $("flow-status").textContent = "未连接";
  }
}

async function connect() {
  if (session.connecting) return;
  session.connecting = true;
  $("connect").disabled = true;
  $("connection-error").textContent = "";
  try {
    const token = $("token").value.trim();
    if (!token) throw new Error("请输入管理令牌");
    session.token = token;
    await api("/capabilities"); // verify token
    if ($("remember").checked) {
      await api("/session", { method: "POST", body: { token } });
      session.token = ""; // cookie now carries the session
    }
    setConnection(true);
    $("connection-dialog").close();
    await resetStreams();
  } catch (error) {
    session.token = "";
    $("connection-error").textContent = error.message;
  } finally {
    session.connecting = false;
    $("connect").disabled = false;
  }
}

async function disconnect() {
  try {
    await api("/session", { method: "DELETE" });
  } catch {
    /* best effort */
  }
  session.token = "";
  session.logRows = [];
  session.flowRows = [];
  session.logCursor = 0;
  session.flowCursor = 0;
  $("log-view").replaceChildren();
  $("flow-view").replaceChildren();
  $("log-empty").style.display = "";
  $("flow-empty").style.display = "";
  setConnection(false);
}

async function restoreSession() {
  if (session.connected || session.connecting) return;
  session.connecting = true;
  try {
    await api("/capabilities");
    setConnection(true);
    await resetStreams();
  } catch {
    /* no remembered session */
  } finally {
    session.connecting = false;
  }
}

// ------------------------------------------------------------ Live logs

async function pollLogs() {
  if (!session.connected || session.logRunning) return;
  session.logRunning = true;
  try {
    const result = await api(
      `/flow/logs?after_sequence=${session.logCursor}&limit=200`,
    );
    if (Array.isArray(result.items) && result.items.length) {
      session.logCursor = result.next_sequence;
      for (const item of result.items) appendLogRow(item);
    } else {
      session.logCursor = result.next_sequence;
    }
    $("logs-status").textContent =
      `已载入 ${session.logRows.length} 条 · 游标 ${session.logCursor}` +
      (result.has_more ? " · 有更多" : "");
  } catch (error) {
    $("logs-status").textContent = `日志拉取失败：${error.message}`;
  } finally {
    session.logRunning = false;
  }
}

function appendLogRow(item) {
  if (session.logLevel !== "ALL" && item.level !== session.logLevel) return;
  const row = document.createElement("div");
  row.className = `log-line ${item.level}`;
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = item.timestamp + " ";
  const lvl = document.createElement("span");
  lvl.className = "lvl";
  lvl.textContent = item.level;
  const src = document.createElement("span");
  src.className = "src";
  src.textContent = item.logger || "runtime";
  const msg = document.createElement("span");
  msg.textContent = " " + item.message;
  row.append(ts, lvl, src, msg);
  $("log-empty").style.display = "none";
  $("log-view").append(row);
  session.logRows.push(item);
  if (session.logRows.length > MAX_LOG_ROWS) {
    const over = session.logRows.length - MAX_LOG_ROWS;
    session.logRows.splice(0, over);
    for (let i = 0; i < over; i++) $("log-view").firstChild?.remove();
  }
  if (session.logFollow) $("log-view").scrollTop = $("log-view").scrollHeight;
}

function clearLogs() {
  session.logRows = [];
  $("log-view").replaceChildren();
  $("log-empty").style.display = "";
  // Keep the cursor: a cleared view only hides history, live tail continues.
}

// ------------------------------------------------------------ Flow events

async function pollFlow() {
  if (!session.connected || session.flowRunning || session.flowPaused) return;
  session.flowRunning = true;
  try {
    const result = await api(
      `/events?after_sequence=${session.flowCursor}&limit=200`,
    );
    if (Array.isArray(result.events) && result.events.length) {
      session.flowCursor = result.next_sequence;
      for (const event of result.events) appendFlowRow(event);
    } else {
      session.flowCursor = result.next_sequence;
    }
    if (session.firstFlow) {
      session.firstFlow = false;
      $("flow-status").textContent = "已建立实时基线";
    }
    $("flow-status").textContent =
      `已载入 ${session.flowRows.length} 条事件 · 游标 ${session.flowCursor}`;
  } catch (error) {
    $("flow-status").textContent = `事件拉取失败：${error.message}`;
  } finally {
    session.flowRunning = false;
  }
}

const FLOW_TYPES = new Set([
  "companion.dialogue_completed",
  "companion.dialogue_silent",
  "companion.dialogue_discarded",
  "companion.transfer_prepared",
  "companion.transfer_committed",
  "companion.transfer_aborted",
  "node.registered",
  "node.observation",
  "node.attention",
  "companion.created",
  "companion.followed",
  "companion.emotion_updated",
  "companion.proactive_scheduled",
  "companion.proactive_delivered",
  "companion.proactive_suppressed",
  "memory.automatic_completed",
  "memory.erased",
]);

function appendFlowRow(event) {
  const type = event.type || "event";
  const row = document.createElement("div");
  row.className = "flow-item";
  const head = document.createElement("div");
  head.className = "fhead";
  const time = document.createElement("span");
  time.className = "ftime";
  time.textContent = (event.occurred_at || "").replace("T", " ").slice(0, 19);
  const label = document.createElement("span");
  label.className = "ftype";
  label.textContent = type;
  const seq = document.createElement("span");
  seq.className = "fseq";
  seq.textContent = `#${event.sequence}`;
  head.append(time, label, seq);
  row.append(head);
  const detail = document.createElement("div");
  detail.className = "fdetail";
  const payload = event.payload || {};
  const bits = [];
  if (event.companion_id) bits.push(`伙伴 ${event.companion_id}`);
  if (event.node_id) bits.push(`节点 ${event.node_id}`);
  if (payload.status) bits.push(`状态 ${payload.status}`);
  if (payload.user_message) bits.push(`“${payload.user_message}”`);
  if (payload.assistant_message) bits.push(`→ ${payload.assistant_message}`);
  if (payload.command_id) bits.push(`命令 ${payload.command_id}`);
  if (payload.reason) bits.push(`原因 ${payload.reason}`);
  if (payload.modality) bits.push(`通道 ${payload.modality}`);
  if (bits.length) detail.textContent = bits.join(" · ");
  else detail.textContent = "";
  row.append(detail);
  if (payload.status) row.classList.add(String(payload.status));
  if (type.includes("error") || type.includes("failed") || type.includes("discarded"))
    row.classList.add("ERROR");
  $("flow-empty").style.display = "none";
  $("flow-view").append(row);
  session.flowRows.push(event);
  if (session.flowRows.length > MAX_FLOW_ROWS) {
    const over = session.flowRows.length - MAX_FLOW_ROWS;
    session.flowRows.splice(0, over);
    for (let i = 0; i < over; i++) $("flow-view").firstChild?.remove();
  }
  $("flow-view").scrollTop = $("flow-view").scrollHeight;
}

function clearFlow() {
  session.flowRows = [];
  $("flow-view").replaceChildren();
  $("flow-empty").style.display = "";
}

function toggleFlowPause() {
  session.flowPaused = !session.flowPaused;
  $("flow-pause").textContent = session.flowPaused ? "继续" : "暂停";
}

// ------------------------------------------------------------ Streams

async function resetStreams() {
  session.logCursor = 0;
  session.flowCursor = 0;
  session.logRows = [];
  session.flowRows = [];
  session.firstFlow = true;
  $("log-view").replaceChildren();
  $("flow-view").replaceChildren();
  $("log-empty").style.display = "";
  $("flow-empty").style.display = "";
  await pollLogs();
  await pollFlow();
}

// ------------------------------------------------------------ Boot

$("connect").onclick = () => connect();
$("disconnect").onclick = () => disconnect();
$("logs-clear").onclick = () => clearLogs();
$("flow-clear").onclick = () => clearFlow();
$("flow-pause").onclick = () => toggleFlowPause();
$("level-filter").onchange = () => {
  session.logLevel = $("level-filter").value;
};
$("follow").onchange = () => {
  session.logFollow = $("follow").checked;
};
$("connection-dialog").addEventListener("close", () => {
  if (!session.connected && !session.token) {
    /* keep dialog for retry; nothing to do */
  }
});

// A click anywhere while disconnected opens the token dialog.
document.body.addEventListener("click", () => {
  if (!session.connected && !session.connecting) {
    const dialog = $("connection-dialog");
    if (!dialog.open) dialog.showModal();
  }
});

setInterval(pollLogs, LOG_POLL_MS);
setInterval(pollFlow, EVENT_POLL_MS);
restoreSession();
