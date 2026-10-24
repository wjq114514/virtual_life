# WebUI：对话测试与记忆查看

页面随 FastAPI 在 `/ui/` 提供，首页 `/` 跳转到这里。使用原生 HTML / CSS / JavaScript，不需要 Node 构建、CDN 或另外启动前端。页面使用已有 Bearer token 连接同源 API；token 只保存在当前页面内存，模型密钥由主服务器统一配置文件或环境变量配置。

## 页面范围

工作台包含五个工作区：对话、人格、日子、记忆、记忆日志。桌面使用侧栏导航和右侧伙伴状态栏；移动端使用底部导航，伙伴状态按需打开。右上角「未连接 / 已连接」和桌面侧栏「连接设置」打开令牌对话框，连接成功后返回当前工作区。连接错误在对话框内显示，关闭时清空输入。

支持 `#chat`、`#persona`、`#days`、`#memory`、`#logs` 链接定位与浏览器前进、后退；切页不重建表单或清空草稿。导航可用方向键、Home、End 操作，弹窗和窄屏状态抽屉支持 Escape、焦点约束与关闭后焦点归还。消息区显示发送状态、不可发送的原因和输入计数；请求预算、提示引用和单轮诊断按需展开。视觉资源全部同源，不请求 CDN 或外部字体。详细方案见 [WebUI 工作台重构](webui-redesign.md)。

- 连接、选择或创建 companion，查看当前人格摘要、短期状态与模型配置状态。
- 对话页：显式"在此页对话"、流式文本消息、近期历史、后台陪伴策略、专注建议与本地心理活动、提示预览、调用用量与实际记忆引用。
- 人格页：字段级编辑 `PersonalityDefinition`、性格特征、表达方式与短期状态；角色卡导入；提示预览。
- 日子页：运行事件时间轴，按本地日期分组、按发生时间倒序，支持类型/伙伴/设备筛选与导出；设备清单与节点图。
- 记忆页：分页记录与逐条编辑（固定、确认、标签、重要度、置信度、保留策略、更正、删除）、类型与当前版本、时间与来源、统计与预算内召回；展示自动整理状态、任务、近期摘要与关系。
- 记忆日志页：操作与状态筛选、游标分页与导出。

WebUI 是设备的访问方式，不再为每个页面注册临时 web 节点。选择 companion 只读；点击“在此页对话”后，以持久 browser_id、直连局域网 IP 或“本机设备”明确绑定复用节点，只为本页建立 access_id。不同设备才需要迁移，同设备重开只重新取得访问和租约。同设备仅允许一个活跃 Web 访问，其他页面仍可只读。详情见[设备身份](device-identity.md)。

浏览器只确认自己的 target ready，等待旧节点 released 或旧租约过期。接入等待和可见活动期间维护访问心跳，接入完成后维持短输出租约。隐藏/断开/切换 companion 时立即停止本地输出并尽力释放旧访问。发送前取得新鲜快照，回复后再次验证 access_id、当前节点、epoch、版本，以及访问和租约的单调期限。页面不会代替其他设备确认 released。

提示预览复用已有接口，不需要启用或调用模型。对话模型未配置时，发送按钮显示配置状态，仍可浏览记忆和预览提示。

所有服务端数据都用 `textContent` 渲染，页面不使用 `innerHTML`。访问令牌只保留在页面内存。

## 人格编辑

`POST /personality` 的语义是**整体替换**：每个缺省字段都会回落到服务器默认值，因此编辑器总是提交从 `GET /personality` 读回的完整 `definition`，不做部分提交。请求模型为 `strict`，性格特征与短期状态以真实数字发送。写入使用载入时的 `expected_revision` 做 CAS；版本不符返回 409，页面显示服务端消息且不推进本地版本号。

未保存的修改只标记为脏状态并启用"保存人格"，不会被静默丢弃；切换伙伴或放弃时显式确认。保存成功后重新读取人格与侧栏状态，并可用"预览当前人格提示"在不消耗额度的情况下查看实际提示内容。

短期状态通过 `POST /personality/state` 单独写入，带 `ttl_seconds`（30..86400），到期后回到人格基线。它与 `PersonalityService` 人格版本相互独立。

人格卡导入使用 `POST /personality/import`，会整体覆盖人格但不影响记忆。内置两张卡：`memory-test.v2.json`（记忆测试）与 `sui.v2.json`（随），都从 `/ui/` 以静态文件提供。真实人格卡不应放在该目录，因为静态目录不做鉴权。

## 日子（事件时间轴）

`GET /v1/events` 只支持 `after_sequence` **向前**读取，`after_sequence=0` 返回的是最老的一页。首次加载因此向前翻页直到到达尾部，翻页上限 20 页（10000 条），到达上限时在状态行明确告知已截断。后续刷新从上次游标继续，等同于跟随事件流尾部。

展示按浏览器本地时区分组（`Intl.DateTimeFormat`），组内按发生时间倒序。`companion.emotion_updated` 在每次情绪变动时都会写入，会淹没时间轴，因此默认隐藏，可用"隐藏情绪更新"复选框恢复；显式选择该类型时始终显示。类型、伙伴与设备筛选在客户端完成，设备筛选同时传给服务端以减少读取量。导出保留服务端顺序（序号升序），便于逐条回放。

设备与迁移区块使用 `GET /v1/nodes` 与 `GET /v1/graph` 展示节点在线状态、能力与最近心跳。页面不会代替其他设备确认 released，也没有提供转移中止入口；迁移只能等待释放或租约到期。

## 记忆记录编辑

每条 `active` 记录下提供编辑区：

- 元数据保存调用 `PATCH /memories/{memory_id}`，可改标签、重要度、置信度、固定、确认与保留策略，`expected_revision` 为该记录当前版本。
- 更正仅对 `semantic` 层记录开放，调用 `POST /memories/{memory_id}/corrections`，需要正文与一个属于该伙伴的真实来源事件；成功后旧版本变为已替代。
- 删除调用 `DELETE /memories/{memory_id}`，需要显式确认；服务端保存来源墓碑，自动整理不会重新生成该记录。

编辑区按需展开，默认不渲染，以保持记忆列表的浏览开销不变。

## 请求健壮性

JSON 请求经同一个 `api()` 包装：20 秒默认超时，对话为 120 秒；响应体先取文本再解析，非 JSON 响应（代理错误页或崩溃）会报告 HTTP 状态。流式对话使用独立的 fetch/ReadableStream 解析器，整体同样限时 120 秒，支持跨网络分片的 UTF-8、CRLF、多行 data 与心跳。超时、格式错误和网络中断均转换为中文提示并保留原命令恢复入口。`GET` 不发送 `Content-Type`。

## 流式聊天

服务端能力 `dialogue.streaming=true` 且浏览器支持流读取时，发送 `POST /dialogue/stream`，从真实提供商分段接收文字。页面立即显示连接、思考、生成状态，首段到达后逐段替换临时气泡；用户正在阅读旧消息时保持滚动位置。仅在发送前发现浏览器或服务端不支持时使用原 JSON 接口，流式请求发出后不自动切换接口重发。

临时文字不进入聊天历史、导出、单轮诊断或记忆来源。收到已提交的 result 后移除临时气泡，再经过原输出权限和上下文复核写入一条正式回复。快回复可能在页面处理 snapshot 前已改变情感版本，此时隐藏临时草稿，继续读取并复核最终结果。

停止本页对话、页面隐藏、掉线、访问过期和伙伴切换均立即撤下草稿并关闭传输。服务端会继续接收用量并保存原命令回执；这不等于取消模型调用。断流、缺失终态或格式异常后使用原“查询回执”，不会用新 command_id 自动重发。服务端 discarded 会撤下草稿并继续接收最终丢弃回执。帧结构、累计文字语义、预算和 SDK 接口见[流式对话协议](dialogue-streaming.md)。

## 后台陪伴

发送区下方的“后台陪伴”默认折叠，可显式保存主动参与开关、每日次数与冷却，查看当前宿主的注意力建议、持续专注段、本地心理活动和最近 20 条决策。心理活动由服务端规则生成，页面只展示，不把它当用户发言。只浏览不会修改策略；保存使用独立 revision/CAS，刷新保留未保存草稿，冲突后由用户核对再保存。

页面不采集或代填宿主注意力。原生宿主需要提供有效的 `attention.observe` 信号；未知、过期、专注、会议和静默状态均不授权主动发言；内在活动也必须通过同样的注意力与输出 gate。连续专注采用 60 秒确认与 30 秒释放保护，阈值是工程默认值。

主动文字只进入当前伙伴的消息展示区，标为“主动陪伴”，不伪造用户轮次，也不追加到发送给模型的聊天历史。页面接入、重开或切换伙伴后先建立实时游标基线，不补播历史。停止、隐藏、忙于前台对话或上下文变化时丢弃输出；呈现前重新读取注意力和专注状态，并再次向服务端核验同一事件。完整条件见[后台陪伴合同](proactivity.md)与[内在活动](inner-life.md)，隔离浏览器与 HTTP 验证见[本轮验证](background-flow-validation.md)。

## 单次对话合同

新增 `POST /v1/companions/{companion_id}/dialogue`，输入 `DialogueRequest`：

```json
{
  "command_id": "unique-command-id",
  "node_id": "my-desktop",
  "access_id": "this-page-access",
  "epoch": 1,
  "expected_revision": 1,
  "user_message": "我们刚才读到哪里了？",
  "user_name": "你",
  "user_initiated": true,
  "audio_requested": false,
  "history": [],
  "token_budget": 3072,
  "memory_token_budget": 800,
  "max_memory_items": 8,
  "max_output_tokens": 512
}
```

沿用 PersonalityPreview 的长度、预算、历史角色与 bounded JSON 限制；该文本接口只允许 user_initiated=true、audio_requested=false，输出预算为 1..1024。进入执行前检查当前节点、在线 text.display、epoch、租约、活动 revision 和没有待提交迁移。模型配置缺失返回 503 `model_not_configured`，不模拟角色回答。

响应 `DialogueResult`：`{command_id, companion_id, status, assistant_message, source_event_id, preview, model, usage, estimated_input_tokens, model_calls, created_at}`。status 为 completed / silent / discarded；usage 为 `{input_tokens, output_tokens}` 或 null。silent 不调用模型；discarded 已结算实际用量，但不返回助手文字。preview 是既有人格预览结果。回复后人格、有效状态、活动版本或输出条件改变时丢弃新输出。

新增 `GET /v1/companions/{companion_id}/dialogue/{command_id}` 查询回执。运行中的命令返回 409 `dialogue_in_progress`，相同命令换请求返回 409 `idempotency_conflict`。命令在所有 companion 间唯一。持久命令先领取，再在数据库事务外调用提供商，终态回执和事件一起提交；相同命令不会重复调用模型。失败结果保留，查询回执不触发重试；崩溃留下的运行中命令也不自动重发。

执行前通过 MemoryService 预留输入估算与输出上限，完成后按提供商 usage 结算。提供商消息包装也计入输入估算；真实 tokenizer 的偏差仍可能造成用量超出预留，账本保留实际用量。网络中断或缺失 usage 时不能假定免费：保留未知用量的预留，并返回明确失败。确定未执行的失败可释放预留。

`companion.dialogue_completed` 事件记录 `{command_id, user_message, assistant_message, memory_refs, memory_versions, usage}`，作为记忆保存的 provenance，并与自动整理入队记录原子提交；`companion.dialogue_silent` / `companion.dialogue_discarded` 事件仅记录 command_id/status，不发布助手文字，也不入队。命令回执是历史结果，重放不会授予新的节点输出权。自动整理调用独立记账，DialogueResult 的 usage 仅描述前台回复；后台费用见任务记录与统计。此阶段不提供工具调用或自主对话循环。

记忆页的“现在整理待积累对话”会提前处理一批 pending，仍遵循共享预算。结果不明确时刷新整理记录，页面不自动重发该请求。来源变更后过时摘要和关系不显示。配置和执行器恢复边界见 [自动记忆](automatic-memory.md)。

## 服务端模型配置

记忆页已增加向量索引状态、任务用量、一批索引、语义召回开关，以及图谱查询和实体别名编辑。语义召回缓存缺失时可调用向量模型；普通召回和人格预览仍在本地完成。图谱查询与别名编辑不调用模型。向量模型使用统一配置 `[embedding]`，与对话模型独立，索引和查询共用每日额度；详细合同见 [向量记忆与图谱](vector-memory-and-graph.md)。

实际对话在启用 `embedding.dialogue_recall` 时可先准备查询向量，原有结果 `model_calls` / `usage` 仅描述文本生成，`memory_retrieval` 单独描述查询成本或降级。页面分别显示二者。引用实际关系的实体版本也在模型派发前与回复提交时校验，别名变化可能导致结果丢弃。

CompatibleChatProvider 使用 HTTP Chat Completions 格式，发送有标签的完整人格上下文与固定执行指令和输出上限。流式聊天发送 `stream=true`、`stream_options={include_usage:true}`；原 JSON 对话及后台任务仍使用 `stream=false`。兼容 API 的字段参考 [DeepSeek 官方接口](https://api-docs.deepseek.com/api/create-chat-completion/)。不执行工具、下载资产或暴露模型密钥到浏览器。请求禁止重定向与隐式重试；返回有效 usage 才能作为已知用量结算。

统一配置文件的 `[model]` 章节集中设置模型参数，环境变量覆盖对应字段，加载与启动方式见 [统一配置](configuration.md)。

| 环境变量 | 默认 / 用途 |
| --- | --- |
| VIRTUAL_LIFE_MODEL_BASE_URL | 空；兼容 API 根地址，可含 `/v1` |
| VIRTUAL_LIFE_MODEL | 空；提供商模型名 |
| VIRTUAL_LIFE_MODEL_API_KEY | 空；服务端密钥，本地匿名接口可省略 |
| VIRTUAL_LIFE_MODEL_TIMEOUT_SECONDS | 60；HTTP 网络阶段超时 |
| VIRTUAL_LIFE_MODEL_EXTRA_BODY | `{}`；有界 JSON 参数，不能覆盖 model/messages/stream/stream_options/输出预算/工具/响应格式等执行字段 |

例如使用 [DeepSeek 官方当前示例](https://api-docs.deepseek.com/)的 `deepseek-flash`，在自己的 `virtual-life.toml` 中填写根地址、模型名与密钥；短对话可设置 `extra_body = { thinking = { type = "disabled" } }`。更换模型后重新启动服务。`[memory]` 控制现有运行时共享 token 额度；无实际调用就没有模型用量。

流式提供商须返回有效的最终 usage、finish_reason 和 `[DONE]`；缺失用量保留未知预留，不静默重发非流式调用。只实现 complete 的自定义提供商可以返回单段临时文字和最终回执，`dialogue.provider_streaming` 可区分它与真实模型流。网络阶段超时不等于整个任务的严格墙钟截止时间。中断后页面可以查原命令回执，失败保留为失败，不用新 ID 自动重发。未知预留不会因过了一天而抹去原记账日的记录。此阶段没有取消任务或自动修复崩溃命令的接口。

## 在页面测试记忆

选择伙伴后，在「人格」页点击“导入内置记忆测试人格”或“导入内置随人格”，或选择 JSON 人格卡后点击“导入所选文件”。按钮明确执行人格替换，读取最新人格 revision 后提交 `POST /personality/import`，服务端版本冲突不会自动重试。人格导入不删除已有记忆。内置卡位于 `/ui/memory-test.v2.json` 与 `/ui/sui.v2.json`，与 `examples/character_cards/` 下的同名文件相同。导入和对话均使用当前选中的伙伴。

对话历史按伙伴保存在当前页面内存中，切换标签、伙伴或断开连接后仍保留；刷新或关闭页面会丢失。不会把令牌、聊天内容或运行时数据写入 localStorage。请求仅携带最近 20 条有效消息，超出 JSON 字节预算时进一步裁剪历史；页面最多展示最近 200 条气泡。可复制文本或导出当前伙伴的完整本页对话及诊断 JSON。“清空本页对话（保留记忆）”仅清除该伙伴的页面历史、草稿及诊断，不修改服务器记忆。命令未确认或正在发送时禁止清空。

预算设置提供提示 token、记忆 token、记忆条数及输出 token 上限。输入长度统一为最多 4096 字符。每轮诊断保留 command_id、状态、source_event_id、实际 preview 记忆引用、文本 usage 和 memory_retrieval 向量召回成本或降级轨迹。状态不明确时发送保持锁定，查询原回执恢复结果，不产生新的命令 ID。回执只在原有节点、epoch、租约、人格和状态验证通过后显示助手文字。

## 记忆日志页面

“记忆日志”是独立导航页，调用 `GET /v1/companions/{id}/memories/logs`，以 `after_sequence` 游标每次读取最多 100 条，支持 operation、status 和 command_id 精确筛选。点击“应用筛选 / 刷新”从游标 0 开始，点击“加载后续日志”继续读取。可选每 5 秒自动读取后续记录，仅在页面和日志标签可见时执行，同一页面不重叠请求。切换伙伴、断开或请求上下文改变后旧响应不会更新页面。

页面最多保留最近 500 条已加载日志；导出只包含当前已加载的筛选结果，并附筛选条件和后续游标，不代表服务器全部历史。日志和人格卡中的内容均通过 textContent 展示，不执行 HTML。记忆日志可能包含用户信息，下载文件由用户自行保存。

### 可重复浏览器冒烟检查

运行 `uv run --with playwright python examples/webui_smoke.py`；首次使用需执行 `uv run --with playwright playwright install chromium`，也可用 `--browser-executable <路径>` 或 `PLAYWRIGHT_CHROMIUM_EXECUTABLE` 指定现有 Chromium。可用 `--artifacts <目录>` 指定输出。脚本在临时 SQLite 数据库和随机本地端口启动独立服务，注入确定性模型，不读取真实提供商配置；退出时停止服务并删除临时数据库。截图、筛选日志 JSON、事件导出和实际执行检查列表默认保存在 `.data/webui-review/`。该检查验证浏览器行为，不代表真实模型记忆质量。

当前检查覆盖：连接与伙伴创建、人格卡导入、租约接入与真实回复、Ctrl/⌘+Enter、历史角色与预算、来源与用量、XSS 文本渲染、情感展示与澄清、损坏回复锁定发送与原回执恢复、手动记忆及来源、伙伴切换后保留历史、真实日志游标分页与筛选导出、人格编辑器载入/编辑/CAS 保存/未触碰字段保留、过期 `expected_revision` 被拒、短期状态写入反映到情感、日子时间轴的日期分组与类型筛选、情绪噪音默认隐藏、设备清单与节点图、事件导出序号升序及记忆元数据编辑固定记录。新版工作台额外检查连接弹窗的键盘与焦点、设备复用与竞争访问、五个工作区的桌面/390px 布局、默认聊天首屏发送按钮不受底栏遮挡、状态抽屉的 Escape 与焦点归还、链接定位/前进/后退及令牌不写入存储。

## 陪伴状态与澄清

侧栏显示自动情感与精力；“反应经历与澄清”可查看六维规则评价、不确定性、原始与当前贡献及最近澄清。可按方向、强度、归因调整理解，也可完整撤回；前三类保留已发生的回复精力消耗。来源遗忘后隐藏解释，已撤回/遗忘的经历不再提供调整按钮。操作使用最新 emotion revision，切换伙伴后旧请求不刷新新伙伴界面。每轮诊断保存接受事件 ID、生成快照和完成快照；回执恢复不会重复刺激。呈现回复前复核当前情感 context_version 及原有输出权。详见[情感说明](emotion.md)。

## Linux 节点分发

连接设置与设备区域均可打开 Linux 分发窗口。输入 SSH 地址、确认主机指纹并提供本次密码或私钥后，可检查环境、安装 systemd 客户端及查看进度。目标仅领取自身状态上报凭据；安装成功需验证新凭据的心跳。窗口支持安装包下载与终态任务的凭据撤销，关闭及切换伙伴后仍能查询原任务。明确拒绝的请求允许修改重提；网络结果未知时保留任务 ID，避免重复安装。

分发时可显式勾选桌面采集，默认关闭，不自动开启主动陪伴。设备清单与后台面板只读显示采集来源、会话类型和未知原因；缺少桌面或权限时继续显示未知。范围、凭据与 X11/Wayland 限制见[自动采集](linux-attention-collection.md)。

要求、安装路径、升级身份与故障恢复见 [Linux 安装与 SSH 分发](linux-deployment.md)。可通过 `examples/webui_linux_smoke.py` 重现模拟任务的浏览器专项检查。
