"use strict";

const $ = (id) => document.getElementById(id);
const encoder = new TextEncoder();
const kinds = {
  episode: "经历",
  fact: "事实",
  preference: "偏好",
  relationship: "关系",
};

// getRandomValues also works on ordinary HTTP, unlike randomUUID.
function uuid() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const BROWSER_ID_KEY = "virtual_life_browser_id";

// This is a browser installation fallback, not a hardware fingerprint. Never store the token.
function persistentBrowserId() {
  const valid = (value) =>
    typeof value === "string" &&
    /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$/.test(value);
  let stored;
  try {
    stored = localStorage.getItem(BROWSER_ID_KEY);
  } catch {
    /* Try same-origin cookie. */
  }
  if (valid(stored)) return stored;
  let cookie;
  try {
    cookie = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(`${BROWSER_ID_KEY}=`))
      ?.slice(BROWSER_ID_KEY.length + 1);
  } catch {
    /* Cookie storage can also be disabled. */
  }
  const id = valid(cookie) ? cookie : `browser-${uuid()}`;
  try {
    localStorage.setItem(BROWSER_ID_KEY, id);
    if (localStorage.getItem(BROWSER_ID_KEY) === id) return id;
  } catch {
    /* Try cookie persistence before allowing an access. */
  }
  try {
    document.cookie = `${BROWSER_ID_KEY}=${id}; Path=/; Max-Age=31536000; SameSite=Strict`;
    if (
      document.cookie
        .split(";")
        .some((part) => part.trim() === `${BROWSER_ID_KEY}=${id}`)
    )
      return id;
  } catch {
    /* Stop rather than silently creating a device on every visit. */
  }
  throw new Error(
    "无法保存浏览器身份。请允许本站本地存储或 Cookie 后再接入；仍可只读查看资料。",
  );
}

function localDeviceKind() {
  const agent = navigator.userAgent || "";
  if (/Android/i.test(agent)) return "android";
  if (/iPhone|iPad|iPod|Mobile/i.test(agent)) return "other";
  return /Windows NT|Macintosh|X11|Linux/i.test(agent) ? "desktop" : "other";
}

async function browserId() {
  // Serialize first installation across tabs where Web Locks is available.
  if (navigator.locks?.request)
    return navigator.locks.request(BROWSER_ID_KEY, persistentBrowserId);
  return persistentBrowserId();
}

function releaseAccess(node, access, token) {
  if (!node || !access || !token) return;
  // Capture the old credentials/identity. No asynchronous completion touches the new session.
  try {
    return fetch(
      `/v1/nodes/${encodeURIComponent(node)}/access/${encodeURIComponent(access)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
        keepalive: true,
      },
    ).then(
      () => {},
      () => {},
    );
  } catch {
    /* Local output has already stopped; TTL is the fallback. */
  }
}

async function waitForRelease(releasing) {
  if (!releasing) return;
  let timer;
  try {
    await Promise.race([
      releasing,
      new Promise((resolve) => {
        timer = setTimeout(resolve, 1500);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function accessAlive() {
  return (
    Boolean(session.node && session.access) &&
    performance.now() < session.accessDeadline
  );
}

function acceptAccess(result, access, started, expectedNode = null) {
  const node = result?.node;
  const live = node?.accesses?.find((item) => item.access_id === access);
  const ttl = Date.parse(result?.expires_at) - Date.parse(live?.last_seen_at);
  if (
    result?.access_id !== access ||
    !node?.node_id ||
    (expectedNode && node.node_id !== expectedNode) ||
    !live ||
    !node.online ||
    !node.capabilities?.includes("text.display") ||
    !Number.isFinite(ttl) ||
    ttl <= 1000 ||
    live.expires_at !== result.expires_at
  ) {
    throw new Error("无法验证本页设备访问或安全期限，请重新接入");
  }
  session.node = node.node_id;
  session.access = access;
  session.accessDeadline = started + ttl - 1000;
  session.deadline = Math.min(session.deadline, session.accessDeadline);
}

function deviceOptions(nodes) {
  const select = $("local-device");
  const chosen = select.value;
  select.replaceChildren(new Option("自动识别本机设备", ""));
  for (const node of nodes.filter((item) => item.kind !== "web")) {
    select.add(
      new Option(
        `${node.label || node.node_id}（${node.kind} · ${node.node_id}）`,
        node.node_id,
      ),
    );
  }
  select.value = [...select.options].some((option) => option.value === chosen)
    ? chosen
    : "";
}

async function loadDeviceOptions() {
  const generation = session.generation;
  const nodes = await api("/v1/nodes");
  if (current(generation)) deviceOptions(nodes);
}

const session = {
  token: "",
  connecting: false,
  companion: "",
  generation: 0,
  node: "",
  access: "",
  accessDeadline: 0,
  releaseBarrier: null,
  active: false,
  activating: false,
  deadline: 0,
  serverClock: null,
  epoch: null,
  history: [],
  conversations: new Map(),
  logRequest: 0,
  logRunning: false,
  logs: [],
  logCursor: 0,
  logMore: false,
  logFilters: {},
  personaRunning: false,
  // Unresolved commands remain in page memory across selection and disconnect.
  commands: new Map(),
  completed: null,
  offset: 0,
  total: 0,
  memoryRequest: 0,
  memoryRunning: false,
  embeddingRunning: false,
  embeddingsEnabled: false,
  recallBusy: false,
  graphEntities: [],
  automationEnabled: false,
  capabilities: null,
  configured: false,
  busy: false,
  tickBusy: false,
  // personality editor
  personaRevision: null,
  personaDirty: false,
  personaLoaded: false,
  personaLoadRequest: 0,
  personaLoading: false,
  personaSaving: false,
  evolutionRequest: 0,
  background: {
    request: 0,
    dirty: false,
    loaded: false,
    running: false,
    saving: false,
    policy: null,
    revision: null,
    attention: null,
    inner: null,
  },
  backgroundOutput: {
    cursor: null,
    running: false,
    identity: null,
    seen: new Set(),
  },
  // day timeline
  dayEvents: [],
  dayCursor: 0,
  dayRequest: 0,
  dayRunning: false,
  // long-connection log window
  connEvents: [],
  connCursor: 0,
  connRequest: 0,
  connRunning: false,
};

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}

function current(generation) {
  return Boolean(session.token) && generation === session.generation;
}

function validVersion(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function versions(...values) {
  if (values.some((value) => !validVersion(value))) {
    stop("服务器版本超出安全范围，已停止输出");
    notice("服务器版本超出浏览器安全整数范围，请检查服务器数据。", true);
    throw new Error("服务器版本超出安全范围");
  }
}

function snapshot(value) {
  versions(value.epoch, value.revision);
  return value;
}

function authorized() {
  return (
    session.active &&
    !document.hidden &&
    accessAlive() &&
    performance.now() < session.deadline
  );
}

function lastCommand() {
  return session.commands.get(session.companion);
}

function conversation(id = session.companion) {
  if (!session.conversations.has(id)) {
    session.conversations.set(id, {
      history: [],
      messages: [],
      turns: [],
      draft: "",
    });
  }
  return session.conversations.get(id);
}

function buttons() {
  const connected = Boolean(session.token);
  const selected = connected && Boolean(session.companion);
  const pending = lastCommand();
  for (const id of [
    "persona-import",
    "persona-fixture",
    "chat-copy",
    "chat-export",
  ])
    $(id).disabled =
      !selected ||
      (id.startsWith("persona-") &&
        (session.personaRunning ||
          session.personaLoading ||
          session.personaSaving ||
          session.busy ||
          Boolean(pending?.unresolved)));
  $("chat-clear").disabled =
    !selected || session.busy || Boolean(pending?.unresolved);
  $("logs-refresh").disabled = !selected || session.logRunning;
  $("logs-more").disabled = !selected || session.logRunning || !session.logMore;
  $("logs-export").disabled = !selected || !session.logs.length;
  $("chat-status").textContent = session.busy
    ? "正在接收回复，请稍候…"
    : pending?.unresolved
      ? "回复结果尚未确认，请查询原回执。"
      : "";
  $("connect").disabled = connected || session.connecting;
  $("disconnect").disabled = !connected;
  $("companions").disabled = !connected;
  $("create").disabled = !connected;
  for (const id of [
    "refresh",
    "preview",
    "memory-refresh",
    "graph-search",
    "emotion-refresh",
    "evolution-refresh",
    "evolution-list",
    "evolution-history",
  ]) {
    $(id).disabled = !selected;
  }
  $("activate").disabled =
    !selected ||
    session.busy ||
    session.active ||
    session.activating ||
    document.hidden;
  $("access-stop").disabled = !session.active && !session.activating;
  $("local-device").disabled = !connected;
  $("local-device-refresh").disabled = !connected;
  $("send").disabled =
    !selected ||
    !session.configured ||
    !authorized() ||
    session.personaRunning ||
    session.busy ||
    Boolean(pending?.unresolved);
  $("receipt").disabled = !selected || !pending || session.busy;
  $("save").disabled = !selected || !session.completed;
  $("memory-run").disabled =
    !selected || !session.automationEnabled || session.memoryRunning;
  $("embedding-run").disabled =
    !selected || !session.embeddingsEnabled || session.embeddingRunning;
  $("recall").disabled = !selected || session.recallBusy;
  $("semantic-recall").disabled = !selected || !session.embeddingsEnabled;
  $("graph-alias-save").disabled = !selected || !$("graph-entity").value;
  $("previous").disabled = !selected || session.offset === 0;
  $("next").disabled = !selected || session.offset + 20 >= session.total;
  // personality editor
  const personaPending = session.personaLoading || session.personaSaving;
  $("persona-fields").disabled =
    !selected || !session.personaLoaded || personaPending;
  $("persona-edit").disabled = !selected || personaPending;
  $("persona-preview").disabled = !selected || personaPending;
  $("persona-revert").disabled =
    !selected || !session.personaLoaded || session.busy || personaPending;
  $("persona-save").disabled =
    !selected ||
    !session.personaDirty ||
    personaPending ||
    session.busy ||
    Boolean(pending?.unresolved);
  $("state-save").disabled = !selected || session.busy || personaPending;
  $("persona-card-sui").disabled = $("persona-import").disabled;
  // day timeline
  $("days-refresh").disabled = !connected || session.dayRunning;
  $("days-more").disabled =
    !connected || session.dayRunning || !session.dayEvents.length;
  $("days-export").disabled = !connected || !session.dayEvents.length;
  $("conn-refresh").disabled = !connected || session.connRunning;
  $("conn-more").disabled =
    !connected || session.connRunning || !session.connEvents.length;
  $("conn-export").disabled = !connected || !session.connEvents.length;
  $("devices-refresh").disabled = !connected;
  backgroundButtons();
  updateShellStatus();
}

function stop(reason) {
  session.background.request++;
  session.background.running = false;
  session.background.saving = false;
  session.backgroundOutput = {
    cursor: null,
    running: false,
    identity: null,
    seen: new Set(),
  };
  removeStream();
  const old = {
    node: session.node,
    access: session.access,
    token: session.token,
  };
  session.node = "";
  session.access = "";
  session.accessDeadline = 0;
  session.active = false;
  session.activating = false;
  session.deadline = 0;
  session.serverClock = null;
  session.epoch = null;
  session.generation++;
  session.logRequest++;
  session.logRunning = false;
  session.busy = false;
  session.tickBusy = false;
  session.memoryRunning = false;
  session.embeddingRunning = false;
  session.recallBusy = false;
  $("ownership").textContent = reason;
  buttons();
  if (old.node && old.access)
    session.releaseBarrier = releaseAccess(old.node, old.access, old.token);
}

function clearCompanionData() {
  resetBackground();
  session.history = session.companion ? conversation().history : [];
  session.logs = [];
  session.logCursor = 0;
  session.logMore = false;
  $("logs-list").replaceChildren();
  $("logs-summary").textContent = "请选择刷新以加载日志";
  session.completed = null;
  session.offset = 0;
  session.total = 0;
  session.memoryRequest++;
  session.memoryRunning = false;
  session.automationEnabled = false;
  session.embeddingsEnabled = false;
  session.graphEntities = [];
  $("graph-entity").replaceChildren();
  $("graph-aliases").value = "";
  $("semantic-recall").checked = false;
  $("messages").replaceChildren();
  $("turns")?.replaceChildren();
  $("memories").replaceChildren();
  for (const id of [
    "stats",
    "stats-raw",
    "page",
    "recall-result",
    "usage",
    "save-content",
    "message",
    "automation-status",
    "automation-jobs",
    "memory-summaries",
    "memory-relations",
  ]) {
    if ($(id).tagName === "TEXTAREA") {
      $(id).value = "";
    } else {
      $(id).textContent = "";
    }
  }
  $("state").textContent = "请选择伙伴";
  $("emotion").textContent = "请选择伙伴";
  $("emotion-experiences").replaceChildren();
  for (const id of ["embedding-status", "embedding-jobs", "graph-result"])
    $(id).textContent = "";
  $("personality").textContent = "请选择伙伴";
  $("prompt").textContent = "尚无预览";
  $("model").textContent = "请选择伙伴";
  session.personaRevision = null;
  session.personaDirty = false;
  session.personaLoaded = false;
  session.personaLoadRequest++;
  session.personaLoading = false;
  session.personaSaving = false;
  $("persona-meta").textContent = "请选择伙伴";
  $("persona-prompt").textContent = "尚无预览";
  $("persona-status").textContent = "";
  $("persona-raw").textContent = "尚无数据";
  $("persona-fields").disabled = true;
  $("evolution-proposals").replaceChildren();
  $("evolution-history-list").replaceChildren();
  $("evolution-status").textContent = "";
  session.evolutionRequest++;
  if (session.companion) {
    for (const item of conversation().messages)
      renderBubble(item.role, item.content, item.proactive);
    $("message").value = conversation().draft;
    renderTurns();
  }
}

function showModel() {
  const model = session.capabilities?.dialogue;
  $("model").textContent = session.configured
    ? `${model.model} · 输出上限 ${model.max_output_tokens} token`
    : "模型尚未配置；可查看记忆与预览提示。";
}

// Long enough for a full non-streaming generation; the server caps the model call
// itself, so this only needs to exceed the provider timeout.
const DIALOGUE_TIMEOUT_MS = 120000;
const DEFAULT_TIMEOUT_MS = 20000;

async function api(
  url,
  body,
  token = session.token,
  method = body === undefined ? "GET" : "POST",
  timeoutMs = DEFAULT_TIMEOUT_MS,
) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (error) {
    if (error.name === "AbortError") {
      const timeout = new Error("请求超时，未收到服务器响应");
      timeout.status = 0;
      throw timeout;
    }
    const offline = new Error("网络请求失败，请检查连接");
    offline.status = 0;
    throw offline;
  } finally {
    clearTimeout(timer);
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      // A proxy or crash can answer with HTML. Surface the status, not a parser error.
      const malformed = new Error(
        `服务器返回了非 JSON 响应（HTTP ${response.status}）`,
      );
      malformed.status = response.status;
      throw malformed;
    }
  }
  if (!response.ok) {
    const detail = (data && (data.error || data.detail)) || data;
    const message =
      typeof detail === "string"
        ? detail
        : detail?.message || JSON.stringify(detail);
    const error = new Error(message || `请求失败（HTTP ${response.status}）`);
    error.code = (data && (data.error?.code || data.code)) || undefined;
    error.status = response.status;
    error.structured = Boolean(data?.error?.code);
    throw error;
  }
  return data;
}

function path(suffix = "", id = session.companion) {
  return `/v1/companions/${encodeURIComponent(id)}${suffix}`;
}

function showJson(id, data) {
  $(id).textContent = JSON.stringify(data, null, 2);
}

async function companions() {
  const generation = session.generation;
  const rows = await api("/v1/companions");
  if (!current(generation)) {
    return;
  }
  $("companions").replaceChildren(new Option("请选择", ""));
  for (const item of rows) {
    $("companions").add(new Option(item.name, item.companion_id));
  }
  $("companions").value = session.companion;
  const chosen = $("days-companion").value;
  $("days-companion").replaceChildren(new Option("全部伙伴", ""));
  for (const item of rows)
    $("days-companion").add(new Option(item.name, item.companion_id));
  $("days-companion").value = chosen;
}

async function info() {
  const generation = session.generation;
  const id = session.companion;
  const [state, profile, mood, emotion] = await Promise.all([
    api(path("", id)),
    api(path("/personality", id)),
    api(path("/personality/state", id)),
    api(path("/emotion", id)),
  ]);
  if (!current(generation) || id !== session.companion) {
    return;
  }
  snapshot(state);
  versions(profile.revision, mood.revision);
  showJson("state", {
    名字: state.name,
    模式: state.interaction_mode,
    活动: state.activity,
    当前设备: state.active_node_id,
    短期状态: mood.state,
    状态已过期: mood.expired,
  });
  showJson("personality", profile.definition);
  showEmotion(emotion);
}

function showEmotion(emotion) {
  versions(emotion.revision);
  const labels = {
    calm: "平静",
    cheerful: "愉快",
    curious: "好奇",
    concerned: "关切",
    tired: "疲惫",
  };
  $("emotion").textContent = emotion.enabled
    ? `${labels[emotion.effective_state.mood] || "平静"} · 精力 ${Math.round(emotion.effective_state.energy * 100)}%` +
      (emotion.quiet_requested ? " · 安静陪伴" : "") +
      (emotion.truncated ? " · 近期经历较多" : "")
    : "自动情感未启用";
}

async function emotionExperiences() {
  const generation = session.generation;
  const id = session.companion;
  const valid = () => current(generation) && id === session.companion;
  const page = await api(path("/emotion/experiences?limit=50", id));
  if (!valid()) return;
  const panel = $("emotion-experiences");
  panel.replaceChildren();
  const reasons = {
    no_explicit_cue: "普通交流",
    explicit_appreciation: "收到明确肯定",
    explicit_distress: "回应难过或疲惫",
    explicit_question: "共同探索",
    quiet_request: "安静陪伴请求",
  };
  const states = {
    active: "有效",
    corrected: "已调整理解",
    invalidated: "已完整撤回",
    forgotten: "来源已遗忘",
  };
  const attributions = {
    self: "自己",
    other: "他人",
    situation: "情境",
    unspecified: "不确定",
  };
  const kinds = {
    direction: "方向不贴切",
    intensity: "反应过重",
    attribution: "归因不对",
  };
  for (const item of page.items) {
    const element = document.createElement("div");
    element.className = "emotion-experience";
    const heading = document.createElement("p");
    heading.textContent =
      `${item.status === "forgotten" ? "来源已遗忘" : reasons[item.reason] || "交流（旧版或未识别规则）"} · ${item.created_at}` +
      ` · ${states[item.status] || "历史记录"}` +
      (item.resonance_valid ? " · 想起共同经历" : "");
    element.append(heading);
    if (item.status !== "forgotten") {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "规则解释与贡献";
      const explanation = document.createElement("pre");
      explanation.textContent = JSON.stringify(
        {
          规则原因: reasons[item.reason] || item.reason || "旧版记录未提供",
          评价版本: item.appraisal_version || "legacy_v1",
          评价向量: item.appraisal || "旧版记录未提供",
          规则解释不确定性: item.uncertainty ?? 1,
          归因线索: attributions[item.attribution] || "不确定",
          原始贡献: item.original_delta || "旧版记录未提供",
          当前贡献: item.delta,
          共鸣贡献: item.resonance_delta,
          最近澄清: item.correction || "无",
        },
        null,
        2,
      );
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent =
        "这些值解释本地规则如何形成反应；归因是线索解释，不确定性不是用户心理测量准确率。";
      details.append(summary, hint, explanation);
      element.append(details);
    }
    if (["active", "corrected"].includes(item.status)) {
      const form = document.createElement("form");
      form.className = "emotion-correction";
      const kindLabel = document.createElement("label");
      kindLabel.textContent = "调整方式";
      const kind = document.createElement("select");
      for (const [value, label] of Object.entries(kinds))
        kind.add(new Option(label, value));
      kindLabel.append(kind);
      const weightLabel = document.createElement("label");
      weightLabel.textContent = "保留当前强度";
      const weight = document.createElement("select");
      for (const value of [0.25, 0.5, 0.75, 0])
        weight.add(new Option(`${value * 100}%`, String(value)));
      weight.value = "0.5";
      weightLabel.append(weight);
      const attributionLabel = document.createElement("label");
      attributionLabel.textContent = "归因线索";
      const attribution = document.createElement("select");
      for (const [value, label] of Object.entries(attributions))
        attribution.add(new Option(label, value));
      attribution.value =
        item.attribution in attributions ? item.attribution : "unspecified";
      attributionLabel.append(attribution);
      const description = document.createElement("p");
      description.className = "hint";
      const updateOptions = () => {
        weightLabel.hidden = kind.value !== "intensity";
        attributionLabel.hidden = kind.value !== "attribution";
        description.textContent = {
          direction:
            "撤去即时情绪影响，不把反应翻转；已发生的回复精力消耗保留。",
          intensity: "按所选比例减弱当前反应，默认保留 50%。",
          attribution: "只调整归因解释，不创造新的正负体验。",
        }[kind.value];
      };
      kind.onchange = updateOptions;
      updateOptions();
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.textContent = "调整理解";
      const invalidate = document.createElement("button");
      invalidate.type = "button";
      invalidate.textContent = "完整撤回这次影响";
      const apply = async (full) => {
        if (!valid()) return;
        const controls = [...form.querySelectorAll("button, select")];
        controls.forEach((control) => {
          control.disabled = true;
        });
        try {
          const snapshot = await api(path("/emotion", id));
          if (!valid()) return;
          const body = {
            command_id: uuid(),
            expected_revision: snapshot.revision,
          };
          if (!full) {
            body.kind = kind.value;
            if (kind.value === "intensity")
              body.retained_weight = Number(weight.value);
            if (kind.value === "attribution")
              body.attribution = attribution.value;
          }
          await api(
            path(
              `/emotion/experiences/${encodeURIComponent(item.event_id)}/${full ? "invalidate" : "correct"}`,
              id,
            ),
            body,
          );
          if (!valid()) return;
          await info();
          if (!valid()) return;
          await emotionExperiences();
          if (valid())
            notice(
              full
                ? "这次影响已完整撤回，原始对话仍保留。"
                : "已调整对这段经历的理解，原始对话仍保留。",
            );
        } catch (error) {
          if (valid()) notice(error.message, true);
        } finally {
          controls.forEach((control) => {
            control.disabled = false;
          });
        }
      };
      form.onsubmit = (event) => {
        event.preventDefault();
        void apply(false);
      };
      invalidate.onclick = () => {
        void apply(true);
      };
      form.append(
        kindLabel,
        weightLabel,
        attributionLabel,
        description,
        submit,
        invalidate,
      );
      element.append(form);
    }
    panel.append(element);
  }
  if (!page.items.length) panel.textContent = "尚无反应经历";
  if (page.has_more)
    panel.append(
      document.createTextNode("显示最早 50 项；其余经历可通过 API 分页查看。"),
    );
}

const RETENTION_LABELS = {
  auto: "自动判断",
  permanent: "永久",
  stable: "稳定认识",
  significant_episode: "重要经历",
  routine_episode: "日常经历",
};

function memoryDate(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

// Editing stays opt-in per record so a read-only sweep stays cheap. Corrections
// are only offered for stable memories, which is where the server accepts them.
function memoryEditor(record, refresh) {
  const form = document.createElement("form");
  form.className = "memory-editor";
  const grid = document.createElement("div");
  grid.className = "settings-grid";

  const importance = document.createElement("label");
  importance.textContent = "重要度 ";
  const importanceInput = document.createElement("input");
  importanceInput.type = "number";
  importanceInput.min = "0";
  importanceInput.max = "1";
  importanceInput.step = "0.05";
  importanceInput.value = String(record.importance ?? 0.5);
  importance.append(importanceInput);

  const confidence = document.createElement("label");
  confidence.textContent = "置信度 ";
  const confidenceInput = document.createElement("input");
  confidenceInput.type = "number";
  confidenceInput.min = "0";
  confidenceInput.max = "1";
  confidenceInput.step = "0.05";
  confidenceInput.value = String(record.confidence ?? 1);
  confidence.append(confidenceInput);

  const retention = document.createElement("label");
  retention.textContent = "保留策略 ";
  const retentionInput = document.createElement("select");
  for (const [value, label] of Object.entries(RETENTION_LABELS)) {
    retentionInput.add(new Option(label, value));
  }
  retentionInput.value = record.retention_policy || "auto";
  retention.append(retentionInput);

  const pinned = document.createElement("label");
  pinned.className = "check";
  const pinnedInput = document.createElement("input");
  pinnedInput.type = "checkbox";
  pinnedInput.checked = Boolean(record.pinned);
  pinned.append(pinnedInput, document.createTextNode("固定"));

  const confirmed = document.createElement("label");
  confirmed.className = "check";
  const confirmedInput = document.createElement("input");
  confirmedInput.type = "checkbox";
  confirmedInput.checked = Boolean(record.confirmed);
  confirmed.append(confirmedInput, document.createTextNode("已确认"));

  grid.append(importance, confidence, retention, pinned, confirmed);

  const tags = document.createElement("label");
  tags.textContent = "标签（逗号分隔）";
  const tagsInput = document.createElement("input");
  tagsInput.maxLength = 2048;
  tagsInput.value = (record.tags || []).join("，");
  tags.append(tagsInput);

  const save = document.createElement("button");
  save.type = "submit";
  save.textContent = "保存元数据";

  const correct = document.createElement("details");
  const correctSummary = document.createElement("summary");
  correctSummary.textContent = "更正这条认识";
  const correctHint = document.createElement("p");
  correctHint.className = "hint";
  correctHint.textContent =
    "更正会生成新版本并把旧版本置为已替代，需要一个属于该伙伴的真实来源事件。";
  const correctContent = document.createElement("textarea");
  correctContent.rows = 3;
  correctContent.maxLength = 32768;
  correctContent.placeholder = "更正后的正文";
  const correctSource = document.createElement("input");
  correctSource.maxLength = 128;
  correctSource.placeholder = "来源事件 ID";
  correctSource.value = (record.source_event_ids || [])[0] || "";
  const correctSend = document.createElement("button");
  correctSend.type = "button";
  correctSend.textContent = "提交更正";

  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "删除这条记忆";

  grid.append(save);
  form.append(grid, tags, remove);
  if (record.layer === "semantic")
    correct.append(
      correctSummary,
      correctHint,
      correctContent,
      correctSource,
      correctSend,
    );
  form.append(correct);

  const base = path(
    `/memories/${encodeURIComponent(record.memory_id)}`,
    record.companion_id || session.companion,
  );

  form.onsubmit = action(async (context) => {
    save.disabled = true;
    try {
      const tagsValue = tagsInput.value
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean);
      if (tagsValue.length > 32) throw new Error("标签最多 32 个");
      for (const item of tagsValue)
        if (item.length > 64) throw new Error("单个标签不能超过 64 个字符");
      await api(
        base,
        {
          command_id: uuid(),
          expected_revision: record.revision,
          tags: tagsValue,
          importance: readRatio("重要度", importanceInput.value),
          confidence: readRatio("置信度", confidenceInput.value),
          confirmed: confirmedInput.checked,
          pinned: pinnedInput.checked,
          retention_policy: retentionInput.value,
        },
        session.token,
        "PATCH",
      );
      if (current(context.generation)) {
        notice("记忆元数据已更新。");
        await refresh();
      }
    } finally {
      if (current(context.generation)) save.disabled = false;
    }
  });

  correctSend.onclick = action(async (context) => {
    correctSend.disabled = true;
    try {
      const content = correctContent.value.trim();
      const source = correctSource.value.trim();
      if (!content) throw new Error("请填写更正后的正文");
      if (!source) throw new Error("请填写来源事件 ID");
      const result = await api(`${base}/corrections`, {
        command_id: uuid(),
        expected_revision: record.revision,
        content,
        source_event_id: source,
      });
      if (current(context.generation)) {
        notice(`更正已保存为新版本 ${result.revision}，旧版本已置为已替代。`);
        await refresh();
      }
    } finally {
      if (current(context.generation)) correctSend.disabled = false;
    }
  });

  remove.onclick = action(async (context) => {
    remove.disabled = true;
    try {
      if (!confirm(`确定删除这条记忆吗？\n\n${record.content.slice(0, 120)}`))
        return;
      await api(base, undefined, session.token, "DELETE");
      if (current(context.generation)) {
        notice("记忆已删除；来源已留墓碑，自动整理不会再次生成它。");
        await refresh();
      }
    } finally {
      if (current(context.generation)) remove.disabled = false;
    }
  });

  return form;
}

function memoryElement(record, refresh) {
  const article = document.createElement("article");
  article.className = "memory";
  const title = document.createElement("strong");
  title.textContent =
    `${kinds[record.kind] || record.kind} · ${record.active ? "当前版本" : "已替代"}` +
    `${record.pinned ? " · 固定" : ""}${record.confirmed ? " · 已确认" : ""}`;
  const content = document.createElement("div");
  content.textContent = record.content;
  const source = document.createElement("small");
  const retention =
    record.effective_retention_policy || record.retention_policy || "auto";
  source.textContent =
    `${record.source_event_ids.length} 个来源 · ${RETENTION_LABELS[retention] || "其他保留策略"}` +
    ` · 更新于 ${memoryDate(record.updated_at)}`;
  const detail = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "记录详情（来源、时间与完整记录）";
  const raw = document.createElement("pre");
  raw.textContent = JSON.stringify(record, null, 2);
  detail.append(summary, raw);
  article.append(title, content, source, detail);
  if (record.active) {
    const editor = document.createElement("details");
    const editorSummary = document.createElement("summary");
    editorSummary.textContent = "编辑这条记忆";
    editor.append(editorSummary, memoryEditor(record, refresh));
    article.append(editor);
  }
  return article;
}

async function memories() {
  const generation = session.generation;
  const id = session.companion;
  const offset = session.offset;
  const request = ++session.memoryRequest;
  let page;
  let stats;
  let automation;
  let jobs;
  let summaries;
  let graph;
  let embeddings;
  let embeddingJobs;
  try {
    [
      page,
      stats,
      automation,
      jobs,
      summaries,
      graph,
      embeddings,
      embeddingJobs,
    ] = await Promise.all([
      api(path(`/memories?limit=20&offset=${offset}`, id)),
      api(path("/memories/stats", id)),
      api(path("/memories/automation", id)),
      api(path("/memories/automation/jobs?limit=5", id)),
      api(path("/memories/summaries?limit=5", id)),
      api(path("/memories/knowledge-graph?limit=12", id)),
      api(path("/memories/embeddings", id)),
      api(path("/memories/embeddings/jobs?limit=5", id)),
    ]);
  } catch (error) {
    if (
      current(generation) &&
      request === session.memoryRequest &&
      offset === session.offset
    ) {
      throw error;
    }
    return;
  }
  if (
    !current(generation) ||
    request !== session.memoryRequest ||
    offset !== session.offset
  ) {
    return;
  }
  session.total = page.total;
  session.automationEnabled =
    automation.enabled && automation.provider_configured;
  const queued = automation.queue_states;
  const jobStates = automation.job_states;
  $("automation-status").textContent = automation.enabled
    ? `每 ${automation.batch_size} 轮或等待 ${automation.max_wait_seconds} 秒自动整理。` +
      `待处理 ${queued.pending || 0}，处理中 ${jobStates.running || 0}，` +
      `待确认 ${jobStates.needs_review || 0}，失败 ${jobStates.failed || 0}，` +
      `执行结果待核对 ${jobStates.unknown || 0}。` +
      (automation.provider_configured ? "" : "模型未配置，仅保留原始对话记忆。")
    : "自动积累已关闭，已有记忆仍可召回。";
  showJson("automation-jobs", jobs);
  session.embeddingsEnabled = embeddings.enabled && embeddings.configured;
  $("embedding-status").textContent = session.embeddingsEnabled
    ? `有效索引 ${embeddings.indexed_records} 条，待索引 ${embeddings.pending_records} 条，` +
      `超长跳过 ${embeddings.skipped_oversized_records} 条，结果待核对 ${embeddings.jobs.unknown || 0} 项。`
    : "向量模型未启用，使用本地词法、摘要和图谱召回。";
  if (!session.embeddingsEnabled) $("semantic-recall").checked = false;
  showJson("embedding-jobs", embeddingJobs);
  $("graph-hops").max = String(
    session.capabilities?.memory?.graph_max_hops || 2,
  );
  $("memory-summaries").replaceChildren(
    ...summaries.items.map((item) => {
      const article = document.createElement("article");
      article.className = "memory";
      const content = document.createElement("p");
      content.textContent = item.content;
      const source = document.createElement("small");
      source.textContent = `${item.source_event_ids.length} 次对话 · ${memoryDate(item.created_at)} · 派生摘要`;
      article.append(content, source);
      return article;
    }),
  );
  if (!summaries.items.length)
    $("memory-summaries").textContent = "暂无有效摘要";
  renderGraph(graph);
  /* Relation display and entity metadata share the same source snapshot. */
  $("memory-relations").replaceChildren(
    ...graph.relations.map((item) => {
      const line = document.createElement("p");
      line.textContent = `${item.subject} → ${item.predicate} → ${item.object}（${item.evidence_count || 1} 份有效证据）`;
      return line;
    }),
  );
  if (!graph.relations.length)
    $("memory-relations").textContent = "暂无有来源支持的关系";
  const reload = () => memories();
  $("memories").replaceChildren(
    ...page.items.map((record) => memoryElement(record, reload)),
  );
  if (!page.items.length) {
    $("memories").textContent = "暂无记忆";
  }
  const quota = stats.token_quota;
  $("stats").textContent =
    `共 ${stats.total_records} 条，当前版本 ${stats.active_records} 条，` +
    `固定 ${stats.pinned_records} 条；内容 ${stats.content_utf8_bytes} 字节。\n` +
    `模型 token 配额（UTC ${quota.day}，全部伙伴共享）：每日 ${quota.daily_token_limit}，` +
    `已预留 ${quota.reserved_tokens}，已结算 ${quota.spent_tokens}，剩余 ${quota.remaining_tokens}。\n` +
    `此伙伴：已预留 ${quota.companion_reserved_tokens}，已结算 ${quota.companion_spent_tokens}。` +
    (quota.over_budget
      ? `\n实际用量超出预算 ${quota.over_budget_tokens} token。`
      : "");
  showJson("stats-raw", stats);
  $("page").textContent =
    `${page.total ? offset + 1 : 0}–${offset + page.items.length} / ${page.total}`;
  buttons();
}

function renderGraph(graph) {
  const previous = $("graph-entity").value;
  session.graphEntities = graph.entities;
  $("graph-entity").replaceChildren(new Option("请选择实体", ""));
  for (const entity of graph.entities)
    $("graph-entity").add(new Option(entity.name, entity.entity_id));
  if (graph.entities.some((entity) => entity.entity_id === previous))
    $("graph-entity").value = previous;
  graphAliasSelection();
  showJson("graph-result", graph);
}

function graphAliasSelection() {
  const entity = session.graphEntities.find(
    (item) => item.entity_id === $("graph-entity").value,
  );
  $("graph-aliases").value = entity ? entity.aliases.join("\n") : "";
  buttons();
}

function serverNow() {
  if (!session.serverClock) {
    return NaN;
  }
  return (
    session.serverClock.time + performance.now() - session.serverClock.started
  );
}

function serverLeaseValid(state) {
  const expiry = Date.parse(state.lease_expires_at);
  const now = serverNow();
  return Number.isFinite(expiry) && Number.isFinite(now) && expiry > now;
}

function effectiveState(mood) {
  return JSON.stringify({
    mood: mood.state.mood,
    energy: mood.state.energy,
    social_battery: mood.state.social_battery,
    expired: mood.expired,
    expires_at: mood.expires_at,
  });
}

function stateStillEffective(mood) {
  if (mood.expired || mood.expires_at === null) {
    return true;
  }
  const expiry = Date.parse(mood.expires_at);
  return Number.isFinite(expiry) && expiry > serverNow();
}

async function lease(generation) {
  if (!accessAlive()) throw new Error("本页设备访问已过期，请重新接入");
  const access = session.access;
  const node = session.node;
  const started = performance.now();
  const id = session.companion;
  const epoch = session.epoch;
  const result = await api(path("/lease", id), { node_id: node, epoch });
  if (
    !current(generation) ||
    id !== session.companion ||
    access !== session.access ||
    node !== session.node ||
    (!session.active && !session.activating) ||
    document.hidden
  ) {
    return;
  }
  if (!accessAlive()) throw new Error("本页设备访问已过期，请重新接入");
  versions(result.epoch);
  if (
    result.node_id !== session.node ||
    result.companion_id !== id ||
    result.epoch !== epoch
  ) {
    throw new Error("租约身份不匹配");
  }
  const serverTime = Date.parse(result.server_time);
  const ttl = Math.min(
    result.ttl_seconds * 1000,
    Date.parse(result.expires_at) - serverTime,
  );
  if (!Number.isFinite(ttl) || ttl <= 1000) {
    throw new Error("无法确定安全租约期限");
  }
  session.deadline = Math.min(started + ttl - 1000, session.accessDeadline);
  // Starting the clock before the request conservatively overestimates server time.
  session.serverClock = { time: serverTime, started };
  buttons();
}

async function activate() {
  stop("正在接入…");
  session.busy = true;
  session.activating = true;
  buttons();
  const generation = session.generation;
  const id = session.companion;
  try {
    const releasing = session.releaseBarrier;
    await waitForRelease(releasing);
    if (!current(generation) || id !== session.companion || document.hidden)
      return;
    const browser = await browserId();
    if (!current(generation) || id !== session.companion || document.hidden)
      return;
    const access = `access-${uuid()}`;
    const token = session.token;
    const started = performance.now();
    const binding = $("local-device").value;
    const result = await api(
      "/v1/nodes/access/web",
      {
        browser_id: browser,
        access_id: access,
        label: "本机设备",
        kind: localDeviceKind(),
        ...(binding ? { bind_node_id: binding } : {}),
      },
      token,
    );
    if (!current(generation) || id !== session.companion || document.hidden) {
      releaseAccess(result?.node?.node_id, access, token);
      return;
    }
    // Install only the server's canonical device ID, never a page-generated node ID.
    try {
      acceptAccess(result, access, started);
    } catch (error) {
      releaseAccess(result?.node?.node_id, access, token);
      throw error;
    }
    let state = await api(path("", id));
    if (!current(generation) || document.hidden) {
      return;
    }
    snapshot(state);
    if (state.active_node_id !== session.node) {
      let transfer = await api(path("/transfers", id), {
        command_id: uuid(),
        target_node_id: session.node,
        expected_revision: state.revision,
        reason: "manual",
      });
      if (!current(generation) || document.hidden) {
        return;
      }
      transfer = await api(
        `/v1/transfers/${encodeURIComponent(transfer.transfer_id)}/ack`,
        {
          node_id: session.node,
          phase: "ready",
        },
      );
      const until = performance.now() + 35000;
      while (
        transfer.status === "prepared" &&
        current(generation) &&
        !document.hidden &&
        performance.now() < until
      ) {
        $("ownership").textContent = "等待原设备释放或租约到期…";
        await new Promise((resolve) => setTimeout(resolve, 1000));
        if (!current(generation) || document.hidden) {
          return;
        }
        transfer = await api(
          `/v1/transfers/${encodeURIComponent(transfer.transfer_id)}`,
        );
      }
      if (transfer.status !== "committed") {
        throw new Error("迁移尚未完成或已终止，请查看原设备后重新接入");
      }
      state = await api(path("", id));
    }
    if (!current(generation) || document.hidden) {
      return;
    }
    snapshot(state);
    if (state.active_node_id !== session.node) {
      throw new Error("当前输出属于其他设备");
    }
    session.epoch = state.epoch;
    await lease(generation);
    if (!current(generation) || document.hidden) {
      return;
    }
    session.active = true;
    session.activating = false;
    if (!authorized()) {
      throw new Error("接入时租约已过期，请重新接入");
    }
    $("ownership").textContent = "此页已接入";
    // Each new access starts from a server baseline; old remarks are never replayed.
    void pollBackgroundOutput();
    void refreshBackground();
    notice(
      lastCommand()?.unresolved
        ? "已接入。上次命令尚未确认，请先查询原回执。"
        : "可以开始对话。",
    );
  } catch (error) {
    if (current(generation)) {
      stop("接入失败");
      const message =
        {
          device_access_busy:
            "本机设备正由另一个页面使用。请先在原页停止，或等待访问到期后再接入。",
          device_ambiguous:
            "有多个可能的本机设备，请在“本机设备”中明确选择已有设备。",
          node_access_expired: "本页设备访问已过期，请重新接入。",
        }[error.code] || error.message;
      notice(message, true);
    }
  } finally {
    if (current(generation)) {
      session.activating = false;
      session.busy = false;
      buttons();
    }
  }
}

function validMessage(content) {
  return (
    typeof content === "string" &&
    content.trim().length > 0 &&
    Array.from(content).length <= 4096
  );
}

function historyTail() {
  const tail = [];
  // Stop at the first invalid entry rather than making holes in recent history.
  for (
    let index = session.history.length - 1;
    index >= 0 && tail.length < 20;
    index--
  ) {
    const item = session.history[index];
    if (
      !validMessage(item.content) ||
      !["user", "assistant"].includes(item.role)
    ) {
      break;
    }
    tail.unshift(item);
  }
  return tail;
}

function requestBody() {
  const message = $("message").value;
  if (!validMessage(message)) {
    throw new Error("请输入非空消息，最多 4096 个字符。");
  }
  const body = {
    user_message: message,
    user_name: "你",
    user_initiated: true,
    audio_requested: false,
    history: historyTail(),
    token_budget: budgetValue("chat-prompt-budget"),
    memory_token_budget: budgetValue("chat-memory-budget"),
    max_memory_items: budgetValue("chat-memory-items"),
  };
  while (
    encoder.encode(JSON.stringify(body)).length > 64000 &&
    body.history.length
  ) {
    body.history.shift();
  }
  if (encoder.encode(JSON.stringify(body)).length > 64000) {
    throw new Error("消息过长，请缩短后发送");
  }
  return body;
}

function budgetValue(id) {
  const input = $(id);
  const value = Number(input.value);
  if (
    !Number.isSafeInteger(value) ||
    value < Number(input.min) ||
    value > Number(input.max)
  ) {
    throw new Error("预算必须为设置范围内的整数");
  }
  return value;
}

function bubble(role, content) {
  conversation().messages.push({ role, content });
  renderBubble(role, content);
}

function renderBubble(role, content, proactive = false) {
  const element = document.createElement("div");
  element.className = `bubble ${role}${proactive ? " proactive" : ""}`;
  const title = document.createElement("strong");
  title.textContent = proactive
    ? "伙伴 · 主动陪伴"
    : role === "user"
      ? "你"
      : $("companions").selectedOptions[0]?.textContent || "伙伴";
  const text = document.createElement("div");
  text.textContent = content;
  element.append(title, text);
  const follow = messagesAtBottom();
  $("messages").append(element);
  while ($("messages").children.length > 200) {
    $("messages").firstElementChild.remove();
  }
  if (follow) $("messages").scrollTop = $("messages").scrollHeight;
  return element;
}

function messagesAtBottom() {
  const list = $("messages");
  return list.scrollHeight - list.scrollTop - list.clientHeight < 64;
}

let activeStream = null;

function removeStream(stream = activeStream) {
  if (!stream) return;
  stream.element.remove();
  stream.controller.abort();
  void stream.reader?.cancel().catch(() => {});
  clearInterval(stream.guard);
  if (activeStream === stream) activeStream = null;
}

function streamContext(pending, generation) {
  return (
    current(generation) &&
    authorized() &&
    pending.unresolved &&
    pending.companion === session.companion &&
    pending.body.node_id === session.node &&
    pending.body.access_id === session.access &&
    pending.body.epoch === session.epoch &&
    session.commands.get(pending.companion) === pending
  );
}

async function streamDialogue(pending, generation) {
  const failed = () => new Error("回复流已中断或格式无效，请查询原命令回执");
  const controller = new AbortController();
  const element = renderBubble("assistant", "正在连接，准备思考…");
  element.classList.add("streaming");
  element.setAttribute("aria-busy", "true");
  const text = element.lastElementChild;
  const state = document.createElement("small");
  state.className = "stream-state";
  state.textContent = "正在连接";
  element.append(state);
  const stream = { controller, element, reader: null, guard: null };
  activeStream = stream;
  updateShellStatus();
  const timer = setTimeout(() => removeStream(stream), DIALOGUE_TIMEOUT_MS);
  stream.guard = setInterval(() => {
    if (!streamContext(pending, generation)) removeStream(stream);
  }, 100);
  let received = 0;
  let sequence = 0;
  let context = null;
  let checked = 0;
  let terminal = null;
  let discarded = false;
  let draftSuppressed = false;
  let eventName = "";
  let data = [];
  const checkLocal = () => {
    if (controller.signal.aborted || !streamContext(pending, generation))
      throw failed();
  };
  const dispatch = async () => {
    if (!eventName && !data.length) return;
    checkLocal();
    let value;
    try {
      value = JSON.parse(data.join("\n"));
    } catch {
      throw failed();
    }
    const name = eventName;
    eventName = "";
    data = [];
    if (
      terminal ||
      !value ||
      typeof value !== "object" ||
      value.command_id !== pending.body.command_id ||
      value.companion_id !== pending.companion
    )
      throw failed();
    if (name === "status") {
      if (!["preparing", "generating", "discarded"].includes(value.stage))
        throw failed();
      if (value.stage === "discarded") {
        discarded = true;
        element.remove();
        return;
      }
      if (discarded) throw failed();
      if (!sequence) {
        text.textContent =
          value.stage === "preparing"
            ? "正在思考，整理上下文…"
            : "正在组织回复…";
        state.textContent =
          value.stage === "preparing" ? "正在思考" : "正在生成，等待首字";
      }
    } else if (name === "snapshot") {
      if (discarded || context || sequence || !value.preview) throw failed();
      versions(
        value.preview.epoch,
        value.preview.companion_revision,
        value.preview.personality_revision,
        value.preview.state_revision,
      );
      if (!(await verifyOutputContext(value, pending, generation, false))) {
        draftSuppressed = true;
        element.remove();
      }
      checkLocal();
      context = value;
      checked = performance.now();
    } else if (name === "delta") {
      if (
        discarded ||
        !context ||
        value.sequence !== sequence + 1 ||
        typeof value.text !== "string" ||
        encoder.encode(value.text).length > 65536
      )
        throw failed();
      if (!draftSuppressed && performance.now() - checked >= 750) {
        if (!(await verifyOutputContext(context, pending, generation, false))) {
          draftSuppressed = true;
          element.remove();
        }
        checkLocal();
        checked = performance.now();
      }
      checkLocal();
      sequence = value.sequence;
      if (draftSuppressed) return;
      if (sequence === 1) notice("伙伴正在回复…");
      const follow = messagesAtBottom();
      text.textContent = value.text;
      state.textContent = value.text
        ? "正在生成 · 临时回复"
        : "正在生成，等待首字";
      element.classList.toggle("has-text", Boolean(value.text));
      if (follow) $("messages").scrollTop = $("messages").scrollHeight;
    } else if (name === "result") {
      if (
        !value.preview ||
        value.preview.companion_id !== pending.companion ||
        !Number.isSafeInteger(value.model_calls) ||
        value.model_calls < 0 ||
        value.model_calls > 1 ||
        !Number.isSafeInteger(value.estimated_input_tokens) ||
        value.estimated_input_tokens < 0 ||
        typeof value.created_at !== "string" ||
        !Number.isFinite(Date.parse(value.created_at))
      )
        throw failed();
      versions(
        value.preview.epoch,
        value.preview.companion_revision,
        value.preview.personality_revision,
        value.preview.state_revision,
      );
      if (
        !["completed", "silent", "discarded"].includes(value.status) ||
        (discarded && value.status !== "discarded")
      )
        throw failed();
      if (
        value.status === "completed" &&
        (typeof value.assistant_message !== "string" ||
          encoder.encode(value.assistant_message).length > 65536)
      )
        throw failed();
      terminal = value;
    } else if (name === "error") {
      const detail = value.error;
      if (
        !detail ||
        !Number.isInteger(detail.status_code) ||
        typeof detail.code !== "string" ||
        typeof detail.message !== "string"
      )
        throw failed();
      const error = new Error(detail.message);
      error.status = detail.status_code;
      error.code = detail.code;
      error.structured = true;
      throw error;
    } else throw failed();
  };
  const line = async (value) => {
    if (!value) return dispatch();
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "event") eventName = content;
    else if (field === "data") data.push(content);
    else throw failed();
  };
  try {
    const response = await fetch(path("/dialogue/stream", pending.companion), {
      method: "POST",
      headers: {
        Authorization: `Bearer ${session.token}`,
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(pending.body),
      cache: "no-store",
      signal: controller.signal,
    });
    checkLocal();
    if (
      !response.ok ||
      !response.headers
        .get("content-type")
        ?.toLowerCase()
        .startsWith("text/event-stream") ||
      !response.body?.getReader
    )
      throw failed();
    stream.reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let buffer = "";
    while (!terminal) {
      const chunk = await stream.reader.read();
      checkLocal();
      received += chunk.value?.byteLength || 0;
      if (received > 2 * 1024 * 1024) throw failed();
      buffer += decoder.decode(chunk.value, { stream: !chunk.done });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const value = buffer.slice(0, newline).replace(/\r$/, "");
        buffer = buffer.slice(newline + 1);
        await line(value);
      }
      if (chunk.done) {
        if (buffer) await line(buffer.replace(/\r$/, ""));
        await dispatch();
        if (!terminal) throw failed();
        break;
      }
    }
    checkLocal();
    return terminal;
  } catch (error) {
    if (error.structured) throw error;
    throw failed();
  } finally {
    clearTimeout(timer);
    removeStream(stream);
  }
}

function completeCommand(result, pending) {
  if (
    result.command_id !== pending.body.command_id ||
    result.companion_id !== pending.companion ||
    !["completed", "silent", "discarded"].includes(result.status)
  ) {
    throw new Error("回执与原命令不匹配；请继续查询原命令。");
  }
  pending.unresolved = false;
  pending.result = result;
  if (!pending.recorded) {
    conversation(pending.companion).turns.push({
      command_id: result.command_id,
      user_message: pending.body.user_message,
      request_budgets: {
        token_budget: pending.body.token_budget,
        memory_token_budget: pending.body.memory_token_budget,
        max_memory_items: pending.body.max_memory_items,
        max_output_tokens: pending.body.max_output_tokens,
      },
      status: result.status,
      source_event_id: result.source_event_id,
      model: result.model,
      usage: result.usage,
      model_calls: result.model_calls,
      memory_retrieval: result.memory_retrieval,
      preview: result.preview,
      accepted_event_id: result.accepted_event_id,
      emotion: result.emotion,
      created_at: result.created_at,
    });
    pending.recorded = true;
  }
}

async function verifyOutputContext(result, pending, generation, final = true) {
  const id = pending.companion;
  const [state, profile, mood, node, emotion] = await Promise.all([
    api(path("", id)),
    api(path("/personality", id)),
    api(path("/personality/state", id)),
    api(`/v1/nodes/${encodeURIComponent(session.node)}`),
    api(path("/emotion", id)),
  ]);
  if (!current(generation) || id !== session.companion) {
    return false;
  }
  snapshot(state);
  versions(profile.revision, mood.revision);
  const preview = result.preview;
  const ownAccess = node.accesses?.find(
    (item) => item.access_id === session.access,
  );
  const safe =
    authorized() &&
    serverLeaseValid(state) &&
    pending.body.node_id === session.node &&
    pending.body.access_id === session.access &&
    ownAccess &&
    Date.parse(ownAccess.expires_at) > serverNow() &&
    node.node_id === session.node &&
    node.online &&
    node.capabilities.includes("text.display") &&
    state.active_node_id === session.node &&
    state.epoch === pending.body.epoch &&
    state.revision === pending.body.expected_revision &&
    preview &&
    preview.companion_id === id &&
    preview.epoch === state.epoch &&
    preview.companion_revision === state.revision &&
    preview.active_node_id === session.node &&
    preview.personality_revision === profile.revision &&
    preview.state_revision === mood.revision &&
    effectiveState(mood) === pending.effectiveState &&
    stateStillEffective(mood) &&
    (!result.emotion ||
      (result.emotion.companion_id === id &&
        typeof result.emotion.context_version === "string" &&
        result.emotion.context_version === emotion.context_version));
  if (final) showEmotion(emotion);
  if (!safe && final) {
    stop("状态或输出权改变，请重新接入");
    notice("本次回复已过时，未显示文字。", true);
  }
  return Boolean(safe);
}

async function displayResult(result, pending, generation) {
  if (!current(generation) || pending.companion !== session.companion) {
    return;
  }
  renderTurns();
  if (result.preview) {
    versions(
      result.preview.epoch,
      result.preview.companion_revision,
      result.preview.personality_revision,
      result.preview.state_revision,
    );
  }
  $("usage").textContent =
    `模型：${result.model || "—"}；调用 ${result.model_calls} 次；用量：` +
    (result.usage
      ? `输入 ${result.usage.input_tokens} / 输出 ${result.usage.output_tokens} token`
      : result.model_calls === 0
        ? "0（未调用模型）"
        : "未知");
  if (result.memory_retrieval) {
    const retrieval = result.memory_retrieval;
    $("usage").textContent +=
      `；向量检索：${retrieval.cache_hit ? "缓存命中" : `调用 ${retrieval.model_calls} 次`}` +
      (retrieval.usage ? ` / 输入 ${retrieval.usage.input_tokens} token` : "") +
      (retrieval.degraded ? "，已回退本地召回" : "");
  }
  if (result.status !== "completed") {
    notice(
      result.status === "silent"
        ? "当前状态保持安静，未调用模型。"
        : "输出条件已改变，本次回复已丢弃。",
    );
    return;
  }
  if (!authorized()) {
    notice("回复已完成，但此页输出权已失效；未显示文字。", true);
    return;
  }
  if (!(await verifyOutputContext(result, pending, generation))) return;
  const preview = result.preview;
  if (pending.displayed) {
    return;
  }
  pending.displayed = true;
  bubble("assistant", result.assistant_message);
  session.history.push({
    role: "assistant",
    content: result.assistant_message,
  });
  conversation().history = session.history;
  session.completed = result.source_event_id
    ? {
        source: result.source_event_id,
        companion: session.companion,
        node: pending.body.node_id,
      }
    : null;
  $("save-content").value = "";
  showJson("prompt", preview);
  notice("回复完成。需要长期保留的信息可手动保存为记忆。");
  buttons();
}

async function send() {
  if (
    !authorized() ||
    session.personaRunning ||
    session.busy ||
    lastCommand()?.unresolved ||
    !session.configured
  ) {
    return;
  }
  const generation = session.generation;
  let pending;
  try {
    const previewBody = requestBody();
    session.busy = true;
    session.completed = null;
    $("save-content").value = "";
    buttons();
    const id = session.companion;
    const [state, mood] = await Promise.all([
      api(path("", id)),
      api(path("/personality/state", id)),
    ]);
    if (!current(generation) || !authorized()) {
      return;
    }
    snapshot(state);
    versions(mood.revision);
    if (
      state.active_node_id !== session.node ||
      state.epoch !== session.epoch ||
      !serverLeaseValid(state) ||
      !stateStillEffective(mood)
    ) {
      throw new Error("输出设备或有效状态已改变，请刷新后重新接入");
    }
    const body = {
      ...previewBody,
      command_id: uuid(),
      node_id: session.node,
      access_id: session.access,
      epoch: state.epoch,
      expected_revision: state.revision,
      max_output_tokens: budgetValue("chat-output-budget"),
    };
    while (
      encoder.encode(JSON.stringify(body)).length > 65536 &&
      body.history.length
    ) {
      body.history.shift();
    }
    if (encoder.encode(JSON.stringify(body)).length > 65536) {
      throw new Error("请求过长");
    }
    pending = {
      body,
      companion: id,
      effectiveState: effectiveState(mood),
      unresolved: true,
      displayed: false,
    };
    session.commands.set(id, pending);
    bubble("user", body.user_message);
    session.history.push({ role: "user", content: body.user_message });
    conversation().history = session.history;
    $("message").value = "";
    conversation().draft = "";
    buttons();
    notice("正在等待模型回复…");
    const result =
      session.capabilities?.dialogue?.streaming === true &&
      typeof ReadableStream !== "undefined" &&
      typeof TextDecoder !== "undefined"
        ? await streamDialogue(pending, generation)
        : await api(
            path("/dialogue", id),
            body,
            session.token,
            "POST",
            DIALOGUE_TIMEOUT_MS,
          );
    if (!current(generation) || id !== session.companion) return;
    completeCommand(result, pending);
    await displayResult(result, pending, generation);
  } catch (error) {
    if (current(generation)) {
      notice(
        `${error.message}${
          pending ? "。发送已锁定，请查询原命令回执；不会换新命令重发。" : ""
        }`,
        true,
      );
    }
  } finally {
    if (current(generation)) {
      session.busy = false;
      buttons();
    }
  }
}

function terminalReceiptError(error) {
  // This GET endpoint replays a persisted failure as its structured domain error.
  // Missing, unauthenticated, running, or ambiguous transport results are not terminal.
  return (
    error.structured &&
    error.status !== 401 &&
    error.status !== 403 &&
    !["not_found", "dialogue_in_progress"].includes(error.code)
  );
}

async function receipt() {
  const pending = lastCommand();
  if (!pending || session.busy) {
    return;
  }
  const generation = session.generation;
  session.busy = true;
  buttons();
  try {
    const result = await api(
      path(
        `/dialogue/${encodeURIComponent(pending.body.command_id)}`,
        pending.companion,
      ),
    );
    completeCommand(result, pending);
    await displayResult(result, pending, generation);
  } catch (error) {
    if (terminalReceiptError(error)) {
      pending.unresolved = false;
      if (!pending.recorded) {
        conversation(pending.companion).turns.push({
          command_id: pending.body.command_id,
          user_message: pending.body.user_message,
          status: "failed",
          error: error.message,
          error_code: error.code,
        });
        pending.recorded = true;
      }
    }
    if (current(generation)) renderTurns();
    if (current(generation)) {
      notice(
        error.code === "dialogue_in_progress"
          ? "原命令仍在执行，发送保持锁定；稍后可再次查询回执。"
          : `${error.message}${
              pending.unresolved
                ? "。结果尚未确认，发送保持锁定，请继续查询原回执。"
                : "。原命令已终止，可开始新的对话。"
            }`,
        true,
      );
    }
  } finally {
    if (current(generation)) {
      session.busy = false;
      buttons();
    }
  }
}

// Check generation after asynchronous errors, including when the action itself changes it.
function action(fn) {
  return async (event) => {
    event?.preventDefault();
    const context = { generation: session.generation };
    try {
      await fn(context);
    } catch (error) {
      if (context.generation === session.generation) {
        notice(error.message, true);
      }
    }
  };
}

$("connect").onclick = action(async (context) => {
  if (session.connecting || session.token) return;
  const token = $("token").value.trim() || "local-dev-token";
  $("token").value = "";
  session.connecting = true;
  if ($("connection-error")) {
    $("connection-error").textContent = "";
    $("connection-error").hidden = true;
  }
  buttons();
  try {
    const caps = await api("/v1/capabilities", undefined, token);
    if (context.generation !== session.generation) return;
    session.token = token;
    session.generation++;
    context.generation = session.generation;
    session.capabilities = caps;
    session.configured = Boolean(caps.dialogue?.configured);
    showModel();
    $("connection").textContent = "已连接";
    buttons();
    await Promise.all([companions(), loadDeviceOptions()]);
    if (current(context.generation)) {
      notice("已连接。选择伙伴只读取资料。");
      showPage(location.hash.slice(1));
      closeConnection();
    }
  } catch (error) {
    if (context.generation === session.generation) {
      session.token = "";
      session.capabilities = null;
      session.configured = false;
      $("connection").textContent = "未连接";
      if ($("connection-error")) {
        $("connection-error").textContent = error.message;
        $("connection-error").hidden = false;
      }
    }
    throw error;
  } finally {
    session.connecting = false;
    buttons();
  }
});

$("disconnect").onclick = () => {
  if (session.companion) conversation().draft = $("message").value;
  stop("已断开");
  session.token = "";
  $("token").value = "";
  session.companion = "";
  session.capabilities = null;
  session.configured = false;
  clearCompanionData();
  $("companions").replaceChildren(new Option("请选择", ""));
  $("local-device").replaceChildren(new Option("自动识别本机设备", ""));
  $("connection").textContent = "未连接";
  $("state").textContent = "未连接";
  $("personality").textContent = "未连接";
  $("model").textContent = "未连接";
  buttons();
  notice("连接已断开。未决命令保留在当前页内存，重新连接后可查询。");
};

$("companions").onchange = action(async (context) => {
  if (session.companion) conversation().draft = $("message").value;
  stop("选择伙伴只查看资料");
  context.generation = session.generation;
  session.companion = $("companions").value;
  clearCompanionData();
  buttons();
  if (session.companion) {
    showModel();
    notice(
      lastCommand()?.unresolved
        ? "此伙伴有未决命令，请查询原回执后再发送。"
        : "已选择伙伴；接入前只查看资料。",
    );
    await Promise.all([info(), memories()]);
    if (current(context.generation)) showPage(location.hash.slice(1));
  } else {
    notice("请选择伙伴。");
  }
});

$("create-form").onsubmit = action(async (context) => {
  await api("/v1/companions", {
    companion_id: $("new-id").value.trim(),
    name: $("new-name").value.trim(),
  });
  if (!current(context.generation)) {
    return;
  }
  await companions();
  if (current(context.generation)) {
    notice("伙伴已创建，请选择后查看。");
  }
});

$("refresh").onclick = action(info);
$("activate").onclick = activate;
$("chat-form").onsubmit = (event) => {
  event.preventDefault();
  send();
};
$("message").onkeydown = (event) => {
  if (
    event.key === "Enter" &&
    (event.ctrlKey || event.metaKey) &&
    !event.isComposing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    send();
  }
};

$("preview").onclick = action(async () => {
  const generation = session.generation;
  const result = await api(path("/personality/preview"), requestBody());
  if (current(generation)) {
    versions(
      result.epoch,
      result.companion_revision,
      result.personality_revision,
      result.state_revision,
    );
    showJson("prompt", result);
    notice("提示预览完成，未调用模型。");
  }
});

$("receipt").onclick = receipt;
$("save-form").onsubmit = action(async () => {
  const completed = session.completed;
  const generation = session.generation;
  if (!completed || completed.companion !== session.companion) {
    return;
  }
  $("save").disabled = true;
  try {
    const command = uuid();
    const kind = $("save-kind").value;
    await api(path("/memories"), {
      command_id: command,
      kind,
      ...(kind === "episode" ? { episode_key: `manual:${command}` } : {}),
      content: $("save-content").value,
      source_event_id: completed.source,
      source_node_id: completed.node,
      confirmed: $("confirmed").checked,
      pinned: $("pinned").checked,
      retention_policy: $("save-retention").value,
    });
    if (current(generation)) {
      notice("记忆已保存。");
      await memories();
    }
  } finally {
    if (current(generation)) {
      buttons();
    }
  }
});

$("memory-refresh").onclick = action(memories);
$("memory-run").onclick = action(async () => {
  if (session.memoryRunning || !session.automationEnabled) return;
  const generation = session.generation;
  const id = session.companion;
  session.memoryRunning = true;
  buttons();
  try {
    const result = await api(path("/memories/automation/run", id), {
      force: true,
    });
    if (current(generation)) {
      notice(
        result.status === "idle"
          ? "暂无可提前整理的对话。"
          : "本次整理已返回，请查看整理记录。",
      );
      await memories();
    }
  } finally {
    if (current(generation)) {
      session.memoryRunning = false;
      buttons();
    }
  }
});
$("previous").onclick = action(async () => {
  session.offset = Math.max(0, session.offset - 20);
  await memories();
});
$("embedding-run").onclick = action(async () => {
  if (session.embeddingRunning || !session.embeddingsEnabled) return;
  const generation = session.generation;
  const id = session.companion;
  session.embeddingRunning = true;
  buttons();
  try {
    const result = await api(path("/memories/embeddings/run", id), {});
    if (current(generation)) {
      notice(
        result.status === "idle"
          ? "暂无可索引的记忆。"
          : `索引任务：${result.status}。`,
      );
      await memories();
    }
  } finally {
    if (current(generation)) {
      session.embeddingRunning = false;
      buttons();
    }
  }
});
$("graph-entity").onchange = graphAliasSelection;
$("graph-form").onsubmit = action(async () => {
  const generation = session.generation;
  const query = new URLSearchParams({
    query: $("graph-query").value,
    hops: $("graph-hops").value,
    limit: "50",
  });
  const result = await api(path(`/memories/knowledge-graph?${query}`));
  if (current(generation)) renderGraph(result);
});
$("graph-alias-form").onsubmit = action(async () => {
  const generation = session.generation;
  const entity = session.graphEntities.find(
    (item) => item.entity_id === $("graph-entity").value,
  );
  if (!entity) return;
  versions(entity.revision);
  await api(
    path(
      `/memories/knowledge-graph/entities/${encodeURIComponent(entity.entity_id)}/aliases`,
    ),
    {
      command_id: uuid(),
      expected_revision: entity.revision,
      aliases: $("graph-aliases")
        .value.split("\n")
        .map((item) => item.trim())
        .filter(Boolean),
      source_event_ids: entity.source_event_ids || [],
    },
    session.token,
    "PUT",
  );
  if (current(generation)) {
    notice("实体别名已保存。");
    await memories();
  }
});
$("next").onclick = action(async () => {
  session.offset += 20;
  await memories();
});
$("recall-form").onsubmit = action(async () => {
  if (session.recallBusy) return;
  const generation = session.generation;
  session.recallBusy = true;
  buttons();
  try {
    const route = $("semantic-recall").checked
      ? "/memories/semantic-recall"
      : "/memories/recall";
    const result = await api(path(route), {
      query: $("query").value,
      ...($("recall-semantic-key").value.trim()
        ? { semantic_key: $("recall-semantic-key").value.trim() }
        : {}),
      token_budget: Number($("budget").value),
      max_items: 8,
    });
    if (current(generation)) showJson("recall-result", result);
  } finally {
    if (current(generation)) {
      session.recallBusy = false;
      buttons();
    }
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stop("页面已隐藏；返回后点击“在此页对话”");
  } else {
    buttons();
  }
});
window.addEventListener("offline", () => stop("网络已断开，请重新接入"));
window.addEventListener("pagehide", () => stop("页面已离开"));

async function maintainLease() {
  buttons();
  if (
    (session.active || session.activating) &&
    session.access &&
    !accessAlive()
  ) {
    stop("本页设备访问已到期，请重新接入");
    return;
  }
  if (session.active && performance.now() >= session.deadline) {
    stop("租约已到期，请重新接入");
    return;
  }
  if (
    (!session.active && !session.activating) ||
    !session.access ||
    document.hidden ||
    session.tickBusy ||
    !session.token
  ) {
    return;
  }
  const generation = session.generation;
  const node = session.node;
  const access = session.access;
  const id = session.companion;
  session.tickBusy = true;
  try {
    const started = performance.now();
    const result = await api(
      `/v1/nodes/${encodeURIComponent(node)}/access/${encodeURIComponent(access)}/heartbeat`,
      {
        context: { page_visible: true },
      },
    );
    if (!current(generation) || document.hidden) {
      return;
    }
    if (
      node !== session.node ||
      access !== session.access ||
      id !== session.companion
    )
      return;
    acceptAccess(result, access, started, node);
    if (!session.active) return;
    const state = await api(path("", id));
    if (
      !current(generation) ||
      node !== session.node ||
      access !== session.access ||
      id !== session.companion
    )
      return;
    snapshot(state);
    if (
      state.active_node_id !== session.node ||
      state.epoch !== session.epoch
    ) {
      throw new Error("输出已转移到其他设备");
    }
    await lease(generation);
  } catch (error) {
    if (current(generation)) {
      stop("输出已停止，请重新接入");
      notice(error.message, true);
    }
  } finally {
    if (generation === session.generation) session.tickBusy = false;
  }
}

setInterval(maintainLease, 2000);
buttons();

function renderTurns() {
  let panel = $("turns");
  if (!panel) {
    panel = document.createElement("div");
    panel.id = "turns";
    $("usage").after(panel);
  }
  panel.replaceChildren();
  for (const turn of conversation().turns.slice(-100)) {
    const details = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent = `${turn.status} · 命令 ${turn.command_id} · 本轮来源、记忆引用与用量`;
    const raw = document.createElement("pre");
    raw.textContent = JSON.stringify(turn, null, 2);
    details.append(title, raw);
    panel.append(details);
  }
}

function download(name, data) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("message").addEventListener("input", () => {
  if (session.companion) conversation().draft = $("message").value;
  updateShellStatus();
});
$("chat-export").onclick = () =>
  download("conversation.json", {
    companion_id: session.companion,
    ...conversation(),
  });
$("emotion-refresh").onclick = action(emotionExperiences);
$("chat-copy").onclick = action(async () => {
  if (!navigator.clipboard?.writeText) {
    notice(
      "此浏览器连接无法访问剪贴板。请点击「导出对话」保存内容，或直接选中文本复制。",
      true,
    );
    return;
  }
  try {
    await navigator.clipboard.writeText(
      conversation()
        .messages.map(
          (item) => `${item.role === "user" ? "你" : "伙伴"}：${item.content}`,
        )
        .join("\n\n"),
    );
    notice("对话已复制。");
  } catch {
    notice(
      "浏览器未允许复制。请点击「导出对话」保存内容，或直接选中文本复制。",
      true,
    );
  }
});
$("chat-clear").onclick = () => {
  if (session.busy || lastCommand()?.unresolved || !session.companion) return;
  session.conversations.delete(session.companion);
  session.history = conversation().history;
  session.completed = null;
  $("messages").replaceChildren();
  $("message").value = "";
  $("save-content").value = "";
  $("usage").textContent = "";
  $("prompt").textContent = "尚无预览";
  renderTurns();
  buttons();
  notice("已清空本页该伙伴的对话历史。服务器记忆仍保留。");
};

async function importPersona(source) {
  if (!session.companion || lastCommand()?.unresolved || session.busy) {
    throw new Error("请选择伙伴，并先确认未决对话回执。");
  }
  const generation = session.generation;
  const id = session.companion;
  let card;
  if (typeof source === "string") {
    const response = await fetch(source, { cache: "no-store" });
    if (!response.ok) throw new Error("内置人格卡加载失败");
    card = await response.json();
  } else {
    const file = $("persona-file").files[0];
    if (!file) throw new Error("请先选择人格卡 JSON 文件");
    if (file.size > 65536) throw new Error("人格卡文件不能超过 64 KiB");
    card = JSON.parse(await file.text());
  }
  if (!current(generation)) return;
  const profile = await api(path("/personality", id));
  if (!current(generation)) return;
  versions(profile.revision);
  const result = await api(path("/personality/import", id), {
    command_id: uuid(),
    expected_revision: profile.revision,
    card,
  });
  if (current(generation)) {
    $("persona-status").textContent =
      `人格卡已导入（版本 ${result.revision}）；记忆仍保留。`;
    await info();
    if (session.personaLoaded) await loadPersona();
    notice("人格卡已导入。可在此页接入并测试记忆。");
  }
}
async function personaAction(source) {
  if (session.personaRunning) return;
  session.personaRunning = true;
  buttons();
  try {
    await importPersona(source);
  } finally {
    session.personaRunning = false;
    buttons();
  }
}
$("persona-import").onclick = action(() => personaAction(null));
$("persona-fixture").onclick = action(() =>
  personaAction("./memory-test.v2.json"),
);
$("persona-card-sui").onclick = action(() => personaAction("./sui.v2.json"));

function logFilters() {
  return {
    operation: $("logs-operation").value.trim(),
    status: $("logs-status").value.trim(),
    command_id: $("logs-command").value.trim(),
  };
}

async function loadLogs(reset = false) {
  if (!session.token || !session.companion || session.logRunning) return;
  const generation = session.generation;
  const request = ++session.logRequest;
  const id = session.companion;
  const filters = logFilters();
  reset =
    reset || JSON.stringify(filters) !== JSON.stringify(session.logFilters);
  const query = new URLSearchParams({
    after_sequence: String(reset ? 0 : session.logCursor),
    limit: "100",
  });
  for (const [key, value] of Object.entries(filters))
    if (value) query.set(key, value);
  session.logRunning = true;
  buttons();
  try {
    const result = await api(path(`/memories/logs?${query}`, id));
    if (
      !current(generation) ||
      request !== session.logRequest ||
      JSON.stringify(filters) !== JSON.stringify(logFilters())
    )
      return;
    if (!validVersion(result.next_sequence) || !Array.isArray(result.items))
      throw new Error("日志响应格式无效");
    session.logFilters = filters;
    session.logs = (
      reset ? result.items : [...session.logs, ...result.items]
    ).slice(-500);
    session.logCursor = result.next_sequence;
    session.logMore = result.has_more;
    $("logs-list").replaceChildren(
      ...session.logs.map((item) => {
        const details = document.createElement("details");
        const title = document.createElement("summary");
        title.textContent = `#${item.sequence} · ${item.operation} · ${item.status} · ${item.created_at}`;
        const raw = document.createElement("pre");
        raw.textContent = JSON.stringify(item, null, 2);
        details.append(title, raw);
        return details;
      }),
    );
    $("logs-summary").textContent =
      `已加载 ${session.logs.length} 条（最多保留最近 500 条） · 游标 ${session.logCursor}` +
      (result.has_more ? " · 有后续日志" : " · 已到最新");
  } catch (error) {
    if (
      current(generation) &&
      request === session.logRequest &&
      JSON.stringify(filters) === JSON.stringify(logFilters())
    )
      throw error;
  } finally {
    if (current(generation) && request === session.logRequest) {
      session.logRunning = false;
      buttons();
    }
  }
}
$("logs-form").onsubmit = action(() => loadLogs(true));
$("logs-more").onclick = action(() => loadLogs());
$("logs-export").onclick = () =>
  download("memory-logs.json", {
    companion_id: session.companion,
    filters: session.logFilters,
    items: session.logs,
    next_sequence: session.logCursor,
    has_more: session.logMore,
  });
setInterval(() => {
  if (
    $("logs-auto").checked &&
    !$("logs-pane").hidden &&
    !document.hidden &&
    !session.logRunning
  ) {
    action(() => loadLogs())();
  }
}, 5000);

// ---------------------------------------------------------------- 日子（事件时间轴）
// GET /v1/events is forward-only: after_sequence=0 yields the OLDEST page, so a
// first load walks forward to the tail. A bounded walk keeps a long-lived runtime
// from paging the whole log on every refresh; the summary states when it clipped.
const DAY_MAX_WALK_PAGES = 20;

const DAY_TYPES = {
  "companion.created": "伙伴创建",
  "companion.context_updated": "上下文更新",
  "companion.transferred": "完成迁移",
  "companion.dialogue_received": "收到消息",
  "companion.dialogue_completed": "完成回复",
  "companion.dialogue_silent": "静默未回复",
  "companion.dialogue_discarded": "回复被丢弃",
  "companion.personality_updated": "人格更新",
  "companion.personality_state_updated": "短期状态更新",
  "companion.emotion_updated": "情绪更新",
  "transfer.prepared": "准备迁移",
  "transfer.aborted": "迁移已取消",
  "transfer.expired": "迁移已过期",
  "node.registered": "节点注册",
  "node.observation": "设备观测",
};

function dayTone(type) {
  if (type.startsWith("transfer.") || type === "companion.transferred")
    return "transfer";
  if (type.startsWith("companion.dialogue_")) return "dialogue";
  if (type.includes("personality") || type.includes("emotion"))
    return "personality";
  if (
    type.endsWith("_discarded") ||
    type.includes("expired") ||
    type.includes("aborted")
  ) {
    return "problem";
  }
  return "other";
}

function dayText(value) {
  return typeof value === "string" ? value : "";
}

function daySummary(event) {
  const payload = event.payload || {};
  const label = DAY_TYPES[event.type] || event.type;
  if (event.type === "companion.dialogue_completed") {
    const reply = dayText(payload.assistant_message);
    return `${label}：${reply.slice(0, 60)}${reply.length > 60 ? "…" : ""}`;
  }
  if (event.type === "companion.dialogue_received") {
    return `${label}：${dayText(payload.user_message).slice(0, 60)}`;
  }
  if (event.type === "companion.transferred") {
    const transfer = payload.transfer || {};
    return `${label}：${dayText(transfer.source_node_id) || "无源"} → ${dayText(transfer.target_node_id) || "未知"}`;
  }
  if (event.type === "node.registered" || event.type === "node.observation") {
    return `${label}：${event.node_id || "未知设备"}`;
  }
  return label;
}

const dayDateFormat = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const dayTimeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

function dayDateKey(iso) {
  const value = new Date(iso);
  return Number.isNaN(value.getTime()) ? "" : dayDateFormat.format(value);
}

function dayTime(iso) {
  const value = new Date(iso);
  return Number.isNaN(value.getTime())
    ? String(iso)
    : dayTimeFormat.format(value);
}

function dayFilters() {
  return {
    type: $("days-type").value,
    companion: $("days-companion").value,
    node: $("days-node").value,
    hideEmotion: $("days-hide-emotion").checked,
  };
}

function dayMatches(event, filters) {
  if (filters.type && event.type !== filters.type) return false;
  // Emotion is re-emitted on every affect touch and drowns out the timeline, so it
  // is hidden unless the caller explicitly asks for all types or that exact type.
  if (
    filters.hideEmotion &&
    event.type === "companion.emotion_updated" &&
    !filters.type
  ) {
    return false;
  }
  if (filters.companion && (event.companion_id || "") !== filters.companion)
    return false;
  if (filters.node) {
    const related = [event.node_id, event.recipient_node_id].filter(Boolean);
    if (!related.includes(filters.node)) return false;
  }
  return true;
}

function renderDays() {
  const filters = dayFilters();
  const visible = session.dayEvents
    .filter((event) => dayMatches(event, filters))
    .slice()
    .reverse();
  const list = $("days-list");
  list.replaceChildren();
  if (!visible.length) {
    list.textContent = session.dayEvents.length
      ? "没有符合筛选条件的事件。"
      : "尚无事件。刷新后按发生时间倒序显示。";
  }
  let heading = null;
  for (const event of visible) {
    const key = dayDateKey(event.occurred_at);
    if (key !== heading) {
      heading = key;
      const title = document.createElement("h3");
      title.className = "day-heading";
      title.textContent = key || "时间未知";
      list.append(title);
    }
    const article = document.createElement("details");
    article.className = "day-event";
    article.dataset.tone = dayTone(event.type);
    const summary = document.createElement("summary");
    summary.append(document.createTextNode(`${dayTime(event.occurred_at)} `));
    const strong = document.createElement("strong");
    strong.textContent = daySummary(event);
    summary.append(strong);
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent =
      `#${event.sequence} · ${event.type}` +
      (event.companion_id ? ` · ${event.companion_id}` : "") +
      (event.node_id ? ` · ${event.node_id}` : "");
    summary.append(meta);
    const raw = document.createElement("pre");
    raw.textContent = JSON.stringify(event, null, 2);
    article.append(summary, raw);
    list.append(article);
  }
  $("days-summary").textContent = session.dayEvents.length
    ? `已加载 ${session.dayEvents.length} 条事件（游标 ${session.dayCursor}）· 显示 ${visible.length} 条`
    : "尚无事件";
}

async function loadDays(reset = false) {
  if (!session.token || session.dayRunning) return;
  const generation = session.generation;
  const request = ++session.dayRequest;
  session.dayRunning = true;
  buttons();
  const collected = [];
  let cursor = reset ? 0 : session.dayCursor;
  let clipped = false;
  try {
    for (let page = 0; page < DAY_MAX_WALK_PAGES; page++) {
      const query = new URLSearchParams({
        after_sequence: String(cursor),
        limit: "500",
      });
      const node = $("days-node").value;
      if (node) query.set("node_id", node);
      const result = await api(`/v1/events?${query}`);
      if (!current(generation) || request !== session.dayRequest) return;
      if (
        !validVersion(result.next_sequence) ||
        !Array.isArray(result.events)
      ) {
        throw new Error("事件响应格式无效");
      }
      collected.push(...result.events);
      cursor = result.next_sequence;
      if (result.events.length < 500) break;
      if (page === DAY_MAX_WALK_PAGES - 1) clipped = true;
    }
    session.dayEvents = reset
      ? collected
      : [...session.dayEvents, ...collected];
    session.dayCursor = cursor;
    renderDays();
    if (clipped)
      notice(
        `事件较多，已加载最近 ${DAY_MAX_WALK_PAGES * 500} 条；更早的事件请用 API 分页。`,
      );
  } finally {
    if (current(generation) && request === session.dayRequest) {
      session.dayRunning = false;
      buttons();
    }
  }
}

async function loadDevices() {
  if (!session.token) return;
  const generation = session.generation;
  const [nodes, graph] = await Promise.all([
    api("/v1/nodes"),
    api("/v1/graph"),
  ]);
  if (!current(generation)) return;
  deviceOptions(nodes);
  const names = new Map(
    nodes.map((node) => [node.node_id, node.label || node.node_id]),
  );
  const select = $("days-node");
  const chosen = select.value;
  select.replaceChildren(new Option("全部设备", ""));
  for (const node of nodes)
    select.add(
      new Option(`${node.label || node.node_id}（${node.kind}）`, node.node_id),
    );
  select.value = chosen;
  const connSelect = $("conn-node");
  const connChosen = connSelect.value;
  connSelect.replaceChildren(new Option("全部设备", ""));
  for (const node of nodes)
    connSelect.add(
      new Option(`${node.label || node.node_id}（${node.kind}）`, node.node_id),
    );
  connSelect.value = connChosen;
  const list = $("devices-list");
  list.replaceChildren();
  for (const node of nodes) {
    const article = document.createElement("div");
    article.className = "device";
    const title = document.createElement("strong");
    title.textContent =
      (node.kind === "web" ? "历史访问节点 · " : "") +
      (node.label || node.node_id);
    const online = document.createElement("span");
    online.className = node.online ? "online" : "offline";
    online.textContent = node.online ? " 在线" : " 离线";
    const detail = document.createElement("small");
    detail.className = "meta";
    detail.textContent = ` · ${node.node_id} · ${node.kind} · 最后心跳 ${dayTime(node.last_seen_at)}`;
    const caps = document.createElement("div");
    caps.textContent = `能力：${(node.capabilities || []).join("、") || "无"}`;
    const addresses = document.createElement("div");
    addresses.textContent = `已观测 IP：${node.device?.ip_addresses?.join("、") || "暂无"}`;
    const accesses = document.createElement("div");
    accesses.textContent = node.accesses?.length
      ? `访问方式：Web 页面（${node.accesses.length}）；访问到期 ${node.accesses.map((item) => dayTime(item.expires_at)).join("、")}`
      : node.kind === "web"
        ? "历史 Web 节点（保留旧引用）"
        : "访问方式：无活跃 Web 访问";
    article.append(title, online, detail, caps, addresses, accesses);
    if (node.context?.attention_collection) {
      const collection = document.createElement("div");
      collection.className = "hint";
      collection.textContent = attentionCollectionText(
        node.context.attention_collection,
      );
      article.append(collection);
    }
    list.append(article);
  }
  if (!nodes.length) list.textContent = "尚无设备节点。";
  showJson("graph-raw", graph);
  const owner = graph?.nodes?.find?.((item) => item.kind === "companion");
  if (owner && names.size) renderDays();
}

function attentionCollectionText(diagnostic) {
  if (!diagnostic) return "桌面采集：尚无诊断";
  const statuses = {
    disabled: "未开启",
    ok: "可用",
    collected: "已采集",
    available: "可用",
    partial: "部分可用",
    unknown: "未知",
    unsupported: "当前桌面不支持",
    no_session: "未找到本地桌面会话",
    session_unavailable: "桌面会话不可用",
    permission_denied: "桌面访问权限不足",
    error: "探测未完成",
  };
  const sources = {
    loginctl: "系统会话服务",
    logind: "系统会话服务",
    "logind+x11": "系统会话服务与 X11",
    x11: "X11 桌面",
    linux_desktop: "Linux 本地桌面",
    linux_logind: "系统会话服务",
    none: "无可用来源",
    unknown: "未知",
  };
  const sessions = {
    x11: "X11",
    wayland: "Wayland",
    tty: "终端",
    unspecified: "未知",
    unknown: "未知",
  };
  const reasons = {
    no_session: "未找到本地桌面会话",
    ambiguous_session: "无法确认唯一桌面会话",
    probe_failed: "桌面探测未完成",
  };
  const reason =
    diagnostic.status === "unknown"
      ? ` · 原因 ${reasons[diagnostic.reason] || "未知"}`
      : "";
  const sampledAt = Number.isFinite(Date.parse(diagnostic.sampled_at))
    ? dayTime(diagnostic.sampled_at)
    : "未知";
  return `桌面采集：${statuses[diagnostic.status] || "未知（未确认探测结果）"} · 来源 ${sources[diagnostic.source] || "未知"} · 会话 ${sessions[diagnostic.session_type] || "未知"}${diagnostic.sampled_at ? ` · 采样于 ${sampledAt}` : ""}${reason}`;
}

// ---------------------------------------------------------------- 后台陪伴
const BACKGROUND_REASONS = {
  eligible: "等待安全参与时机",
  model_not_configured: "尚未配置对话模型",
  quiet_requested: "已请求安静陪伴",
  candidate_expired: "参与时机已过",
  policy_changed: "陪伴策略已更新",
  ownership_changed: "输出设备已变更",
  attention_session_changed: "宿主会话已变更",
  transfer_in_progress: "正在切换设备",
  disabled: "策略未开启",
  policy_disabled: "策略未开启",
  cooldown: "仍在冷却期",
  daily_limit: "今日次数已达上限",
  waiting_for_user: "等待明确的交谈时机",
  focus_protection: "专注保护中，保持安静",
  focused: "正在专注，保持安静",
  focus_protected: "专注保护中",
  focus_releasing: "专注结束缓冲中",
  suspected_stuck: "可能遇到困难，等待明确邀请",
  busy: "当前忙碌",
  user_away: "用户离开或设备锁定",
  meeting_or_sharing: "会议或屏幕共享中",
  attention_unknown: "宿主信号不足，保持安静",
  attention_stale: "宿主信号已过期",
  user_interacting: "正在与伙伴交谈",
  output_not_permitted: "当前设备无法输出",
  boundary_superseded: "活动边界已更新",
  lease_expired: "输出租约已过期",
  completed: "已完成主动陪伴",
  queued: "等待安全参与时机",
  running: "正在准备陪伴",
  silent: "本次保持安静",
  discarded: "已放弃过时输出",
  failed: "本次参与未完成",
  suppressed: "已抑制主动参与",
};
const backgroundLabel = (value) =>
  BACKGROUND_REASONS[value] || `未参与（${value || "等待明确活动边界"}）`;
function backgroundButtons() {
  const b = session.background;
  const selected = Boolean(session.token && session.companion);
  $("background-fields").disabled = !selected || !b.loaded || b.saving;
  $("background-save").disabled =
    !selected || !b.loaded || !b.dirty || b.saving;
  $("background-refresh").disabled = !selected || b.running || b.saving;
}
function resetBackground() {
  session.background = {
    request: 0,
    dirty: false,
    loaded: false,
    running: false,
    saving: false,
    policy: null,
    revision: null,
    attention: null,
    inner: null,
  };
  $("background-enabled").checked = false;
  $("background-summary").textContent = "尚未载入";
  $("background-policy-status").textContent = "只有明确保存才会更改策略。";
  $("background-attention").textContent = "尚无宿主信号";
  $("background-focus").textContent = "专注建议未知（尚无宿主信号）";
  $("background-inner").textContent = "尚无内在活动";
  $("background-focus-segments").replaceChildren();
  $("background-reason").textContent = "尚无参与决策";
  $("background-decisions").replaceChildren();
}
function fillBackgroundPolicy(snapshot) {
  const b = session.background;
  b.policy = snapshot.policy;
  b.revision = snapshot.revision;
  b.loaded = true;
  b.currentPolicy = snapshot.policy;
  $("background-enabled").checked = snapshot.policy.enabled;
  $("background-daily").value = String(snapshot.policy.daily_limit);
  $("background-cooldown").value = String(snapshot.policy.cooldown_seconds);
}
async function refreshBackground() {
  if (!session.token || !session.companion) return;
  const b = session.background;
  if (b.saving) return;
  const generation = session.generation,
    id = session.companion,
    request = ++b.request;
  const valid = () =>
    current(generation) &&
    id === session.companion &&
    b === session.background &&
    request === b.request;
  b.running = true;
  backgroundButtons();
  try {
    const [policy, state, decisions, inner] = await Promise.all([
      api(path("/proactivity", id)),
      api(path("", id)),
      api(path("/proactivity/decisions?latest=true&limit=20", id)),
      api(path("/inner-life", id)),
    ]);
    if (!valid()) return;
    let attention = null,
      focus = null,
      activeNode = null;
    if (state.active_node_id) {
      [attention, focus, activeNode] = await Promise.all([
        api(`/v1/nodes/${encodeURIComponent(state.active_node_id)}/attention`),
        api(`/v1/nodes/${encodeURIComponent(state.active_node_id)}/focus`),
        api(`/v1/nodes/${encodeURIComponent(state.active_node_id)}`),
      ]);
    }
    if (!valid()) return;
    versions(policy.revision);
    if (!b.dirty) fillBackgroundPolicy(policy);
    b.attention = attention;
    b.focus = focus;
    b.state = state;
    b.inner = inner;
    b.currentPolicy = policy.policy;
    $("background-summary").textContent = policy.policy.enabled
      ? "已开启 · 遵循安静边界"
      : "未开启";
    if (!b.dirty)
      $("background-policy-status").textContent =
        `策略版本 ${policy.revision} · 只有保存才会更改。`;
    const activityNames = {
      code: "编程",
      docs: "阅读资料",
      chat: "交谈",
      meeting: "会议",
      video: "观看视频",
      game: "游戏",
      media: "媒体",
      terminal: "终端操作",
      browser: "浏览网页",
      unknown: "未知",
    };
    const sample = attention?.sample;
    $("background-attention").textContent = !sample
      ? "尚无宿主信号"
      : `宿主信号${attention.stale ? "已过期" : "有效"} · 节点 ${state.active_node_id} · 活动 ${activityNames[sample.activity] || "未知"} · ${sample.sampled_at}`;
    if (activeNode?.context?.attention_collection)
      $("background-attention").textContent +=
        ` · ${attentionCollectionText(activeNode.context.attention_collection)}`;
    const focusNames = {
      unknown: "未知",
      unfocused: "未专注",
      candidate: "专注候选",
      focused: "持续专注",
      releasing: "结束缓冲",
    };
    $("background-focus").textContent = !focus
      ? "专注建议未知（尚无宿主信号）"
      : `专注建议：${focusNames[focus.state] || "未知"}${focus.stale ? "（信号过期）" : ""} · ${focus.protection_active ? "保护中" : "未开启保护"} · 持续 ${focus.duration_seconds} 秒 · 已完成 ${focus.completed_count} 段（宿主建议）`;
    const innerNames = {
      restless: "想靠近",
      curious: "好奇",
      concerned: "在意",
      reflective: "回想",
      content: "安然",
      resting: "想安静",
    };
    const currentInner = inner?.current;
    const innerSource = currentInner?.generator === "model_v1"
      ? `模型评判${currentInner.model ? `，置信 ${currentInner.model.confidence.toFixed(2)}` : ""}`
      : "本地规则";
    $("background-inner").textContent = !currentInner
      ? "尚无内在活动"
      : `内在活动（${innerSource}）· ${innerNames[currentInner.kind] || currentInner.kind} · 冲动 ${currentInner.urge.toFixed(2)} · ${currentInner.content}`;
    $("background-focus-segments").replaceChildren();
    for (const segment of (focus?.recent_segments || []).slice(0, 5)) {
      const item = document.createElement("li");
      item.textContent = `宿主建议记录 · ${segment.started_at} 至 ${segment.ended_at} · ${segment.duration_seconds} 秒`;
      $("background-focus-segments").append(item);
    }
    const rows = [...decisions.decisions].reverse().slice(0, 20);
    $("background-reason").textContent = rows.length
      ? `最新状态：${backgroundLabel(rows[0].reason || rows[0].status)}`
      : "尚无参与决策";
    $("background-decisions").replaceChildren();
    for (const row of rows) {
      const item = document.createElement("li"),
        source = document.createElement("small");
      item.textContent = `${backgroundLabel(row.reason || row.status)} · ${backgroundLabel(row.status)}`;
      const origin = row.origin === "inner" ? "内在活动" : "宿主边界";
      source.textContent = `${row.created_at} · ${origin} · 节点 ${row.node_id}`;
      item.append(source);
      $("background-decisions").append(item);
    }
  } catch (error) {
    if (valid())
      $("background-policy-status").textContent =
        `状态刷新失败：${error.message}。未保存的修改仍保留。`;
  } finally {
    if (valid()) {
      b.running = false;
      backgroundButtons();
    }
  }
}
async function saveBackgroundPolicy() {
  const b = session.background;
  if (!b.loaded || b.saving || !session.companion) return;
  const generation = session.generation,
    id = session.companion;
  const valid = () =>
    current(generation) && id === session.companion && b === session.background;
  b.request++;
  b.running = false;
  b.saving = true;
  backgroundButtons();
  try {
    const daily = Number($("background-daily").value),
      cooldown = Number($("background-cooldown").value);
    if (
      !Number.isInteger(daily) ||
      daily < 1 ||
      daily > 20 ||
      !Number.isInteger(cooldown) ||
      cooldown < 30 ||
      cooldown > 86400
    )
      throw new Error("请填写有效的每日次数和冷却秒数");
    const result = await api(path("/proactivity", id), {
      command_id: uuid(),
      expected_revision: b.revision,
      policy: {
        ...b.policy,
        enabled: $("background-enabled").checked,
        daily_limit: daily,
        cooldown_seconds: cooldown,
      },
    });
    if (!valid()) return;
    versions(result.revision);
    b.dirty = false;
    fillBackgroundPolicy(result);
    $("background-policy-status").textContent =
      `陪伴策略已保存 · 版本 ${result.revision}`;
    $("background-summary").textContent = result.policy.enabled
      ? "已开启 · 遵循安静边界"
      : "未开启";
  } catch (error) {
    if (valid()) {
      // Keep the user's values, but obtain a fresh CAS base for an explicit retry.
      const latest = await api(path("/proactivity", id)).catch(() => null);
      if (!valid()) return;
      if (latest) {
        b.policy = latest.policy;
        b.currentPolicy = latest.policy;
        b.revision = latest.revision;
      }
      $("background-policy-status").textContent =
        `保存失败：${error.message}。修改已保留；请核对后再次保存。`;
    }
  } finally {
    if (valid()) {
      b.saving = false;
      backgroundButtons();
    }
  }
}
$("background-form").onsubmit = action(saveBackgroundPolicy);
$("background-refresh").onclick = () => refreshBackground();
$("background-companionship").ontoggle = () => {
  if ($("background-companionship").open) refreshBackground();
};
for (const id of [
  "background-enabled",
  "background-daily",
  "background-cooldown",
]) {
  $(id).addEventListener("input", () => {
    session.background.dirty = true;
    $("background-policy-status").textContent =
      "有未保存修改；自动刷新不会覆盖。";
    backgroundButtons();
  });
}
setInterval(() => {
  if (
    !document.hidden &&
    (session.active || $("background-companionship").open) &&
    !session.background.running &&
    !session.background.saving
  )
    refreshBackground();
}, 5000);

function backgroundOutputReady(b = session.background) {
  const sample = b.attention?.sample;
  return Boolean(
    b.currentPolicy?.enabled &&
    b.state?.interaction_mode === "chat" &&
    b.state.active_node_id === session.node &&
    b.attention?.node_id === session.node &&
    !b.attention.stale &&
    sample &&
    Date.parse(b.attention.expires_at) > serverNow() &&
    sample.focus === "unfocused" &&
    sample.availability === "available" &&
    sample.user_present === true &&
    sample.locked === false &&
    sample.meeting === false &&
    sample.screen_sharing === false &&
    b.focus?.node_id === session.node &&
    !b.focus.stale &&
    Date.parse(b.focus.expires_at) > serverNow() &&
    !b.focus.protection_active &&
    b.focus.state === "unfocused",
  );
}

async function pollBackgroundOutput() {
  if (!authorized() || !session.token || !session.companion) return;
  const output = session.backgroundOutput;
  if (output.running) return;
  const generation = session.generation,
    id = session.companion,
    node = session.node,
    access = session.access,
    epoch = session.epoch;
  const identity = `${generation}:${id}:${node}:${access}:${epoch}`;
  if (output.identity !== identity) {
    output.identity = identity;
    output.cursor = null;
    output.seen.clear();
  }
  const valid = () =>
    output === session.backgroundOutput &&
    current(generation) &&
    authorized() &&
    id === session.companion &&
    node === session.node &&
    access === session.access &&
    epoch === session.epoch;
  if (output.cursor !== null && !backgroundOutputReady()) return;
  output.running = true;
  try {
    const query = new URLSearchParams({
      node_id: node,
      access_id: access,
      epoch: String(epoch),
    });
    if (output.cursor !== null)
      query.set("after_sequence", String(output.cursor));
    const baseline = output.cursor === null;
    const result = await api(path(`/proactivity/output?${query}`, id));
    if (
      !valid() ||
      result.companion_id !== id ||
      result.node_id !== node ||
      result.access_id !== access ||
      result.epoch !== epoch
    )
      return;
    versions(result.next_sequence, result.companion_revision);
    output.cursor = result.next_sequence;
    if (
      baseline ||
      !backgroundOutputReady() ||
      session.busy ||
      lastCommand()?.unresolved
    )
      return;
    if (!result.items.length) return;
    const [state, attention, focus] = await Promise.all([
      api(path("", id)),
      api(`/v1/nodes/${encodeURIComponent(node)}/attention`),
      api(`/v1/nodes/${encodeURIComponent(node)}/focus`),
    ]);
    if (
      !valid() ||
      state.active_node_id !== node ||
      state.epoch !== epoch ||
      state.revision !== result.companion_revision ||
      !backgroundOutputReady({
        ...session.background,
        state,
        attention,
        focus,
      }) ||
      state.interaction_mode !== "chat" ||
      !backgroundOutputReady() ||
      session.busy ||
      lastCommand()?.unresolved
    )
      return;
    // Revalidate all server-side context (policy, persona, emotion and references)
    // after the fresh host reads. The same cursor is a read, never a model request.
    const final = await api(path(`/proactivity/output?${query}`, id));
    if (
      !valid() ||
      final.companion_id !== id ||
      final.node_id !== node ||
      final.access_id !== access ||
      final.epoch !== epoch ||
      final.companion_revision !== result.companion_revision ||
      !backgroundOutputReady() ||
      session.busy ||
      lastCommand()?.unresolved
    )
      return;
    versions(final.next_sequence, final.companion_revision);
    output.cursor = Math.max(output.cursor, final.next_sequence);
    const initialEvents = new Set(result.items.map((item) => item.event_id));
    // No network await occurs between this final authorization and text rendering.
    for (const item of final.items) {
      if (!initialEvents.has(item.event_id) || output.seen.has(item.event_id))
        continue;
      output.seen.add(item.event_id);
      while (output.seen.size > 200)
        output.seen.delete(output.seen.values().next().value);
      conversation().messages.push({
        role: "assistant",
        content: item.assistant_message,
        proactive: true,
        event_id: item.event_id,
      });
      renderBubble("assistant", item.assistant_message, true);
    }
  } catch {
    /* No stale or historical output is replayed after an authorization failure. */
  } finally {
    output.running = false;
  }
}
setInterval(pollBackgroundOutput, 2000);

// ---------------------------------------------------------------- 人格编辑器
// POST /personality replaces the whole definition and every omitted field falls
// back to a server default, so the editor always submits the full object it read
// back from GET. The request model is strict, so traits/energy are sent as real
// numbers rather than strings.
const PF_FIELDS = {
  description: ["pf-description", 4096],
  personality: ["pf-personality", 2048],
  scenario: ["pf-scenario", 2048],
  first_message: ["pf-first-message", 1024],
  example_dialogue: ["pf-example-dialogue", 4096],
};
const PF_LISTS = {
  values: ["pf-values", 8, 128],
  boundaries: ["pf-boundaries", 8, 128],
  alternate_greetings: ["pf-alternate", 8, 1024],
};
const PF_TRAITS = ["warmth", "curiosity", "assertiveness", "playfulness"];

function lines(value) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function checkLength(label, value, max) {
  if (value.length > max)
    throw new Error(`${label}不能超过 ${max} 个字符（当前 ${value.length}）`);
}

function readList(label, value, maxItems, maxLength) {
  const items = lines(value);
  if (items.length > maxItems)
    throw new Error(`${label}最多 ${maxItems} 条（当前 ${items.length}）`);
  for (const item of items) checkLength(`${label}中的条目`, item, maxLength);
  return items;
}

function readRatio(label, value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0 || number > 1) {
    throw new Error(`${label}必须是 0 到 1 之间的数字`);
  }
  return number;
}

function fillDefinition(definition = {}) {
  for (const [field, [id, max]] of Object.entries(PF_FIELDS)) {
    $(id).value = String(definition[field] ?? "");
    if ($(id).value.length > max) $(id).value = $(id).value.slice(0, max);
  }
  for (const [field, [id]] of Object.entries(PF_LISTS)) {
    $(id).value = (definition[field] || []).join("\n");
  }
  const traits = definition.traits || {};
  for (const trait of PF_TRAITS)
    $(`pf-${trait}`).value = String(traits[trait] ?? 0.5);
  const style = definition.speech_style || {};
  $("pf-language").value = String(style.language ?? "zh-CN");
  $("pf-response-length").value = style.response_length || "brief";
  $("pf-tone").value = String(style.tone ?? "");
  $("pf-habits").value = (style.habits || []).join("\n");
}

function collectDefinition() {
  const definition = {};
  for (const [field, [id, max]] of Object.entries(PF_FIELDS)) {
    checkLength(field, $(id).value, max);
    definition[field] = $(id).value;
  }
  for (const [field, [id, items, length]] of Object.entries(PF_LISTS)) {
    definition[field] = readList(field, $(id).value, items, length);
  }
  const traits = {};
  for (const trait of PF_TRAITS)
    traits[trait] = readRatio(trait, $(`pf-${trait}`).value);
  definition.traits = traits;
  const style = {
    language: $("pf-language").value.trim() || "zh-CN",
    response_length: $("pf-response-length").value,
    tone: $("pf-tone").value,
    habits: readList("语言习惯", $("pf-habits").value, 8, 160),
  };
  checkLength("语言", style.language, 64);
  checkLength("语气", style.tone, 512);
  definition.speech_style = style;
  return definition;
}

function markPersonaDirty(dirty) {
  session.personaDirty = dirty;
  buttons();
}

async function loadPersona() {
  if (!session.companion || session.personaSaving) return false;
  const generation = session.generation;
  const id = session.companion;
  const request = ++session.personaLoadRequest;
  const valid = () =>
    current(generation) &&
    id === session.companion &&
    request === session.personaLoadRequest;
  session.personaLoading = true;
  buttons();
  try {
    const [profile, mood] = await Promise.all([
      api(path("/personality", id)),
      api(path("/personality/state", id)),
    ]);
    if (!valid()) return false;
    versions(profile.revision, mood.revision);
    session.personaRevision = profile.revision;
    session.personaLoaded = true;
    fillDefinition(profile.definition);
    $("persona-meta").textContent = profile.configured
      ? `版本 ${profile.revision} · 更新于 ${profile.updated_at || "未知"}`
      : `尚未设置人格（版本 ${profile.revision}），当前显示服务器默认值。`;
    $("ps-mood").value =
      (mood.recorded_state || mood.state || {}).mood || "calm";
    $("ps-energy").value = String(
      (mood.recorded_state || mood.state || {}).energy ?? 0.8,
    );
    $("ps-battery").value = String(
      (mood.recorded_state || mood.state || {}).social_battery ?? 0.7,
    );
    $("ps-ttl").value = "1800";
    const warnings = profile.import_warnings || [];
    showJson("persona-raw", {
      配置状态: profile.configured,
      导入警告: warnings.length ? warnings : "无",
      来源卡片: profile.source_card ? "已归档（见 JSON）" : "无",
      创建时间: profile.created_at,
      更新时间: profile.updated_at,
    });
    $("persona-prompt").textContent = "尚无预览";
    markPersonaDirty(false);
    return true;
  } catch (error) {
    if (valid()) throw error;
    return false;
  } finally {
    if (valid()) {
      session.personaLoading = false;
      buttons();
    }
  }
}

async function savePersona() {
  if (!session.companion) throw new Error("请选择伙伴");
  if (session.personaLoading || session.personaSaving)
    throw new Error("请等待人格载入或保存完成");
  if (!validVersion(session.personaRevision))
    throw new Error("请先载入人格编辑器");
  const generation = session.generation;
  const id = session.companion;
  const definition = collectDefinition();
  // A save supersedes every earlier read, including responses already in flight.
  session.personaLoadRequest++;
  session.personaSaving = true;
  buttons();
  try {
    const result = await api(path("/personality", id), {
      command_id: uuid(),
      expected_revision: session.personaRevision,
      definition,
    });
    if (!current(generation) || id !== session.companion) return;
    versions(result.revision);
    session.personaRevision = result.revision;
    fillDefinition(result.definition);
    await info();
    if (!current(generation) || id !== session.companion) return;
    markPersonaDirty(false);
    notice(`人格已保存，当前版本 ${result.revision}。记忆未受影响。`);
  } finally {
    if (current(generation) && id === session.companion) {
      session.personaSaving = false;
      buttons();
    }
  }
}

async function saveState() {
  if (!session.companion) throw new Error("请选择伙伴");
  const generation = session.generation;
  const id = session.companion;
  const mood = await api(path("/personality/state", id));
  if (!current(generation) || id !== session.companion) return;
  versions(mood.revision);
  const ttl = Number($("ps-ttl").value);
  if (!Number.isInteger(ttl) || ttl < 30 || ttl > 86400) {
    throw new Error("有效秒数必须是 30 到 86400 之间的整数");
  }
  await api(path("/personality/state", id), {
    command_id: uuid(),
    expected_revision: mood.revision,
    state: {
      mood: $("ps-mood").value,
      energy: readRatio("精力", $("ps-energy").value),
      social_battery: readRatio("社交电量", $("ps-battery").value),
    },
    ttl_seconds: ttl,
  });
  if (!current(generation) || id !== session.companion) return;
  await loadPersona();
  await info();
  notice("短期状态已更新；到期后回到人格基线。");
}

async function personaPreview() {
  if (!session.companion) throw new Error("请选择伙伴");
  const generation = session.generation;
  const id = session.companion;
  const state = await api(path("", id));
  if (!current(generation) || id !== session.companion) return;
  snapshot(state);
  const result = await api(path("/personality/preview", id), {
    user_message: $("message").value.trim() || "你今天过得怎么样？",
    user_initiated: true,
    audio_requested: false,
    token_budget: Number($("chat-prompt-budget").value) || 3072,
    memory_token_budget: Number($("chat-memory-budget").value) || 800,
    max_memory_items: Number($("chat-memory-items").value) || 8,
  });
  if (current(generation) && id === session.companion) {
    $("persona-prompt").textContent =
      `${result.context_text}\n\n---\n估算 ${result.estimated_tokens}/${result.token_budget} token` +
      (result.omitted_sections.length
        ? `\n未纳入：${result.omitted_sections.join("、")}`
        : "");
    $("prompt").textContent = result.context_text;
  }
}

// ---------------------------------------------------------------- 人格进化提议
// Proposals are generated from local evidence only; accepting one is the only
// path that writes the stable profile, and it is always an explicit owner choice.
const EVOLUTION_SKIP_REASONS = {
  insufficient_evidence: "证据不足",
  below_threshold: "未达阈值",
  open_proposal_exists: "已有同类未决提议",
  cooldown_active: "同类字段仍在冷却期",
  cumulative_drift_exceeded: "累计漂移已达上限",
  field_at_bound: "字段已到边界",
  calibration_requires_phase_c: "校准需阶段 C",
  model_semantics_required: "偏好语义需模型（阶段 C）",
};
const EVOLUTION_PROPOSAL_STATES = {
  proposed: "待决定",
  accepted: "已采纳",
  rejected: "已拒绝",
  superseded: "已被取代",
  expired: "已过期",
};

function proposalPatchText(operation) {
  if (operation.op === "adjust_trait") {
    const delta = `${operation.delta >= 0 ? "+" : ""}${operation.delta}`;
    return `${operation.field}: ${operation.before} → ${operation.after}（${delta}）`;
  }
  if (operation.op === "add_item") {
    return `新增 ${operation.field}：${operation.item}`;
  }
  return `替换 ${operation.field}：${operation.before || "（空）"} → ${operation.after || "（空）"}`;
}

async function evolutionList() {
  const generation = session.generation;
  const id = session.companion;
  const valid = () => current(generation) && id === session.companion;
  const page = await api(path("/personality/proposals?limit=50", id));
  if (!valid()) return;
  const panel = $("evolution-proposals");
  panel.replaceChildren();
  for (const proposal of page.items) {
    panel.append(evolutionProposalElement(proposal, valid));
  }
  if (!page.items.length) panel.textContent = "尚无演化提议；可先生成提议。";
  else if (page.has_more)
    panel.append(document.createTextNode("仅显示最近 50 条提议。"));
}

function evolutionProposalElement(proposal, valid) {
  const element = document.createElement("div");
  element.className = "evolution-proposal";
  const heading = document.createElement("p");
  const aggregate = (proposal.evidence.aggregates || [])
    .map((item) => `${item.metric} ${item.value}/${item.threshold}`)
    .join("，");
  heading.textContent =
    `${EVOLUTION_PROPOSAL_STATES[proposal.status] || proposal.status}` +
    ` · 置信 ${Math.round(proposal.confidence * 100)}%` +
    ` · 基于版本 ${proposal.base_personality_revision}` +
    ` · ${proposal.created_at}`;
  const rationale = document.createElement("p");
  rationale.textContent = proposal.rationale;
  const patch = document.createElement("ul");
  for (const operation of proposal.patch) {
    const item = document.createElement("li");
    item.textContent = proposalPatchText(operation);
    patch.append(item);
  }
  const evidence = document.createElement("p");
  evidence.className = "hint";
  evidence.textContent =
    `窗口 ${proposal.evidence.window_days} 天 · 样本 ${proposal.evidence.sample_size}` +
    (aggregate ? ` · ${aggregate}` : "");
  element.append(heading, rationale, patch, evidence);
  if (proposal.status === "proposed") {
    const controls = document.createElement("div");
    controls.className = "toolbar";
    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = "采纳并写入人格";
    const reject = document.createElement("button");
    reject.type = "button";
    reject.textContent = "拒绝";
    const decide = async (kind) => {
      if (!valid()) return;
      const pendingControls = [accept, reject];
      pendingControls.forEach((control) => {
        control.disabled = true;
      });
      try {
        const profile = await api(path("/personality", session.companion));
        if (!valid()) return;
        const body = {
          command_id: uuid(),
          expected_personality_revision: profile.revision,
          expected_proposal_revision: proposal.revision,
        };
        if (kind === "reject") body.reason = "webui";
        await api(
          path(
            `/personality/proposals/${encodeURIComponent(proposal.proposal_id)}/${kind}`,
            session.companion,
          ),
          body,
        );
        if (!valid()) return;
        await evolutionList();
        if (!valid()) return;
        if (session.personaLoaded) await loadPersona();
        await info();
        if (valid())
          notice(
            kind === "accept"
              ? "已采纳提议并写入人格。"
              : "已拒绝该提议，人格未改变。",
          );
      } catch (error) {
        if (valid()) notice(error.message, true);
      } finally {
        pendingControls.forEach((control) => {
          control.disabled = false;
        });
      }
    };
    accept.onclick = () => {
      void decide("accept");
    };
    reject.onclick = () => {
      void decide("reject");
    };
    controls.append(accept, reject);
    element.append(controls);
  }
  return element;
}

async function evolutionRefresh() {
  const generation = session.generation;
  const id = session.companion;
  const valid = () => current(generation) && id === session.companion;
  const result = await api(path("/personality/proposals/refresh", id), {
    command_id: uuid(),
    use_model: false,
    max_proposals: 1,
  });
  if (!valid()) return;
  const skipped = (result.skipped || [])
    .map(
      (item) =>
        `${EVOLUTION_SKIP_REASONS[item.reason] || item.reason}${
          item.metric ? `（${item.metric}）` : ""
        }`,
    )
    .join("，");
  $("evolution-status").textContent =
    `本次生成 ${result.generated} 条提议` +
    (skipped ? `；未生成原因：${skipped}` : "");
  await evolutionList();
  if (valid())
    notice(
      result.generated ? "已生成新提议，等待你的决定。" : "本次没有新的可采纳提议。",
    );
}

async function evolutionHistory() {
  const generation = session.generation;
  const id = session.companion;
  const valid = () => current(generation) && id === session.companion;
  const page = await api(path("/personality/history?limit=50", id));
  if (!valid()) return;
  const panel = $("evolution-history-list");
  panel.replaceChildren();
  const sources = {
    owner: "所有者编辑",
    import: "角色卡导入",
    evolution: "采纳演化提议",
    pre_migration: "迁移前记录",
  };
  for (const entry of page.items) {
    const element = document.createElement("div");
    element.className = "evolution-history";
    const heading = document.createElement("p");
    heading.textContent = `版本 ${entry.revision} · ${
      sources[entry.source] || entry.source
    } · ${entry.created_at}`;
    const detail = document.createElement("pre");
    detail.textContent = JSON.stringify(entry.definition, null, 2);
    element.append(heading, detail);
    panel.append(element);
  }
  if (!page.items.length) panel.textContent = "尚无版本历史";
}

// ---------------------------------------------------------------- 新页面接线
$("evolution-refresh").onclick = action(evolutionRefresh);
$("evolution-list").onclick = action(evolutionList);
$("evolution-history").onclick = action(evolutionHistory);
$("days-form").onsubmit = action(() => loadDays(true));
$("days-more").onclick = action(() => loadDays(false));
$("days-hide-emotion").onchange = () => renderDays();
$("devices-refresh").onclick = action(loadDevices);
$("local-device-refresh").onclick = action(loadDeviceOptions);
$("local-device").onchange = () => stop("本机设备关联已更改，请重新接入");
$("access-stop").onclick = () => stop("已停止本页对话；设备仍保留");
$("days-export").onclick = () =>
  download("events.json", {
    filters: dayFilters(),
    cursor: session.dayCursor,
    events: session.dayEvents,
  });
setInterval(() => {
  if (
    $("days-auto").checked &&
    !$("days-pane").hidden &&
    !document.hidden &&
    !session.dayRunning
  ) {
    action(() => loadDays(false))();
  }
}, 10000);

// ---------------------------------------------------------------- 连接日志（长连接）
// Long-lived connections (Web access sessions and node event streams) are
// traceable through the durable event log: opened, released, registered, auth
// rejected or disconnected. The window lists connection lifecycle events only.
const CONN_MAX_WALK_PAGES = 20;

function connFilters() {
  return {
    type: $("conn-type").value,
    node: $("conn-node").value,
  };
}

function connMatches(event, filters) {
  if (filters.type && event.type !== filters.type) return false;
  if (filters.node) {
    const related = [event.node_id, event.recipient_node_id].filter(Boolean);
    if (!related.includes(filters.node)) return false;
  }
  return true;
}

function connTone(type) {
  if (type === "node.auth_failed" || type === "node.disconnected") return "problem";
  if (type === "node.web_access_released") return "transfer";
  return "other";
}

function connSummary(event) {
  const node = event.node_id || "未知设备";
  switch (event.type) {
    case "node.web_access_opened":
      return `访问建立：${node}`;
    case "node.web_access_released":
      return `访问释放：${node}`;
    case "node.registered":
      return `节点注册：${node}`;
    case "node.auth_failed":
      return `鉴权失败：${event.payload?.stage || "auth"}`;
    case "node.disconnected":
      return "连接断开";
    default:
      return event.type;
  }
}

function renderConnectionLogs() {
  const filters = connFilters();
  const visible = session.connEvents
    .filter((event) => connMatches(event, filters))
    .slice()
    .reverse();
  const list = $("conn-list");
  list.replaceChildren();
  const summaryText = visible.length
    ? `已加载 ${session.connEvents.length} 条（游标 ${session.connCursor}）· 显示 ${visible.length} 条`
    : session.connEvents.length
      ? "没有符合筛选条件的事件。"
      : "尚无连接日志。刷新后按发生时间倒序显示。";
  $("conn-summary").textContent = summaryText;
  if (!visible.length) {
    list.textContent = summaryText;
    return;
  }
  let heading = null;
  for (const event of visible) {
    const key = dayDateKey(event.occurred_at);
    if (key !== heading) {
      heading = key;
      const title = document.createElement("h3");
      title.className = "day-heading";
      title.textContent = key || "时间未知";
      list.append(title);
    }
    const article = document.createElement("details");
    article.className = "day-event";
    article.dataset.tone = connTone(event.type);
    const summary = document.createElement("summary");
    summary.append(document.createTextNode(`${dayTime(event.occurred_at)} `));
    const strong = document.createElement("strong");
    strong.textContent = connSummary(event);
    summary.append(strong);
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent =
      `#${event.sequence} · ${event.type}` +
      (event.companion_id ? ` · ${event.companion_id}` : "") +
      (event.node_id ? ` · ${event.node_id}` : "");
    summary.append(meta);
    const raw = document.createElement("pre");
    raw.textContent = JSON.stringify(event, null, 2);
    article.append(summary, raw);
    list.append(article);
  }
}

async function loadConnectionLogs(reset = false) {
  if (!session.token || session.connRunning) return;
  const generation = session.generation;
  const request = ++session.connRequest;
  session.connRunning = true;
  buttons();
  const collected = [];
  let cursor = reset ? 0 : session.connCursor;
  let clipped = false;
  try {
    for (let page = 0; page < CONN_MAX_WALK_PAGES; page++) {
      const query = new URLSearchParams({
        after_sequence: String(cursor),
        limit: "500",
      });
      const node = $("conn-node").value;
      if (node) query.set("node_id", node);
      const result = await api(`/v1/events?${query}`);
      if (!current(generation) || request !== session.connRequest) return;
      if (
        !validVersion(result.next_sequence) ||
        !Array.isArray(result.events)
      ) {
        throw new Error("日志响应格式无效");
      }
      collected.push(...result.events);
      cursor = result.next_sequence;
      if (result.events.length < 500) break;
      if (page === CONN_MAX_WALK_PAGES - 1) clipped = true;
    }
    session.connEvents = reset ? collected : [...session.connEvents, ...collected];
    session.connCursor = cursor;
    renderConnectionLogs();
    if (clipped)
      notice(
        `事件较多，已加载最近 ${CONN_MAX_WALK_PAGES * 500} 条；更早的事件请用 API 分页。`,
      );
  } finally {
    if (current(generation) && request === session.connRequest) {
      session.connRunning = false;
      buttons();
    }
  }
}

$("conn-form").onsubmit = action(() => loadConnectionLogs(true));
$("conn-more").onclick = action(() => loadConnectionLogs());
$("conn-type").onchange = () => renderConnectionLogs();
$("conn-node").onchange = () => renderConnectionLogs();
$("conn-export").onclick = () =>
  download("connection-logs.json", {
    filters: connFilters(),
    cursor: session.connCursor,
    events: session.connEvents,
  });
setInterval(() => {
  if (
    $("conn-auto").checked &&
    !$("conn-pane").hidden &&
    !document.hidden &&
    !session.connRunning
  ) {
    action(() => loadConnectionLogs(false))();
  }
}, 10000);

$("persona-edit").onclick = action(async () => {
  if (
    session.personaDirty &&
    !confirm("编辑器中有未保存修改，确定重新载入服务器上的人格吗？")
  ) {
    return;
  }
  if (await loadPersona())
    notice("已载入服务器上的人格。修改后点击“保存人格”。");
});
$("persona-revert").onclick = action(async () => {
  if (await loadPersona()) notice("已放弃未保存修改。");
});
$("persona-form").onsubmit = action(savePersona);
$("state-save").onclick = action(saveState);
$("persona-preview").onclick = action(personaPreview);
for (const id of [
  "persona-file",
  "persona-import",
  "persona-fixture",
  "persona-card-sui",
  "pf-description",
  "pf-personality",
  "pf-scenario",
  "pf-first-message",
  "pf-example-dialogue",
  "pf-values",
  "pf-boundaries",
  "pf-alternate",
  "pf-warmth",
  "pf-curiosity",
  "pf-assertiveness",
  "pf-playfulness",
  "pf-language",
  "pf-response-length",
  "pf-tone",
  "pf-habits",
]) {
  $(id).addEventListener("input", () => markPersonaDirty(true));
  $(id).addEventListener("change", () => markPersonaDirty(true));
}

// Presentation follows session state; it never grants output ownership.
function updateShellStatus() {
  const connected = Boolean(session.token);
  const name = session.companion
    ? $("companions").selectedOptions[0]?.textContent || session.companion
    : "等待与你相识";
  if ($("companion-name")) $("companion-name").textContent = name;
  if ($("companion-avatar"))
    $("companion-avatar").textContent = session.companion
      ? Array.from(name)[0]
      : "✳";
  document.body.dataset.connection = session.connecting
    ? "connecting"
    : connected
      ? "connected"
      : "offline";
  document.body.dataset.ownership = authorized() ? "active" : "inactive";
  $("connection").dataset.state = document.body.dataset.connection;
  $("send").setAttribute("aria-busy", String(session.busy));
  $("send").textContent = activeStream
    ? "正在回复…"
    : session.busy
      ? "请稍候…"
      : "发送";
  $("messages").setAttribute("aria-busy", String(session.busy));
  const hint = !connected
    ? "连接服务器后，选择伙伴开始相识。"
    : !session.companion
      ? "选择一位伙伴，继续你们的日常。"
      : session.busy
        ? activeStream
          ? "伙伴正在回复，你可以继续阅读。"
          : "正在准备或读取回复，请稍候。"
        : lastCommand()?.unresolved
          ? "上一条回复结果尚未确认，请先查询原回执。"
          : !session.configured
            ? "服务器尚未配置对话模型，可先查看人格与记忆。"
            : session.personaRunning
              ? "人格正在更新，请稍候。"
              : !authorized()
                ? "点击「在此页对话」接入后即可发送。"
                : "准备好后发送；Enter 换行，Ctrl / ⌘ + Enter 发送。";
  const count = $("message-count");
  if ($("composer-hint"))
    $("composer-hint").replaceChildren(
      document.createTextNode(`${hint} `),
      ...(count ? [count] : []),
    );
  if (count) count.textContent = `${$("message").value.length} / 4096`;
  if ($("chat-empty"))
    $("chat-empty").hidden = Boolean($("messages").children.length);
}

let connectionReturnFocus = null;
function closeConnection() {
  if ($("connection-dialog")?.open) $("connection-dialog").close();
}

const contextMedia = matchMedia("(max-width: 1200px)");
let contextReturnFocus = null;
const contextInert = [];
function closeContext(restoreFocus = true) {
  document.body.classList.remove("context-open");
  $("context-backdrop").hidden = true;
  $("context-toggle").setAttribute("aria-expanded", "false");
  $("context-panel").removeAttribute("aria-modal");
  $("context-panel").removeAttribute("role");
  for (const element of contextInert.splice(0)) element.inert = false;
  $("context-panel").inert = contextMedia.matches;
  if (restoreFocus && contextReturnFocus?.isConnected)
    contextReturnFocus.focus();
  contextReturnFocus = null;
}
function openContext() {
  if (!contextMedia.matches) return;
  contextReturnFocus = document.activeElement;
  document.body.classList.add("context-open");
  $("context-backdrop").hidden = false;
  $("context-toggle").setAttribute("aria-expanded", "true");
  const panel = $("context-panel");
  panel.inert = false;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  // Preserve any pre-existing inert state and keep the backdrop operable.
  for (const element of document.querySelector(".app-shell").children) {
    if (element === panel || element === $("context-backdrop") || element.inert)
      continue;
    element.inert = true;
    contextInert.push(element);
  }
  $("context-close").focus();
}

for (const id of ["connection-open", "connection-open-rail"]) {
  if (!$(id)) continue;
  $(id).onclick = () => {
    closeContext(false);
    connectionReturnFocus = document.activeElement;
    if ($("connection-error")) {
      $("connection-error").textContent = "";
      $("connection-error").hidden = true;
    }
    $("connection-dialog").showModal();
    (session.token ? $("disconnect") : $("token")).focus();
  };
}
$("connection-close").onclick = closeConnection;
$("connection-dialog").addEventListener("close", () => {
  $("token").value = "";
  if (connectionReturnFocus?.isConnected) connectionReturnFocus.focus();
  connectionReturnFocus = null;
});
$("connection-dialog").addEventListener("click", (event) => {
  if (event.target !== $("connection-dialog")) return;
  const rect = $("connection-dialog").getBoundingClientRect();
  if (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  )
    closeConnection();
});
$("token").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    $("connect").click();
  }
});
$("context-toggle").onclick = () =>
  document.body.classList.contains("context-open")
    ? closeContext()
    : openContext();
$("context-close").onclick = () => closeContext();
$("context-backdrop").onclick = () => closeContext();
contextMedia.addEventListener("change", () => {
  const focusInside = $("context-panel").contains(document.activeElement);
  closeContext(false);
  if (contextMedia.matches && focusInside) $("context-toggle").focus();
});
document.addEventListener("keydown", (event) => {
  if (
    !document.body.classList.contains("context-open") ||
    $("connection-dialog").open ||
    $("linux-dialog").open
  )
    return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeContext();
  }
  if (event.key !== "Tab") return;
  const targets = [
    ...$("context-panel").querySelectorAll(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex="0"]',
    ),
  ].filter((element) => element.getClientRects().length);
  const first = targets[0];
  const last = targets[targets.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
});

const PAGE_COPY = {
  chat: ["对话", "从一句问候开始，续写属于你们的日常。"],
  persona: ["人格", "了解彼此的性格，也为陪伴留出新的可能。"],
  days: ["日子", "沿着时间，回望一起度过的片刻。"],
  conn: ["连接日志", "长连接的接入、断开与释放轨迹。"],
  memory: ["记忆", "留下值得记住的事，也可以澄清和修正。"],
  logs: ["记忆日志", "看看记忆如何沉淀，追溯每一次变化。"],
};
function showPage(tab) {
  if (!Object.hasOwn(PAGE_COPY, tab)) tab = "chat";
  for (const name of Object.keys(PAGE_COPY)) {
    $(`${name}-pane`).hidden = tab !== name;
    $(`${name}-tab`).setAttribute("aria-pressed", String(tab === name));
    if (tab === name) $(`${name}-tab`).setAttribute("aria-current", "page");
    else $(`${name}-tab`).removeAttribute("aria-current");
  }
  $("page-title").textContent = PAGE_COPY[tab][0];
  $("page-description").textContent = PAGE_COPY[tab][1];
  document.title = `${PAGE_COPY[tab][0]} · Virtual Life`;
  if (tab === "logs" && session.companion && !session.logs.length)
    action(() => loadLogs(true))();
  if (tab === "days" && session.token && !session.dayEvents.length)
    action(() => loadDays(true))();
  if (tab === "conn" && session.token) {
    if (!session.connEvents.length) action(() => loadConnectionLogs(true))();
    // The device filter is filled by the device panel, so prime it on demand.
    if ($("conn-node").options.length < 2) action(() => loadDevices())();
  }
  if (
    tab === "persona" &&
    session.companion &&
    !session.personaLoaded &&
    !session.personaDirty &&
    session.personaPageLoading !== `${session.generation}:${session.companion}`
  ) {
    const request = `${session.generation}:${session.companion}`;
    session.personaPageLoading = request;
    action(async () => {
      try {
        await loadPersona();
      } finally {
        if (session.personaPageLoading === request)
          session.personaPageLoading = null;
      }
    })();
  }
}
const pageNames = Object.keys(PAGE_COPY);
for (const [index, name] of pageNames.entries()) {
  $(`${name}-tab`).onclick = () => {
    if (location.hash === `#${name}`) showPage(name);
    else location.hash = name;
  };
  $(`${name}-tab`).addEventListener("keydown", (event) => {
    let next;
    if (["ArrowRight", "ArrowDown"].includes(event.key))
      next = (index + 1) % pageNames.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key))
      next = (index + pageNames.length - 1) % pageNames.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = pageNames.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    $(`${pageNames[next]}-tab`).focus();
    $(`${pageNames[next]}-tab`).click();
  });
}
window.addEventListener("hashchange", () => {
  const tab = location.hash.slice(1);
  if (Object.hasOwn(PAGE_COPY, tab)) showPage(tab);
});
const skipLink = document.querySelector('a[href="#main-content"]');
if (skipLink)
  skipLink.onclick = (event) => {
    event.preventDefault();
    $("main-content").focus();
  };
closeContext(false);
showPage(location.hash.slice(1));
updateShellStatus();

// Deployment state is page memory, independent of companion epochs and browser access.
const linuxDeployment = {
  revision: 0,
  fingerprint: null,
  busy: false,
  commandId: null,
  job: null,
  timer: null,
  pollUntil: 0,
};
const linuxStages = {
  queued: "等待分发",
  connecting: "连接 SSH",
  checking: "检查远端环境",
  uploading: "上传安装包",
  installing: "安装系统服务",
  verifying: "验证节点上线",
  completed: "分发完成",
  failed: "分发失败",
};
const linuxReasons = {
  linux_required: "目标必须为 Linux",
  python_312_required: "需要 Python 3.12+",
  systemd_required: "需要 systemd",
  unsafe_existing_config: "已有配置路径或大小不符合要求",
  existing_config_invalid: "已有节点配置无效",
  python_missing: "未找到 Python",
  python_version: "需要 Python 3.12+",
  python_too_old: "需要 Python 3.12+",
  systemd_missing: "systemd 不可用",
  privilege_required: "需要 root 或免密 sudo",
  sudo_required: "需要 root 或免密 sudo",
};
const linuxErrors = {
  ssh_connection_failed: "SSH 连接失败，请检查地址、端口和网络。",
  ssh_auth_failed: "SSH 认证失败，请重新输入密码或私钥。",
  ssh_host_key_changed: "主机指纹发生变化，请通过可信渠道重新核对。",
  ssh_key_invalid: "私钥格式或解密口令无效。",
  remote_check_failed:
    "环境检查失败，请确认 Python 3.12+、systemd 和安装权限。",
  remote_not_ready: "目标环境不满足要求，请先检查环境。",
  remote_identity_changed: "目标已有节点 ID 与本次填写不一致。",
  remote_server_changed: "目标已有安装绑定了另一台服务器。",
  remote_install_failed: "安装器返回失败，请检查目标服务状态后再操作。",
  remote_outcome_unknown:
    "SSH 中断，远端结果未知；请先检查目标服务，避免重复安装。",
  node_not_reporting:
    "安装器已返回成功，但未收到新节点心跳；请检查目标访问服务器的地址和网络。",
  deployment_interrupted: "服务器重启中断了任务，请先检查目标服务状态。",
  deployment_failed: "分发失败，请检查目标环境和服务状态。",
  forbidden: "节点身份或能力不适用此安装方式，请确认没有其他原生宿主。",
};
function clearLinuxSecrets() {
  for (const id of ["linux-password", "linux-key", "linux-passphrase"])
    $(id).value = "";
}
function invalidateLinuxHost() {
  linuxDeployment.revision++;
  linuxDeployment.fingerprint = null;
  $("linux-trust").checked = false;
  $("linux-trust").disabled = true;
  $("linux-fingerprint").textContent = "尚未获取指纹";
  $("linux-environment").textContent = "主机已改变，请重新获取并核对指纹。";
  linuxButtons();
}
function linuxButtons() {
  const connected = Boolean(session.token);
  const busy = linuxDeployment.busy;
  const trusted =
    connected && linuxDeployment.fingerprint && $("linux-trust").checked;
  $("linux-probe").disabled = !connected || busy;
  $("linux-inspect").disabled = !trusted || busy;
  $("linux-deploy").disabled =
    !trusted || busy || Boolean(linuxDeployment.commandId);
  $("linux-bundle").disabled = !connected || busy;
  $("linux-attention").disabled = busy || Boolean(linuxDeployment.commandId);
  $("linux-refresh").disabled =
    !connected || busy || !linuxDeployment.commandId;
  $("linux-revoke").disabled =
    !connected ||
    busy ||
    !linuxDeployment.job?.node_id ||
    ["queued", "running"].includes(linuxDeployment.job?.status) ||
    linuxDeployment.job?.credential_revoked;
  $("linux-new").disabled =
    busy ||
    !["completed", "failed", "interrupted"].includes(
      linuxDeployment.job?.status,
    );
}
function linuxMessage(message) {
  $("linux-message").textContent = message;
}
function linuxActive(revision, token) {
  return (
    $("linux-dialog").open &&
    linuxDeployment.revision === revision &&
    session.token === token
  );
}
function linuxHost() {
  const host = $("linux-host").value.trim();
  const port = Number($("linux-port").value);
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error("请输入主机和有效端口。");
  return { host, port };
}
function linuxSsh() {
  if (!linuxDeployment.fingerprint || !$("linux-trust").checked)
    throw new Error("请先核对并信任主机指纹。");
  const body = {
    ...linuxHost(),
    username: $("linux-user").value.trim(),
    host_key_fingerprint: linuxDeployment.fingerprint,
    python: $("linux-python").value.trim(),
  };
  if (
    !body.username ||
    !/^(?:[A-Za-z0-9_.-]+|\/[A-Za-z0-9_./-]+)$/.test(body.python)
  )
    throw new Error("请输入用户名；Python 仅支持程序名或绝对路径。");
  if ($("linux-auth").value === "password") {
    if (!$("linux-password").value) throw new Error("请输入 SSH 密码。");
    body.password = $("linux-password").value;
  } else {
    if (!$("linux-key").value.trim()) throw new Error("请输入 SSH 私钥。");
    body.private_key = $("linux-key").value;
    if ($("linux-passphrase").value)
      body.passphrase = $("linux-passphrase").value;
  }
  return body;
}
async function linuxOperation(operation) {
  if (linuxDeployment.busy || !session.token) return;
  const revision = linuxDeployment.revision,
    token = session.token;
  linuxDeployment.busy = true;
  linuxButtons();
  try {
    await operation(revision, token);
  } catch (error) {
    if (linuxActive(revision, token))
      linuxMessage(
        linuxErrors[error.code] ||
          "操作失败。请检查连接、主机指纹和认证信息后重试；SSH 凭据需重新输入。",
      );
  } finally {
    linuxDeployment.busy = false;
    linuxButtons();
  }
}
function renderLinuxJob(job) {
  linuxDeployment.job = job;
  $("linux-job-panel").hidden = false;
  $("linux-job-status").textContent =
    job.status === "interrupted"
      ? "任务已中断"
      : linuxStages[job.stage] || "正在查询任务";
  $("linux-job-detail").textContent =
    `任务 ${job.command_id} · ${job.host}:${job.port}${job.node_id ? ` · 节点 ${job.node_id}` : ""}${job.credential_revoked ? " · 凭据已撤销" : ""}${job.error ? ` · ${linuxErrors[job.error.code] || "安装未完成，请检查目标环境和服务状态。"}` : ""}`;
  linuxButtons();
}
async function queryLinuxJob(revision, token) {
  const id = linuxDeployment.commandId;
  if (!id || !linuxActive(revision, token)) return;
  try {
    const job = await api(
      `/v1/deployments/linux/${encodeURIComponent(id)}`,
      undefined,
      token,
    );
    if (!linuxActive(revision, token) || linuxDeployment.commandId !== id)
      return;
    renderLinuxJob(job);
    if (["completed", "failed", "interrupted"].includes(job.status)) {
      linuxMessage(
        job.status === "completed"
          ? "节点已完成分发，可刷新设备状态查看。"
          : "任务已结束，重新分发前请确认目标状态。",
      );
      return;
    }
    linuxMessage("分发进行中，关闭窗口后仍可重新打开查询原任务。");
  } catch {
    if (!linuxActive(revision, token)) return;
    linuxMessage(
      "暂未取得任务状态；保留原任务编号，恢复连接后查询，不会重复安装。",
    );
  }
  if (linuxActive(revision, token) && Date.now() < linuxDeployment.pollUntil) {
    clearTimeout(linuxDeployment.timer);
    linuxDeployment.timer = setTimeout(
      () => queryLinuxJob(revision, token),
      1000,
    );
  } else if (linuxActive(revision, token))
    linuxMessage(
      "已停止自动等待，请点击「查询原任务」继续查看。原任务编号已保留。",
    );
}
function openLinuxDeployment() {
  if ($("connection-dialog").open) $("connection-dialog").close();
  $("linux-server").value ||= location.origin;
  $("linux-dialog").showModal();
  linuxButtons();
  linuxMessage(
    session.token
      ? "先获取并核对 SSH 主机指纹。"
      : "请先在连接设置中连接服务器，再进行分发。",
  );
  if (linuxDeployment.commandId && session.token) {
    linuxDeployment.pollUntil = Date.now() + 300000;
    queryLinuxJob(linuxDeployment.revision, session.token);
  }
}
for (const id of ["linux-open-devices", "linux-open-connection"])
  $(id).onclick = openLinuxDeployment;
$("linux-close").onclick = () => {
  clearLinuxSecrets();
  $("linux-dialog").close();
};
$("linux-dialog").addEventListener("close", () => {
  clearLinuxSecrets();
  linuxDeployment.revision++;
  clearTimeout(linuxDeployment.timer);
});
$("disconnect").addEventListener("click", () => {
  clearLinuxSecrets();
  linuxDeployment.revision++;
  clearTimeout(linuxDeployment.timer);
  if ($("linux-dialog").open) $("linux-dialog").close();
});
for (const id of ["linux-host", "linux-port"])
  $(id).addEventListener("input", invalidateLinuxHost);
for (const id of ["linux-user", "linux-python"])
  $(id).addEventListener("input", () => {
    $("linux-environment").textContent = "配置已改变，请重新检查环境。";
  });
$("linux-auth").onchange = () => {
  clearLinuxSecrets();
  $("linux-password-label").hidden = $("linux-auth").value !== "password";
  $("linux-key-fields").hidden = $("linux-auth").value !== "key";
};
$("linux-trust").onchange = linuxButtons;
$("linux-probe").onclick = () =>
  linuxOperation(async (revision, token) => {
    const body = linuxHost();
    invalidateLinuxHost();
    // Invalidation deliberately creates a fresh identity for this probe.
    revision = linuxDeployment.revision;
    const result = await api("/v1/deployments/linux/host-key", body, token);
    if (!linuxActive(revision, token)) return;
    linuxDeployment.fingerprint = result.fingerprint;
    $("linux-fingerprint").textContent =
      `${result.key_type} · ${result.fingerprint}`;
    $("linux-trust").disabled = false;
    linuxMessage("请通过可信渠道核对 SHA256 指纹，再勾选信任。");
  });
$("linux-inspect").onclick = () =>
  linuxOperation(async (revision, token) => {
    const body = linuxSsh();
    clearLinuxSecrets();
    const result = await api(
      "/v1/deployments/linux/inspect",
      body,
      token,
      "POST",
      60000,
    );
    if (!linuxActive(revision, token)) return;
    const reasons = (result.reason_codes || []).map(
      (reason) => linuxReasons[reason] || "远端环境未满足安装条件",
    );
    $("linux-environment").textContent =
      `${result.ready ? "环境就绪" : "环境需要调整"} · Python ${result.python_version || "不可用"} · systemd ${result.systemd ? "可用" : "不可用"} · 权限${result.privileged ? "就绪" : "不足"}${result.existing_node_id ? ` · 已有节点 ${result.existing_node_id}` : ""}${reasons.length ? ` · ${reasons.join("；")}` : ""}。安装前请重新输入 SSH 凭据。`;
  });
$("linux-form").onsubmit = (event) => {
  event.preventDefault();
  if (linuxDeployment.commandId) return;
  linuxOperation(async (revision, token) => {
    const body = linuxSsh();
    body.server_url = $("linux-server").value.trim();
    body.label = $("linux-label").value.trim();
    body.attention_enabled = $("linux-attention").checked;
    if (!body.server_url || !body.label)
      throw new Error("请输入服务器地址和节点名称。");
    const node = $("linux-node").value.trim();
    if (node) body.node_id = node;
    body.command_id = uuid();
    linuxDeployment.commandId = body.command_id;
    clearLinuxSecrets();
    $("linux-job-panel").hidden = false;
    $("linux-job-status").textContent = "正在提交分发任务";
    $("linux-job-detail").textContent = `任务 ${body.command_id}`;
    linuxButtons();
    try {
      const job = await api(
        "/v1/deployments/linux",
        body,
        token,
        "POST",
        60000,
      );
      if (!linuxActive(revision, token)) return;
      renderLinuxJob(job);
    } catch (error) {
      if (!linuxActive(revision, token)) return;
      if (
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 425].includes(error.status)
      ) {
        linuxDeployment.commandId = null;
        linuxDeployment.job = null;
        $("linux-job-panel").hidden = true;
        linuxMessage(
          "服务器未接受此次分发，请检查表单和权限后重新提交；SSH 凭据需重新输入。",
        );
        linuxButtons();
        return;
      }
      linuxMessage("提交结果尚未确认；将查询原任务，请勿重复分发。");
    }
    linuxDeployment.pollUntil = Date.now() + 300000;
    await queryLinuxJob(revision, token);
  });
};
$("linux-refresh").onclick = () => {
  clearTimeout(linuxDeployment.timer);
  linuxDeployment.pollUntil = Date.now() + 300000;
  return linuxOperation(queryLinuxJob);
};
$("linux-new").onclick = () => {
  if (
    !["completed", "failed", "interrupted"].includes(
      linuxDeployment.job?.status,
    )
  )
    return;
  clearTimeout(linuxDeployment.timer);
  linuxDeployment.revision++;
  linuxDeployment.commandId = null;
  linuxDeployment.job = null;
  $("linux-job-panel").hidden = true;
  clearLinuxSecrets();
  linuxButtons();
};
$("linux-revoke").onclick = () =>
  linuxOperation(async (revision, token) => {
    const job = await api(
      `/v1/deployments/linux/${encodeURIComponent(linuxDeployment.commandId)}/credential`,
      undefined,
      token,
      "DELETE",
    );
    if (linuxActive(revision, token)) {
      renderLinuxJob(job);
      linuxMessage("此部署的节点凭据已撤销，远端程序仍保留。");
    }
  });
$("linux-bundle").onclick = () =>
  linuxOperation(async (revision, token) => {
    const response = await fetch("/v1/deployments/linux/bundle", {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) throw new Error("下载失败");
    const blob = await response.blob();
    if (!linuxActive(revision, token)) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "virtual-life-linux.tar.gz";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    linuxMessage("安装包已下载；手动安装说明见项目文档。");
  });
