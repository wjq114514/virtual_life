# MCP 桥接层

2026-10-27。把运行时已有的 REST 能力整体暴露为 MCP 工具，供 DeepSeek Harness 或任何 MCP 客户端直接调用。桥接层是**独立进程**，通过 HTTP 调用运行时，因此真实鉴权、严格请求校验、`command_id` 幂等等线级合同不会被绕过。

## 为什么是契约驱动

桥接层启动时读取运行时自己的 OpenAPI 文档（`GET /openapi.json`），把每个 operation 变成一个 MCP 工具。工具表的来源是运行时的契约本身，所以：

- 运行时改了接口，重启桥接层即生效，不需要手工同步工具定义；
- 桥接层不可能宣称一个运行时并不存在的操作；
- 参数 schema（类型、枚举、长度上限、必填、默认值）直接取自契约，模型填写时不会偏离。

## 运行

```bash
# Streamable HTTP：DSH 的 dsh-mcp-client 面向服务的标准形态
VIRTUAL_LIFE_TOKEN=local-dev-token \
uv run python -m virtual_life.mcp_bridge --base-url http://127.0.0.1:8038 --listen 127.0.0.1:8039

# Stdio：本地排查，或给其他 MCP 客户端
uv run python -m virtual_life.mcp_bridge --stdio --base-url http://127.0.0.1:8038

# 不需要客户端，直接列出或调用
uv run python -m virtual_life.mcp_bridge --list-tools
uv run python -m virtual_life.mcp_bridge --call capabilities_list '{}'
```

参数：`--base-url`（默认 `http://127.0.0.1:8038`）、`--token`（默认读 `VIRTUAL_LIFE_TOKEN`，再退到 `local-dev-token`）、`--openapi PATH`（从文件读契约而不是运行时）、`--timeout`（默认 120 秒）。

只有 `httpx`（运行时已有依赖）用于上游调用；JSON-RPC 2.0 与两种传输直接实现，不引入新包。

## 工具命名

路径去前缀 `v1`，各段以下划线连接，`{param}` 去掉花括号保留参数名，末尾按动词补后缀：

| 形态 | 例子 |
| --- | --- |
| GET 集合 | `nodes_list` |
| GET 单例 | `nodes_node_id_get` |
| GET 非复数资源 | `graph_get` |
| POST 创建 | `companions_create` |
| POST 单例动作 | `companions_companion_id_heartbeat` |

动词表：`get/post/put/patch/delete → get/create/replace/update/delete`。集合判定是启发式（末段以 `s` 结尾且不以 `ss/us/is` 结尾），唯一性是硬保证（同名冲突追加 `_2`）。工具名被压缩到 MCP 允许的 64 字符内，超长路径可能只保留前缀，实际语义以描述里的 `Upstream: METHOD /path` 为准。

## 结果与错误

`tools/call` 的结果是一个 `content[0].text`，内容为 JSON：

```json
{
  "tool": "companions_list",
  "upstream": {"method": "GET", "path": "/v1/companions"},
  "command_id": "mcp-9266954ad3f64647815f2cf1",
  "status": 200,
  "response": { }
}
```

成功时 `isError` 为 false；上游返回 4xx/5xx、连接失败时 `isError` 为 true，`text` 直接是 `Runtime returned HTTP 409:` 加原始错误体。JSON-RPC 协议层的错误（未知工具、缺路径参数、参数类型错）走标准 `error` 字段：`-32600` 无效请求、`-32602` 参数错、`-32002` 未知工具、`-32603` 内部错。结果正文上限 200000 字符，超出截断。

## command_id 的三种情况

桥接层不发明运行时的语义，只做三种确定处理：

1. **契约在 JSON body 里声明 `command_id`**（对话、转移、语音轮次等）：调用方没给就生成 `mcp-<uuid12>`，同时写进 body 和 `Idempotency-Key` 头，二者一致。调用方给了就原样使用。
2. **契约没有 `command_id` 字段**（节点、同伴创建、租约续期等）：生成一个仅用于 `Idempotency-Key` 头的键，**不进 body**。运行时的严格校验会拒绝未知字段，所以这个剥离是必须的。
3. 结果里始终回显 `command_id`，失败后可以用同一个键精确重试。

运行时的幂等语义是逐路由的：记忆写入与知识图谱别名按 `command_id` 去重并做冲突检测，屏幕与语音服务用 `command_id` 加内容指纹回放进站收据；而 `POST /v1/companions` 的天然幂等键是 `companion_id`（重复返回 `409 already_exists`），并不看 `command_id`。桥接层不能也不应统一这一层。

## 被排除的 operation

工具结果装不下字节流，以下按媒体类型和路径自动剔除：

- SSE 流：`/v1/companions/{companion_id}/dialogue/stream`、`/v1/nodes/{node_id}/voice/sessions/{session_id}/turns/{turn_id}/events`
- 音频与二进制：`.../turns/{turn_id}/segments/{sequence}/audio`、`/v1/nodes/{node_id}/screen/uploads/{upload_id}/content`
- 会话握手与包分发：`/v1/session`、`/v1/dist/packages*`、`/v1/deployments/linux/bundle`

保留 `GET /v1/events`（持久事件日志，纯 JSON）与 `POST /v1/companions/{companion_id}/dialogue`（非流式对话）。当前契约生成 106 个工具。

## 在 DSH 里接入

`@deepseek-ai/dsh-mcp-client` 支持 Streamable HTTP，工具命名为 `mcp__<serverName>__<tool>`：

```yaml
- id: mcp-virtual-life
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: virtual_life
    transport: streamable-http
    url: http://127.0.0.1:8039/mcp
    headers:
      Authorization: !!js '`Bearer ${process.env.VIRTUAL_LIFE_TOKEN}`'
```

该包与当前 profile 同版本（`0.2.1-rc.3`），9 个 peer 依赖（`attachment`、`llm`、`scope`、`subprocess`、`timeout`、`tools`、`mcp-resources`、`system-prompt`、`cordis`）全部已满足。接入方式与选型分析见 [DSH 插件迁移分析](dsh-plugin-migration-analysis.md)。

## 验证

- `uv run pytest backend/tests/test_mcp_bridge.py -q`：20 个测试，覆盖契约生成、工具名唯一性与合法性、流式/二进制剔除、零参数读接口、参数分流（路径/查询/body）、`command_id` 三种情况、JSON-RPC 分发、批处理、通知丢弃、HTTP 会话流与 405。
- 端到端（真实运行时 `127.0.0.1:8038`、本地 Qwen 模型 `8092`）：节点注册 → 同伴创建 → 房间迁移 prepare/ready/released/committed → 租约续期 → 对话返回 `status: completed` 与 `assistant_message`，情绪评估同时产出 `valence/arousal/control`、`mood: calm`、`energy: 0.8`。
- `ruff check` 与 `ruff format --check` 在 `backend examples clients/system-node scripts` 全量通过。
