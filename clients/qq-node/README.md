# QQ 节点桥（NapCat）

把 QQ（经 **NapCat** 的 OneBot v11 双通道）接入 Virtual Life。QQ 私聊消息映射为对话轮次，「正在输入 / 在线」映射为 attention 样本（`user_present` + `activity=chat`），伙伴回复通过 NapCat HTTP 接口 `send_private_msg` 回发。

NapCat 按 OneBot v11 部署为两个通道：

- **反向 WebSocket**：NapCat 的 `websocketClients[].url` 指向本进程的 `websocket_url`，NapCat 主动连入并把事件（`post_type`：message / notice / meta_event）推给我们。
- **HTTP 服务器**：NapCat 的 `httpServers` 接收动作调用；我们 `POST {http_url}/send_private_msg`，带 `Authorization: Bearer <access_token>`。

运行（stdlib-only，Python 3.12+）：

```bash
VIRTUAL_LIFE_API_TOKEN=local-dev-token \
python3 qq_node.py --config example.json
```

配置字段见 `example.json`。要点：

- `node_id` 是注册到运行时的房间节点；它会成为伙伴的 `active_node_id`（当伙伴跟随到 QQ 房间时）。
- `websocket_url` 是本进程监听的反向 WS 地址（NapCat 连入）；`http_url` 是 NapCat 的 HTTP 动作服务器地址；`access_token` 是两侧共用的 OneBot 令牌（可空，需与 NapCat 配置一致）。
- `companions` 是目前绑定的伙伴 ID 列表（每个 QQ 用户映射到列表第一个伙伴）。
- `test_user_id` 是好友测试消息的固定接收人（QQ 号），避免误发他人。

OneBot v11 事件映射：

| QQ 事件 | Virtual Life 动作 |
| --- | --- |
| `post_type=message`（私聊） | `POST /v1/companions/{id}/dialogue` |
| `notice_type=typing`（正在输入） | `POST /v1/nodes/{node}/attention`（user_present, activity=chat） |
| `notice_type=offline` | `POST /v1/nodes/{node}/attention`（user_present=false） |
| `meta_event`（心跳） | `POST /v1/nodes/{node}/attention`（user_present=true） |

依赖：仅标准库。`qq_node/json_utils.py`、`qq_node/api.py` 与 system-node 风格一致（严格 JSON、字节/嵌套上限、无重定向）；`qq_node/onebot.py` 用 `http.server` + 原生 socket 实现反向 WS 服务器，用 `http.client` 实现 NapCat HTTP 动作客户端。

## 测试

运行时事件映射与发送队列用标准库 `unittest` 覆盖（无真实 NapCat、无网络）；反向 WS 握手与 HTTP 动作客户端用本地回环 socket 覆盖：

```bash
python3 -m unittest discover -s clients/qq-node/tests -p "test_*.py"
```
