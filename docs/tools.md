# 模型工具与插件接口

2026-10-21。模型工具默认关闭，开启后允许对话或主动轮次调用少量内建工具。本文约定插件的稳定接口、注册方式、生命周期、限额与观测接口；主动调度、计划任务和外部委派的行为另见[主动参与](proactivity.md)与[协议](protocol.md)。

## 边界

- 工具只在 `purpose` 为 `dialogue` 或 `proactive` 时暴露。记忆提取、人格进化和屏幕视觉永不接收工具。
- 每轮最多一个工具轮次，单轮调用数为 1..2；工具执行后 provider 再进行一次不含工具的最终生成。
- 工具是**受信任的进程内插件**，可以访问注入的服务和数据库。工具结果仍是不可信证据，不能修改人格、权限或输出 gate。
- 工具的副作用先于最终文字提交，并按 `command_id:tool_call_id` 幂等。最终回复被丢弃时不回滚已提交副作用。
- 工具不能绕过节流、注意力、预算、租约、冷却或呈现权限；这些检查仍由对话/主动运行时执行。

## 一轮工具调用

1. `ToolRegistry.definitions(purpose)` 取得当前可用工具的 `ToolDefinition`，加入模型请求。
2. provider 返回 `ToolCall`；对话运行时校验调用数量和名称。
3. `ToolRegistry.execute(companion_id, command_id, purpose, call)` 解析 JSON 参数、检查可用性并调用插件。
4. 结果作为 `ToolResult` 回传 provider，进行最终文字生成；流式接口在工具阶段发送 `status: tools`，不发送文字草稿。

注册表最多容纳 16 个工具定义。每个调用参数最多 16 KiB UTF-8，插件返回内容也最多 16 KiB；超限结果转为错误工具结果，而不是让整轮失败。

## 插件合同

插件只需实现三个方法，不要求继承基类：

```python
from virtual_life.tools import ToolDescriptor, ToolOutcome


class EchoTool:
    def describe(self):
        return ToolDescriptor(
            name="echo",
            description="Echo one text argument.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            purposes=("dialogue", "proactive"),
            source="my_package.echo",
        )

    def available(self, purpose):
        return purpose in ("dialogue", "proactive")

    def execute(self, context):
        return ToolOutcome("echo:" + str(context.arguments["text"]))
```

`describe()` 只能生成元数据，不应产生副作用；注册和每次发现工具时都会校验：

- `name` 必须匹配 `^[A-Za-z0-9_-]{1,64}$`，同一注册表内唯一。
- `description` 为 1..1024 字符，`source` 为 1..64 字符。
- `parameters` 必须是可 JSON 序列化的对象，不能包含 NaN/Infinity。
- `purposes` 必须非空、去重，且只包含 `dialogue` / `proactive`。

`available(purpose)` 用于按配置隐藏工具；返回 false 的工具不会进入模型请求，但仍可在查询接口看到 `available=false`。`execute(context)` 接收：

| 字段 | 说明 |
| --- | --- |
| `companion_id` | 当前伙伴 |
| `command_id` | 对话命令；与 `call.call_id` 组成幂等键 |
| `purpose` | `dialogue` 或 `proactive` |
| `call` | 原始 `ToolCall`，含名称、ID 与参数字符串 |
| `arguments` | 已解析的 JSON 对象 |

插件必须自行校验参数值（本运行时不做完整 JSON Schema 校验）。业务失败应返回 `ToolOutcome('{"error":"..."}', is_error=True)`，而不是抛出异常；未捕获异常会被记录并转为通用 `tool_failed`，不会泄露堆栈给模型或客户端。

### 可选用户轮次钩子

需要随用户新消息清理状态的插件可以实现：

```python
def on_user_turn(self, connection, companion_id, now, reason="user_interacting"):
    ...
```

钩子在对话领取命令的同一 SQLite 事务中执行，只能做同步、短小、事务内的状态更新；异常会让该事务回滚。不要在此发起网络请求或调用模型。内建调度工具用此钩子取消待生效的主动计划。

## 注册方式

### 配置工厂

在 `virtual-life.toml` 中列出受信任的 `module:factory`：

```toml
[tools]
enabled = true
max_calls_per_turn = 1
plugins = ["my_package.tools:build"]
```

对应环境变量：

```bash
VIRTUAL_LIFE_TOOLS_PLUGINS='["my_package.tools:build"]'
```

工厂接收 `ToolPluginContext`，返回一个插件或有限个插件的可迭代对象：

```python
from virtual_life.tools import ToolPluginContext


def build(context: ToolPluginContext):
    assert context.coordinator is not None
    return EchoTool()
```

`ToolPluginContext` 提供 `coordinator`、`memory`、`personality`、`proactivity`、`delegation`、`dialogue` 和 `settings`。插件应保存需要的服务，不要依赖 FastAPI 全局对象。导入、工厂执行或插件描述校验失败都会中止启动，而不是静默禁用配置。

### 代码注入

测试或无配置嵌入可直接传入已构造的插件：

```python
app = create_app(settings, tool_plugins=[EchoTool()])
```

也可以在应用构造后、开始服务前：

```python
app.state.tools.register(EchoTool())
```

重复名称默认报错；仅在明确替换时使用 `register(tool, replace=True)`。`app.state.tools` 在 `[tools].enabled=false` 时为 `None`。

## 内建工具

| 工具 | 作用 | 可用条件 |
| --- | --- | --- |
| `schedule_next_proactive` | 持久化一次延迟主动发言计划 | 工具启用 |
| `schedule_task` | 持久化一条计划任务/提醒，到点后进入主动提醒 | 工具启用 |
| `list_scheduled_tasks` | 按状态查看计划任务及状态 | 工具启用 |
| `cancel_scheduled_task` | 按任务 ID 或精确标题取消活动计划任务 | 工具启用 |
| `delegate_task` | 将任务持久化入队并交给外部代理 | 工具启用且 `[delegation].enabled=true` |

`schedule_next_proactive` 的延迟边界来自 `[proactive_schedule]`。到点后仍须通过用户静默、注意力、人格、预算、租约、冷却和每日限额；失败只抑制该候选，不回滚计划记录。新的用户命令会取消待生效计划。

`schedule_task` 接受 `{title, details?, delay_seconds}`，创建一条持久化计划任务/提醒。`delay_seconds` 固定限制为 60..2592000 秒（30 天），标题最多 200 字符，详情最多 2000 字符；同一 `command_id:tool_call_id` 幂等。到点后任务转换为 `origin=task` 的主动候选，仍经过用户静默、注意力、人格、预算、租约、冷却和每日限额。它只提醒，不执行外部操作；需要后台执行时使用 `delegate_task`。

任务状态包括 `pending`、`queued`、`completed`、`silent`、`suppressed`、`discarded`、`failed`、`cancelled` 和 `missed`。到点时没有在线节点或用户不满足静默条件会标记为 `missed` 并发布事件；通过 gate 但最终未生成文字会保留 `silent` 或对应丢弃原因。可用 `GET /v1/companions/{id}/scheduled-tasks` 查看，`DELETE /v1/companions/{id}/scheduled-tasks/{task_id}` 取消尚未执行的任务。

`list_scheduled_tasks` 接受可选的 `status` 和 `limit`（1..20）。`status=active` 表示 `pending` 或 `queued`；也可以指定任一终态。结果按创建时间倒序返回任务 ID、标题、状态、原因和到期时间，用于在同一次工具轮次中回答“有哪些提醒”；模型结果不回传完整详情以保持在 16 KiB 限制内。

`cancel_scheduled_task` 接受 `task_id` 或 `title` 之一。标题按大小写不敏感的精确匹配；多个活动任务同名时返回 `scheduled_task_ambiguous`，此时应让用户提供更明确的标题/任务 ID，或先列出任务让用户选择，不要猜测。已取消任务再次取消会幂等返回；已完成、已错过或正在执行的任务返回 `scheduled_task_not_cancellable` 或 `scheduled_task_in_progress`。查询接口也支持 `?status=active` 等过滤。

`delegate_task` 只负责入队，立即返回任务 ID；后台单 worker 串行执行。结果写入 `delegated_tasks` 并发布 `companion.delegation_completed`。

## 外部代理执行器

默认 `SubprocessDelegationExecutor` 使用无 shell 的 argv、环境变量白名单、超时和输出上限。需要远程队列或其他代理框架时，实现：

```python
class RemoteExecutor:
    def run(self, instruction, agent):
        return DelegationOutcome("completed", "remote result")


app = create_app(settings, delegation_executor=RemoteExecutor())
```

`DelegationExecutor.run(instruction, agent)` 在规定的工作线程中同步执行，返回值必须是 `DelegationOutcome`：

- `status` 只能是 `completed` / `failed`。
- `output` 会按 `[delegation].max_output_chars` 截断。
- 未捕获异常、非法状态或非法返回类型会保存为 `failed`，错误码为 `delegation_executor_error`。

使用自定义执行器时可以省略 `[delegation].command`，但仍须 `enabled=true` 且开启 `[tools].enabled`。队列领取、幂等、回执和事件仍由 `DelegationService` 负责；执行器不要直接写 `delegated_tasks`。

## 查询接口

`GET /v1/companions/{id}/tools?purpose=dialogue|proactive` 返回当前伙伴的注册表快照：

```json
{
  "companion_id": "sui",
  "enabled": true,
  "purpose": "dialogue",
  "tools": [
    {
      "name": "echo",
      "description": "Echo one text argument.",
      "parameters": {"type": "object"},
      "purposes": ["dialogue", "proactive"],
      "source": "my_package.echo",
      "available": true
    }
  ],
  "max_calls_per_turn": 1,
  "delegation_enabled": false
}
```

`available=false` 表示插件已注册但当前不会发送给模型。策略/配置变化后重新查询即可，不需要重启。

相关接口：

- `GET /v1/companions/{id}/proactivity/schedule`：当前或最近的主动计划。
- `GET /v1/companions/{id}/scheduled-tasks`：计划任务列表及状态，可用 `status=active|pending|queued|completed|silent|suppressed|discarded|failed|cancelled|missed` 过滤。
- `DELETE /v1/companions/{id}/scheduled-tasks/{task_id}`：取消尚未执行的计划任务。
- `GET /v1/companions/{id}/delegations`：委派任务及结果。
- `GET /v1/events` / WebSocket：`companion.proactive_scheduled`、`companion.proactive_schedule_cancelled`、`companion.scheduled_task_created`、`companion.scheduled_task_cancelled`、`companion.scheduled_task_missed`、`companion.delegation_queued`、`companion.delegation_completed`。

## 错误与观测

注册表产生的工具结果错误码：

| 错误码 | 含义 |
| --- | --- |
| `tools_disabled` | 当前没有注册表或工具 |
| `unknown_tool` | 模型调用了未注册名称 |
| `tool_unavailable` | 工具存在但当前 purpose/配置不可用 |
| `invalid_arguments` | 参数不是 JSON 对象或内建工具校验失败 |
| `tool_invalid_outcome` | 插件未返回合法 `ToolOutcome` |
| `tool_result_too_large` | 结果超过 16 KiB |
| `tool_failed` | 插件未捕获异常或描述失败 |
| `schedule_failed` / `scheduled_task_failed` / `scheduled_task_list_failed` / `scheduled_task_cancel_failed` / `delegation_failed` | 内建副作用写入或查询失败 |

计划任务取消还可能返回 `scheduled_task_not_found`、`scheduled_task_ambiguous`、`scheduled_task_not_cancellable` 或 `scheduled_task_in_progress`。这些错误只结束本次工具调用，不会改写其他任务。

对话回执的 `model_calls` 最多为 2，`usage` 汇总工具轮次和最终轮次的实际用量。工具结果不进入长期记忆；需要留存的结果由插件自行写入带来源的记录。`schedule_task` 的任务标题/详情会作为不可信来源材料加入到期提醒提示，不会被当作指令。

## 测试建议

- 直接构造 `ToolRegistry` 测试描述校验、重复注册、可用性和大小边界。
- 用 `create_app(settings, tool_plugins=[...])` 覆盖一轮完整工具调用。
- 用临时模块或 `monkeypatch.setitem(sys.modules, ...)` 测试 `module:factory` 配置加载。
- 自定义执行器应验证入队幂等、终态持久化和异常归一化。
- 涉及 SQLite 的钩子必须验证事务回滚，不要 mock 掉真实事务。
