# 流式聊天与断流恢复

聊天页面使用真实的提供商流，将生成中的文字逐段显示为临时回复。连接后立即呈现准备、思考和生成状态；收到已提交的最终回执后，重新核验设备访问、节点、租约和上下文，再写入一条正式回复。原 JSON 对话接口继续可用。

## 接口

`POST /v1/companions/{companion_id}/dialogue/stream` 使用原 `DialogueRequest`、Bearer token 和请求长度/预算限制。Web 访问仍需要 `access_id`。不增加 stream 请求字段；两个 POST 入口共享 command_id、请求指纹、领取记录、模型预留、事件和回执。

响应为 `text/event-stream`，同时发送 `Cache-Control: no-store`、`X-Accel-Buffering: no`。普通鉴权和请求体校验在开始流之前返回 HTTP 错误；开始后业务失败通过 error 事件报告，因此 HTTP 200 本身不代表完成。

```text
event: delta
data: {"command_id":"turn-1","companion_id":"sui","sequence":1,"text":"我在"}

event: delta
data: {"command_id":"turn-1","companion_id":"sui","sequence":2,"text":"我在，陪你。"}

```

| 事件 | 内容与行为 |
| --- | --- |
| status | `stage` 为 preparing / generating / tools / discarded。preparing 带 accepted_event_id，discarded 带 discard_reason，客户端立即撤下临时文字；tools 表示正在执行工具轮次。 |
| snapshot | `preview` 与生成所用的 `emotion` 快照。用于显示前复核，不写入聊天历史。 |
| delta | `sequence` 从 1 连续增加；`text` 是累计、已去自身姓名冒号前缀的文字，替换整个草稿而非追加。 |
| result | 完整的既有 `DialogueResult`。先提交数据库，再发送；状态为 completed / silent / discarded。 |
| error | `error: {status_code, code, message}`。终止显示，保留原命令供查询。 |

所有数据事件包含 command_id 与 companion_id。空闲 10 秒发送 `: ping` 注释；一个 result 或 error 结束订阅。没有终态的 EOF 是结果不明。协议没有 SSE id、续传游标或 Last-Event-ID 恢复。

已完成命令重放只发送 result，既不重新接受用户事件，也不调用模型。运行中重复命令返回 error / dialogue_in_progress；同 ID 不同请求返回 idempotency_conflict。流式执行后可从原 JSON 接口重放，反向也成立。

## 工具轮次

开启 `[tools].enabled` 后，一次流式命令最多包含一个工具轮次。provider 先返回工具调用（可跨多个分片），服务端校验名称、参数与调用数后执行，再发起不含工具的最终生成。执行期间发送 `status` 的 `tools` 阶段；工具轮次不发送 delta，最终文字从新的 sequence 开始。工具副作用已经提交，即使最终文字随后 discarded 也不回滚。超限或无效工具调用以 error 结束，且不执行任何调用。

## 临时显示与正式回复

提供商派发前、每个非空分段和最终提交时均校验所有者、epoch、活动 revision、在线显示能力、租约、待迁移、Web access、人设、有效手动状态、情感上下文及实际记忆/实体引用。途中任何一次失效会撤下草稿，并让最终回执保持 discarded，即使条件随后恢复也不会复活旧输出。模型用量仍按证据结算。

草稿不产生完成事件、不进入自动记忆，也不进入 WebUI 的历史、导出、诊断和手动保存来源。生成末尾情感更新可能早于浏览器处理 snapshot：页面此时隐藏草稿并继续读取，最终以回执内的情感版本重新验证。正式回复只入历史一次。

页面在本地权限丢失、停止、隐藏、掉线、过期或切换伙伴时撤下草稿并中止传输。显示草稿前读取最新服务端上下文，持续输出时定期复核；最终输出再独立复核。流式文字已被阅读后无法撤销既有阅读，但失效草稿不会成为正式记录。

消息列表仅在读者原本接近底部时跟随输出，向上阅读时不抢滚动位置。临时气泡有生成状态，使用 textContent 呈现，内容不解释为 HTML。

## 断流与用量

断开连接只撤销订阅，服务端工作线程继续接收提供商尾部用量并提交原回执。慢订阅的队列最多 32 项，投递阻塞超过 2 秒后脱离订阅，防止浏览器长期拖住模型任务。进程正常退出时等待这些工作线程完成。

断流、超时或解析失败后，用 `GET /v1/companions/{companion_id}/dialogue/{command_id}` 查询原回执。pending 为 409，尚无记录为 404；终态错误照常重放。不用新 ID 自动重发，也不在派发后静默退回 JSON 调用。页面只会在派发前已知缺少流能力时使用原 JSON 接口。

已知 usage 即使生成失败或文字被丢弃也结算。已派发但缺少 usage 保留未知预留；只有确认未派发的本地失败才释放。停止本页显示不取消这个文本接口的服务端模型任务。

原生连续语音使用独立的 [语音合同](voice-contract.md)：先提交持久 turn，再订阅 `/v1/nodes/{node_id}/voice/sessions/{session_id}/turns/{turn_id}/events`。其中 `text_delta.text` 也是累计草稿；`dialogue_result` 后合成仍可继续，直到 `speech_end` / `voice_end`。语音的显式 interrupt 会持久撤销旧 output generation；已经完成的文字回执保持不变，尚未播放的音频立即失去授权。断开语音 SSE 本身不等于用户插话，客户端先停本地播放、再发送 interrupt；不以断线取消冒充模型调用未发生。普通原生文字输入会停止当前语音会话，连续语音窗口内抑制后台主动输出。

## 提供商与边界

CompatibleChatProvider 发送 `stream=true`、`stream_options={include_usage:true}`，转发纯文本 `delta.content`，忽略推理字段。工具关闭时拒绝工具/函数调用；开启后接受带 `index` 的 `tool_calls` 分片，组装参数并在工具轮次结束前不发送文字草稿。仍拒绝非文本及多候选输出。要求合法的 stop/length（工具轮次为 tool_calls）结束原因、`[DONE]` 和最终 usage，接受中途 usage=null 以及尾部 choices=[] 的用量块。字段依据 [DeepSeek Chat Completions 官方文档](https://api-docs.deepseek.com/api/create-chat-completion/)。

兼容非终态 `finish_reason:null` 或空字符串；空字符串本身不能完成生成。真实 `sensenova-6.8-flash-lite` 流式与 JSON 对话已验证，首次失败的原因、修复及单次网络耗时见[真实模型验收](linux-attention-validation.md)。

解析支持跨字节的 UTF-8、CRLF、多行 data 和注释，不依赖 HTTP chunk 与 SSE frame 对齐。上游整次传输最多 2 MiB、单帧最多 256 KiB、回复纯文本最多 65536 UTF-8 字节。拒绝重定向与隐式重试，额外配置不能覆盖 stream 或 stream_options。原 JSON 接口和后台任务继续使用 complete。

向浏览器累计发送的 delta 总量最多 768 KiB，避免细碎分段导致累计正文的平方级传输增长；耗尽后停止临时更新，继续接收、校验并提交完整回复。WebUI 接收总量最多 2 MiB，单次总时限 120 秒，异常后恢复原回执。反向代理设置见[统一配置](configuration.md)。

自定义提供商可选择实现 `stream(request, on_delta) -> ModelResult`；回调接收增量纯文本，返回 text 必须等于所有回调原文拼接，最终需要有效用量。只实现 complete 的提供商兼容此 SSE 接口，返回单段草稿与回执。`/v1/capabilities` 的 `dialogue.streaming` 表示服务端提供该接口，`provider_streaming` 表示提供商支持真正增量生成。

Android SDK 提供 `RuntimeApi.streamDialogue(companionId, request, onEvent)` 与强类型 status/snapshot/delta/result/error。回调在 OkHttp IO 线程执行；宿主将 delta.text 替换临时文字，继续使用 LeaseGate，丢弃、错误或取消时清草稿。协程取消关闭传输，结果不明时通过 getDialogueResult 查询原命令。

## 可重现验证

后端测试 `backend/tests/test_model_streaming.py` 与 `backend/tests/test_streaming_dialogue.py` 使用确定性模型、MockTransport 和临时数据库，覆盖提供商分片、用量、幂等、生成中上下文失效、有限草稿与最终完整回执。浏览器脚本使用本地假模型、随机端口和临时 SQLite，不需要模型密钥：

```bash
uv run --with playwright python examples/webui_stream_smoke.py --browser-executable /path/to/chromium
uv run --with playwright python examples/webui_smoke.py --browser-executable /path/to/chromium
```

专项检查将模型暂停在首段之后，确认页面已出现中文草稿而数据库命令仍 pending，再验证唯一提交、首段后断流与原回执恢复、停止撤草稿、切伙伴隔离。通用 smoke 保留 19 项工作流与桌面/手机布局检查。默认截图和报告分别写入 `.data/webui-stream-review/` 与 `.data/webui-review/`。
