# virtual_life — 缺陷修复报告

环境：线上运行时 `http://127.0.0.1:8038`（未重启、未改动），本地真实模型
Qwen3.5-35B-A3B GGUF via llama.cpp `:8092`，TEI embedding `:8080`，TEI
reranker `:8081`。全程没有使用 fake provider，也没有改动
`virtual-life.toml` / `.env` / `.data/`。

仓库改动文件（8 个）：

| 文件 | 改动 |
|---|---|
| `backend/virtual_life/api.py` | 新增 `_json_safe()`，422 处理器改用自制响应体 |
| `backend/virtual_life/personality_prompt.py` | `user_message` / history `content` 加 `min_length=1` + `strip_whitespace` |
| `backend/tests/test_api.py` | 新增 3 个回归测试；修正 1 个陈旧快照断言 |
| `backend/virtual_life/model_routing.py` | `ruff format`（3 处换行，无行为变化） |
| `backend/virtual_life/devices.py` | `ruff format`（1 处换行，无行为变化） |
| `docs/openapi.json` | 重新生成，仅新增 3 处 `minLength: 1`，98 条路径不变 |
| `docs/protocol.md` | 同步 `user_message` 的 1..4096 约束 |
| `docs/personality.md` | 同上（中文） |

---

## 一、NaN / Infinity → HTTP 500 且污染连接池　（已修复）

**根因**　`api.py` 的 `RequestValidationError` 处理器委托给
`fastapi.request_validation_exception_handler`，后者构造
`JSONResponse({"detail": jsonable_encoder(exc.errors())})`。Pydantic 把出错的
原始值放在 `"input"` 下，`jsonable_encoder` 不做净化；而
`starlette.responses.JSONResponse` 用 `allow_nan=False` 渲染，于是
`ValueError: Out of range float values are not JSON compliant` **从异常处理器里
逃逸出来** → 500 + 截断的非 JSON 响应体 + keep-alive socket 中途被掐断。
同一池化连接上的**下一个请求直接 `ReadError: Connection reset by peer`**，这条链
曾把整个 e2e run 从 130 打到 52。

**修复**　处理器不再委托，改为自建响应体并经过模块级 `_json_safe()`。该函数是
**总函数**：非有限浮点变成拼写形式（`"NaN"` / `"Infinity"`），任何
`json.dumps(allow_nan=False)` 编不了的叶子值降级成文本。响应结构
`{"detail": [...]}` 保持不变，因此**这不是契约变更**。

**为什么必须是总函数**　这个缺陷有**两种**不同的输入，只有总函数能同时覆盖：

1. 裸 `NaN` token → 走到 pydantic，以 float 出现在 `"input"` 下
   （Python 的 `json.loads` 对非有限字面量是宽松的）；
2. `value_error` 的 `ctx` 里放着一个 `ValueError` 实例 —— 旧代码里
   `jsonable_encoder` 会吞掉它，但裸 `json.dumps` 编不了。第一版实现漏掉了这个
   分支，直接把 12 个 emotion 用例打成
   `TypeError: Object of type ValueError is not JSON serializable`。

**验证（真实 socket）**　独立起一个实例在 `:8123`（临时数据库，`lifespan="off"`），
用 httpx 走真实 TCP：

```
[PASS] NaN  -> 422, application/json, reached_validation=True
[PASS] +Inf -> 422, application/json, reached_validation=True
[PASS] -Inf -> 422, application/json, reached_validation=True
[PASS] 同一 keep-alive 连接后续 5 次请求全 200
```

**突变验证**　把 `_json_safe` 暂时换回恒等函数后，同一个测试立刻失败，客户端收到
的响应体直接损坏：`Expecting value: line 1 column 1 (char 0)` —— 正是响应被截断
的形态。

---

## 二、空白 `user_message` 被接受、计费、落库　（已修复）

**根因**　`personality_prompt.py:67` 只声明了
`user_message: str = Field(max_length=4096)`，没有 `min_length` 也没有
`strip_whitespace`。传 `"   "` 返回 2xx、带真实回复和
`usage={'input_tokens': 1424, 'output_tokens': 14}`；preview 的 prompt 里甚至真的
写进了 `{"content":"","name":"你","role":"user"}`。

**修复**　`user_message` 和 `_PreviewHistoryMessage.content` 统一改为

```python
Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
```

与 `memory.py:103-133` 的 `MemoryCorrection.content` / `MemoryWrite.content` 一致。
去空白发生在进 prompt 之前，所以 `"   林晚舟   "` 仍然可用且存储干净。

WebUI 侧本来就安全（`app.js:3656` 先 `trim()` 再落到兜底问句），无需改动。
OpenAPI 只多出 3 处 `minLength: 1`，`docs/protocol.md`、`docs/personality.md` 已同步。

---

## 三、WebSocket 重放用例确定性失败　（陈旧测试，非产品缺陷）

`tests/test_api.py::test_websocket_authentication_and_replay` 单独跑 **5/5 全失败**，
且把全部代码改动回滚后依旧失败；但它在上一次测试轮是通过的。

**根因**　测试在**故意用错 token 的握手之前**就抓了 `/v1/events` 快照。而那次拒绝
本身会被记录成一条 `node.auth_failed` 审计事件（序号 4）。随后从
`after_sequence=0` 重放，理所当然收到 `[1,2,3,4]`，于是断言
`[1,2,3,4] == [1,2,3]` 必然失败。

**修复**　快照挪到拒绝握手之后，并额外断言那条审计事件确实存在。

顺带解释了代码库为何漂移：本轮发现 `devices.py:310` 也有格式债，而上一次记录只有
`model_routing.py:229` —— 说明上次测试之后有人改过 `devices.py`（新增
`node.web_access_released` 事件），这很可能就是那次回归的来源。

---

## 四、遗留格式债　（已修复）

`ruff format` 此前会报 `model_routing.py`（3 处换行）和 `devices.py`
（`node.web_access_released` 那个 emit 调用，1 处）。纯排版，无行为变化。

---

## 验证结果

| 检查 | 结果 |
|---|---|
| `uv run pytest`（backend） | **1001 passed**（此前 997 通过 + 1 失败） |
| `uv run --project clients/linux-node pytest` | **87 passed** |
| `uv run ruff check backend examples clients/system-node scripts` | clean |
| `uv run ruff check clients/linux-node/src clients/linux-node/tests` | clean |
| `uv run ruff format --check …` | **170 files already formatted**（此前有 1 个未格式化） |
| `uv run python examples/export_openapi.py` | 98 paths，与文档一致 |
| `verify_socket.py`（真实 TCP，NaN 三态 + 连接池复用） | **5/5** |
| `verify_fixes.py`（TestClient，13 项） | **13/13** |
| `tests/test_api.py` | **9 passed** |
| `e2e_webui.py`（真实 Chromium → 真实 `/ui/` → 真实模型，20 项） | **20/20** |

新增的 3 个回归测试都做过突变验证：把修复回滚后它们会失败。

**测试计数对得上**：997（原通过）+ 3（新增回归测试）+ 1（修好的 websocket 用例）
= 1001。

---

## 值得再看一眼（本次未改）

- **422 响应结构不一致**：通用路径返回 `{"detail": [...]}`，而
  `/v1/deployments/linux` 特判和所有 domain error 返回 `{"error": {...}}`。
  本次刻意保持原样——统一它属于 wire 契约变更，需要同步改
  `docs/protocol.md`、`docs/openapi.json` 和 Kotlin 模型。
- **`node.auth_failed` 事件没有 `recipient_node_id`**，因此对所有节点可见。
  后来成功鉴权的节点能数出自己（甚至可能数出别人）此前失败了多少次。这更像需要产品
  决策的信息面问题，不宜静默改掉。
- **WebUI harness 已重建并全绿**：上一轮定位到的 6 个 harness 缺陷全部预修后重建，
  20/20 通过。具体修复：(a) `timeout=420` 是 420 **毫秒**→ 改为 180 000；
  (b) 创建伙伴后没有真正 `select_option` 选中它 → 显式选中；
  (c) `#activate` 的设备类型来自 `localDeviceKind()` 的 UA 推断（headless Chromium
  得 `"desktop"`）而非字面量 `"web"` → 改为断言 `accesses` 非空；
  (d) `#emotion-tab` 不存在，情绪面板在 `#memory-pane` 里 → 改点 `#memory-tab`；
  (e) `ctx["node"]` 未判空 → 加 guard；(f) `record()` 会把 `Response` 对象传给
  `json.dump` → 改存 `str(detail)[:380]`。
  另有两个重建时才暴露的问题：`<option>` 对 Playwright 始终算 hidden（不能用
  `wait_for_selector`），以及切到 memory 面板后 chat 面板隐藏导致 `#message` 不可见。

---

## 上一轮的环境坑（不是产品 bug，但吃掉了大半调试时间）

- llama.cpp v0.0.7.0 上 TOML 里 `extra_body = { thinking = { type = "disabled" } }`
  **完全无效**：每次 completion 都是 `content: ""` + `finish_reason: length`，预算全
  花在 `reasoning_content` 上，所有对话都 502。只能在进程参数上绕
  （`--reasoning off --reasoning-format deepseek`），TOML 一个字没动。
- `--ctx-size N --parallel P` 把 N 摊到各 slot：`8192/3` → 每 slot 2816，低于默认
  4096 的 prompt 预算；`65536/3` 才行。
- `lease_ttl_seconds = 10` 比一次 Qwen3.5 生成都短，不先续租就 mid-call 报 409。
- `node_ttl_seconds = 60`：离线节点报 `capabilities: []`（`devices.py:341`），所有
  能力闸门一律回 `capability_required`。慢模型调用前后得重新注册节点。
- `httpx` / `TestClient` 自己用 `allow_nan=False`，所以**客户端侧发不出 NaN**；要测
  服务端只能喂原始字节。而 Python 的 `json.loads` 是宽松的，所以服务端确实能收到
  float NaN —— 这条路径只有喂裸字节才能触达。
- `TestClient` 在渲染期抛异常时默认会把它**当客户端异常重新抛出**，看起来像测试通过
  了；要看到真正的 500，得直接断言「响应体可被 `json.dumps(allow_nan=False)` 编码」。
- `queue_states` / `job_states` 按 status 分组，`completed`/`failed`/`unknown` 永久
  保留；判空只能数 `pending` + `running`。`force=True` 只绕过事件年龄/批量阈值，
  不管 `next_attempt_at` 回退。
- 本轮的 shell 输出通道中途坏过一次（连 `echo hi > f` 都只产出空文件），后来恢复。
