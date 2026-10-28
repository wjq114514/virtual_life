# 后台专注保护与主动陪伴验证

2026-10-15，在已有注意力边界自触发之上，补齐持续专注状态、保护滞回、WebUI 策略入口和主动文字呈现。这里记录的是宿主建议的专注工作，不是经过校准的主观心流测量；本轮也没有新增伙伴独立思考或无边界定时问候。

## 交付行为

- 第一个有效专注样本立即保护，连续有效样本达到 60 秒才确认专注段；普通结束需后续样本确认持续 30 秒，明确且安全的任务结束或休息边界可立即释放。阈值是工程默认值，读取接口不会按墙钟自行确认、产生边界或调用模型。
- 专注状态与注意力、事件和候选在同一事务内更新，支持重启恢复、重复请求及回滚。每节点保留最近 100 段，读取最近 20 段，累计完成数不随历史清理减少。
- 聊天页新增折叠的“后台陪伴”，显式保存策略，保留草稿并处理版本冲突；可查看宿主状态、专注段及最近 20 条决策。浏览页面不创建注意力信号。
- 主动文字只向仍有输出权的当前访问呈现，单独标记，不伪造用户轮次、不进入前台模型对话历史或自动记忆。新接入建立游标基线；生成后遇到专注、未知、会议等不安全注意力变化，即使后来恢复空闲，也不补播旧问候。
- 服务端重查节点、epoch、租约、访问、策略及人格/情感/记忆上下文；浏览器重新读取宿主状态后再次请求服务端核验同一事件，最终响应通过本地 gate 后同步呈现。历史回执不授予输出权限。

合同与字段见[注意力与自触发](proactivity.md)、[协议](protocol.md)、[WebUI](webui.md)及 [OpenAPI](openapi.json)。Kotlin SDK 同步专注快照、最近决策查询和实时文字读取。

## 验证结果

全部测试使用临时 SQLite、本地随机端口和确定性模型，没有读取或改动用户 `.env`、生产数据库、现有运行服务，也没有连接家庭设备或外部 LLM。

| 检查 | 实际结果 |
| --- | --- |
| 完整后端 pytest | **646 passed**，244.82 秒；一条既有 Starlette/httpx 弃用警告 |
| 专注与实时输出 | 覆盖确认/释放、短暂波动、显式边界、未知/过期/会话切换、幂等、原子回滚、重启、历史上限、最新决策分页和 receipt/候选状态提交竞态 |
| 旧输出抑制 | 覆盖策略/人格/状态变更、无 revision 变化的短期状态过期，以及不安全注意力后恢复仍不补播；已有生成链路继续覆盖情感纠错及引用有效性 |
| 后台浏览器专项，8 组 | 真实 Chromium + Uvicorn + SQLite；只读无隐式写入、CAS/草稿、宿主专注保护、单次纯文字、伙伴隔离、专注晚包丢弃、关闭策略晚包复核、桌面/手机布局；无页面运行错误 |
| 原有 WebUI 工作流，19 组 | 对话、情感、人格 CAS、记忆、设备身份、日志、导航与桌面/手机布局通过；发送按钮恢复首屏可见 |
| 流式浏览器回归，5 组 | 暂停模型后首段可见且数据库 pending；唯一提交、中断恢复、停止及伙伴切换隔离；无页面运行错误 |
| 实际 HTTP 模型链路，4 组 | 本地确定性 HTTP 模型经真实 JSON/SSE 传输；lifespan 自动消费、专注/会议/quiet 抑制、实时基线/投递/去重、进入专注后不补播、情感纠错与中文流式回复；共 4 次模型 HTTP 请求 |
| 静态检查 | Ruff 检查及格式检查通过，95 个 Python 文件；JavaScript 语法和 UI 三文件 Prettier 检查通过 |
| 协议与客户端 | OpenAPI 导出 59 个路径；SDK assemble/exampleClasses 编译通过，序列化探针验证整秒可读为 Double |
| wheel | 构建通过，专注/输出模块和 UI 三资源与源码字节一致；脱离源码导入并生成原 Linux 安装包，SHA256 保持一致 |

本轮发现并修正：可选后台面板放在消息区上方会把 900 像素高桌面的发送按钮挤出首屏，现放在发送区下方；旧响应呈现前增加宿主新鲜状态和服务端二次复核；输出读取兼容对话回执已提交但候选状态尚未更新的短暂窗口。

原工作流复跑曾在创建伙伴前置步骤超时，当时尚未执行检查项，无页面异常或 HTTP 5xx；随后保留原断言、仅增加 HTTP 观察的诊断复跑通过 19 组，最终又使用未修改的原脚本独立通过全部 19 组。该偶发情况尚未复现或确定根因，没有据此修改产品创建流程或放宽断言。

## 复现与证据

```sh
uv run pytest -q
uv run python examples/runtime_live_test.py
uv run --with playwright python examples/webui_smoke.py --help
clients/android-node/build-sdk.sh assemble exampleClasses
```

浏览器脚本支持 `--browser-executable` 与 `--artifacts`，会启动独立本地应用并在结束时清理临时数据库及服务。本轮非秘密证据：

- `.data/background-flow-review/pytest.txt`、`wheel-check.json` 和 `wheel/`。
- `.data/proactivity-ui-review/report.json`、`late-output-evidence.json`、桌面/手机截图。
- `.data/proactivity-ui-existing-regression/report.json`。
- `.data/background-flow-review/webui/report.json` 为最终未修改原脚本的回归；发送按钮底边在桌面约 837/900 像素、手机约 748/844 像素且高于底栏。
- `.data/background-flow-review/stream/report.json` 和首段/切换截图。
- `.data/background-flow-review/runtime-live-test.json`（保留本轮快照），原脚本默认输出 `.data/runtime-live-review/runtime-live-test.json`。

## 尚未验证的部分

尚未交付系统注意力采集器；Linux CPU、内存等设备状态不能代替在场、会议或专注证据。未校准个体专注阈值，未验证主观心流识别、真实 LLM 的主动表达质量、Android 真机或家庭物理设备。浏览器实时读取没有持久投递 ACK；新页面以空基线开始，不保证跨页面恢复历史问候。此前 Linux 真机虚拟环境验收保留在[上一轮报告](real-validation-report.md)，本轮没有重新安装目标机。
