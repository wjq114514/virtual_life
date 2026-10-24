# 记忆与浏览器验证记录

最新验证（2026-10-13）：后端 168 项回归通过，图谱优化后的模型联测与本地探针 23/23 项通过。2026-10-12 的浏览器与首轮记录保留如下。后端与浏览器检查不使用日常数据库，不读取真实模型密钥；下述真实联测通过 Settings.load 在进程内使用已配置的嵌入提供商凭据。

## 执行结果

| 检查 | 实际结果 |
| --- | --- |
| `uv run pytest -q` | 最新 168 passed，44.06 秒；首轮 150 passed，38.71 秒；均有 1 条既有 Starlette/httpx 弃用警告 |
| `uv run ruff check backend examples` | 通过 |
| `uv run ruff format backend examples` | 完成；最终新增脚本格式检查通过 |
| `node --check backend/virtual_life/webui/app.js` | 通过 |
| `clients/android-node/build-sdk.sh assemble exampleClasses` | BUILD SUCCESSFUL，SDK 与示例编译通过 |
| `uv run python examples/export_openapi.py` | 导出 40 个路径 |
| `uv run --with playwright python examples/webui_smoke.py` | Chromium 实际运行通过；未发生页面异常或 HTTP 5xx |

后端测试覆盖记忆保留与衰减、强化、修正、来源版本、日志过滤与隔离、额度及失败场景。确定性断言不代表真实模型能稳定提取或回答同样的信息。

## 浏览器实际检查

脚本使用临时 SQLite、随机本地端口、确定性假模型，关闭自动整理与向量模型。实际执行了：

- 连接、新建两个伙伴、选择伙伴、明确导入内置中立人格。
- 接入租约、Ctrl+Enter 和按钮发送、真实 HTTP 回复、下一轮历史 user/assistant 角色、四项自定义预算、每轮 source_event_id/preview/已知 usage。
- HTML 字符串以纯文本显示，未创建 script 或 img 元素。
- 在服务端提交后故意损坏一轮响应，确认发送锁定；查询原命令回执恢复一次，重复查询不增加气泡，模型调用数保持 3，没有重复 POST 对话。
- 切换伙伴、断开再连接后保留该伙伴的历史和草稿。
- 读取持久日志，超过 100 条后使用后续游标分页，序号不重复；精确 operation 筛选和导出 JSON。
- 清空页面历史和草稿；390px 宽移动视口没有页面横向溢出。

产物位于 `.data/memory-tests/browser/`：`chat.png`、`logs.png`、`mobile.png`、`memory-logs.json`、`report.json`。这些运行产物按仓库规则不提交。可使用 `--artifacts` 更换输出目录。

浏览器检查验证 UI 和传输行为，不评估真实模型质量、Android 设备渲染或人工视觉观感。日志测试的 105 条分页样本由脚本写入隔离数据库，便于稳定触发分页；真实对话日志也由服务端正常生成。自动整理关闭，不能据此宣称浏览器执行了自动长期记忆提取。


## 真实 Luna 与 BGE 联测

2026-10-12 已完成独立联测：6 次全新 GPT-6 Luna medium 子代理生成（2 次自动提取、4 次 history=[] 的真实人格提示回答），以及真实 SiliconFlow BAAI/bge-m3 的 2 次索引和 1 次查询。19 项断言中 18 项通过；当前薄荷茶图谱边缺失，已如实记录，不能算全部通过。生日区分、更正后偏好、未知昵称/狗名、语义检索缓存、图谱别名和确定性衰减通过。子代理 token usage 无法取得；账本零值只是适配占位，不代表免费。对话完成事件为明确标记的合成夹具，不等于真实 DialogueService 端到端成功。详见 [真实联测报告](memory-live-test-report.md)。


## 图谱优化后的真实复测（2026-10-13）

全新隔离运行 `.data/memory-tests/20261012T230510Z-90dd704c/` 已完成23/23断言（exit0），6次全新Luna medium与3次真实BGE请求共178 input tokens。`auto-memory-v2` 真实纠正提取生成“用户 → prefers_drink → 无咖啡因薄荷茶”，旧乌龙茶边消失，新边支持版本存在且active；副本删除来源后边失效，主数据库保留原始证据。初始妹妹关系边因局部引用缺少第一人称而被保守过滤（unsupported_user_subject），所以不宣称完整提取全部关系。英文日期边界另由确定性回归检查，本轮live为中文案例。子代理usage未知和合成对话事件范围与上轮一致。详见 [真实联测报告](memory-live-test-report.md)。

本轮最终独立后端回归为 **168 passed**（44.06秒；1条既有Starlette/httpx警告），自动记忆定向 **35 passed**，Ruff check/format check均通过。相对原150基线新增18项回归，覆盖关系证据、否定旧对象、主体一致性，以及英文缩写/点分日期、公历农历日期的主体归属。该回归结果与上面23/23真实联测的范围分别记录。
