# Linux 自动采集与真实模型对话验收

2026-10-15，交付可选的 Linux 桌面状态采集，并按用户要求使用已配置的真实模型 `sensenova-6.8-flash-lite` 验证对话。功能范围见[自动采集](linux-attention-collection.md)，接口见[协议](protocol.md)与 [OpenAPI](openapi.json)。

## 实际采集链路

`examples/attention_live_test.py` 启动临时 SQLite、随机端口 Uvicorn 和独立 Xvfb，使用实际客户端 Runtime、标准库 HTTP 传输及受限节点凭据提交样本。X11 窗口和 `xprop` 查询是真实的；logind 会话由脚本显式提供命令 fixture，不能视为真实桌面锁屏验证。

同一设备节点连续提交五个新鲜样本：Code 应用、未锁定空闲、锁定、解锁返回、XTerm 应用。序号从 1 到 5 递增，应用映射为 code/terminal；未锁定空闲的在场状态保持 null，明确锁定后恢复产生且仅产生一次 `user_returned`。下一轮心跳保留上一轮采集诊断及其独立时间。窗口带有测试私密标题，检查事件没有上传该标题。

当前运行环境的真实 logind 探测得到 `unknown/probe_failed`，没有将无桌面环境伪装成采集成功。采集保留 meeting/sharing=null、focus/availability=unknown；即使显式打开主动策略并使用 chat 模式，返回边界也被 `attention_unknown` 抑制，采集阶段模型调用为零。浏览器以访问方式关联该设备，没有增加第二个感知节点。

## 真实 LLM 对话

从 `Settings.load()` 仅读取配置的模型设置，替换为临时数据库和随机 API 令牌，并关闭自动记忆和向量召回。模型密钥、地址和生产数据库内容不进入报告。

首次流式对话失败：服务商返回 HTTP 200/SSE，但非终态使用 `finish_reason:""`，原解析器将其拒绝为 `model_invalid_response`。随后一次最小结构诊断确认该格式，同时返回有效 stop、尾部 usage 和 `[DONE]`。修复仅将空字符串视为未结束，继续要求有效终态、用量和结束帧，保留工具调用、非法值、重复终态及终态后文字等拒绝规则。没有自动降级或重发失败命令。

修复后两次真实调用通过：

| 链路 | 结果 | 提供商用量 |
| --- | --- | --- |
| 真实 Chromium → SSE → 提供商 | 首次可见文字 **2.711 秒**，完整回复 **3.130 秒**；正式回执 completed，页面历史仅一组用户/助手消息 | 输入 625 / 输出 12 token |
| HTTP JSON → 同一提供商 | 第二轮携带真实历史，completed，GET 原回执一致 | 输入 695 / 输出 17 token |

第一轮实际回复为“你好，随在这里——很高兴这条连接是通的。”；第二轮用户显式询问缺少可靠会议和专注信号时应采取的态度，回复为“保持坦诚和克制——不确定时宁可不越界，也别编造。”。普通聊天提示尚未自动注入节点注意力，因此这不验证模型自动理解桌面状态或主动表达质量。首字与完整耗时是单次网络测量，不代表延迟承诺；浏览器无页面运行错误。

本轮外部模型请求共 **4 次**：首次失败流式 1 次、结构诊断 1 次、修复后成功对话 2 次。诊断用量为输入 65 / 输出 3 token；首次失败未取得可计量 usage，不能将后续成功请求用量当作本轮总消耗。

## 回归与产物

| 检查 | 实际结果 |
| --- | --- |
| 完整后端 pytest | **721 passed**，312.63 秒；一条既有 Starlette/httpx 弃用警告 |
| 提供商与流式对话专项 | 47 passed；新增 12 项空 finish_reason 与非法终态回归，随后纳入完整回归 |
| 原 WebUI 工作流 | 真实 Chromium 19 组通过 |
| 后台陪伴专项 | 真实 Chromium 8 组通过 |
| 自动采集 UI 专项 | 真实 Chromium 3 组通过，使用隔离静态服务和模拟部署 API，验证默认关闭、严格布尔值、未知任务查询原 ID、只读诊断、秘密清理和桌面/手机布局 |
| 流式 UI 回归 | 真实 Chromium 5 组通过，确定性暂停模型验证 pending 时中文首段可见、唯一提交、断流恢复、停止及伙伴切换 |
| 静态检查 | Ruff 检查与格式检查通过，100 个 Python 文件；JavaScript 语法及 UI 三文件 Prettier 通过 |
| 客户端与接口 | Kotlin SDK assemble/exampleClasses 通过；OpenAPI 59 路径，安装采集选项为 boolean、默认 false |
| 分发产物 | wheel 构建、源码/资源一致性、包含采集模块的 zipapp 离线配置检查，以及脱离源码从 wheel 生成同哈希安装包均通过 |

Linux bundle SHA256：`a7c13e4d3970d3dbe62580cef9fefe0c9726d1ff983847ee1e7c4e10c48e273d`。

## 复现与证据

```sh
uv run pytest -q
uv run python examples/attention_live_test.py
# 下条会读取配置的模型凭据，发出两次真实模型请求：
uv run --with playwright python examples/attention_live_test.py --real-model
clients/android-node/build-sdk.sh assemble exampleClasses
uv run python clients/system-node/build.py
```

浏览器脚本支持 `--browser-executable` 与 `--artifacts`。本轮非秘密证据保存在 `.data/attention-review/`：`live/report.json`、`real-llm/report.json`、`real-llm/provider-diagnostic.json`、`real-llm/attempts.json`、`real-llm/real-dialogue.png`、四组浏览器报告与截图、`wheel-check.json`、`wheel/`、`sdk-build.txt`；完整 pytest 日志为 `.data/attention-pytest-final.txt`。

没有改动生产配置、生产数据库或重启现有运行服务，没有连接家庭设备。本轮临时 API、数据库和 Xvfb 已清理。未重做 SSH 目标机安装、GNOME/KDE 实际锁屏、Wayland 前台采集或 Android 真机验证；SSH 系统服务使用 `virtual-life` 用户，通常无法读取其他桌面用户的 X11。此版不识别会议、共享屏幕或主观心流，也不凭自动采集授予主动发言权限。
