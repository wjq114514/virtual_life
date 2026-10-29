# Matrix 节点桥

把 Matrix（经任一 homeserver）接入 Virtual Life。Matrix 房间消息映射为对话轮次，用户在场/输入映射为 attention 样本（`user_present` + `activity=chat`），伙伴回复通过 Matrix `m.room.message` 回发。

参考 [AstrBot matrix adapter](https://github.com/stevessr/astrbot_plugin_matrix_adapter) 但**只实现 stdlib-only 子集**：密码/Token 认证、sync 长轮询、`m.text` 消息提取、`m.room.message` 发送、加入房间。**默认不实现** E2EE、SSO/OAuth2/QR、媒体、贴纸、线程、Reaction（如需要，请参考 AstrBot 完整实现）。

## E2EE（可选）

`e2ee: true` 时启用端到端加密支持。此时运行一个 Node 边车进程（`e2ee/sidecar.js`），由 `matrix-js-sdk`（含 RustCrypto WASM）负责 Olm/Megolm 密钥交换与解密/加密；Python 侧只做 stdio 桥接。边车在 `m.room.encrypted` 事件解密后以明文 `message` 事件上报，运行时与普通 `m.room.message` 走同一对话路径；发送经 `send` 命令交由边车加密投递。

配置：

- `e2ee`: `true` / `false`（默认 `false`）。
- `e2ee_sidecar`: 边车目录路径（默认 `客户端目录/e2ee`，需先 `cd e2ee && npm install`）。
- `e2ee_config`: 传给边车的对象，键为 `baseUrl`/`userId`/`accessToken`/`deviceId`/`syncTimeoutMs`；缺省时从 `homeserver`/`user_id`/`access_token` 自动补。

```json
{
  "e2ee": true,
  "e2ee_sidecar": "clients/matrix-node/e2ee",
  "e2ee_config": { "deviceId": "VLMX" }
}
```

边车依赖：Node ≥ 18 + `npm install`（`matrix-js-sdk`）。若边车无法启动（如 Node 缺失），客户端以错误码 3 退出。

运行（stdlib-only，Python 3.12+）：

```bash
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
python3 matrix_node.py --config example.json
```

配置字段见 `example.json`。要点：

- `node_id` 是注册到运行时的房间节点；它会成为伙伴的 `active_node_id`（当伙伴跟随到 Matrix 房间时）。
- `homeserver` 是 Matrix 服务器基址；`user_id` 是机器人账号（`@bot:server`）；`auth_method` 可选 `password`（需 `password`）或 `token`（需 `access_token`）。
- `rooms` 是 `[{room, companion}]` 绑定列表：每个 Matrix 房间 ID（`!id:server` 或别名）映射到伙伴 ID。
- `sync_timeout_ms` 是 sync 长轮询超时（默认 30000）；`interval_seconds` 是心跳间隔。

Matrix 事件映射：

| Matrix 事件 | Virtual Life 动作 |
| --- | --- |
| `m.room.message`（`m.text`，非本账号发送） | `POST /v1/companions/{id}/dialogue` |
| 用户发送消息 | `POST /v1/nodes/{node}/attention`（user_present=true, activity=chat） |
| 心跳 | `POST /v1/nodes/{node}/attention`（user_present=true） |
| 伙伴回复 | `PUT /_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}` |

依赖：仅标准库。`matrix_node/json_utils.py`、`matrix_node/api.py` 与 system-node/qq-node 风格一致（严格 JSON、字节/嵌套上限、无重定向）；`matrix_node/matrix.py` 用 `urllib.request` 实现 Matrix 客户端（登录、sync、发送、加入）；`matrix_node/runtime.py` 做事件→对话映射与发送队列；`matrix_node/e2ee_bridge.py` 桥接 Node 边车（E2EE 可选）。

## 测试

运行时事件映射与发送队列用标准库 `unittest` 覆盖（无真实 homeserver、无网络）；Matrix 客户端请求构建与响应解析用本地回环 opener 覆盖；E2EE 边车有真实 `node` 子进程冒烟测试（验证 ready/事件处理，无真实 homeserver）：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p "test_*.py"
```
