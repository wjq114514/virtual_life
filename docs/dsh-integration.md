# DSH × virtual_life 集成现状（2026-10-28）

> 这是**接线层**的总览：哪些 bundle 已装、怎么启动、如何验证、当前已知边界。
> 设计决策与迁移选型见 [dsh-fusion-plan.md](dsh-fusion-plan.md) 和
> [dsh-plugin-migration-analysis.md](dsh-plugin-migration-analysis.md)；
> `{{char}}` 变量与 profile reload 的故障排查见
> [dsh-troubleshooting-char-variable.md](dsh-troubleshooting-char-variable.md)。

## 1. 一句话

Virtual Life 运行时（`127.0.0.1:8038`）通过三个 DSH bundle 接进 DeepSeek Harness：

1. **MCP 桥**（`@virtuallife/dsh-virtual-life-mcp`）——把运行时 OpenAPI 契约暴露成 `mcp__virtual_life__*` 工具；
2. **persona**（`@virtuallife/dsh-virtual-life-persona`）——把陪伴人格/情绪/慢变量/内心活动注入 system prompt；
3. **proactivity**（`@virtuallife/dsh-virtual-life-proactivity`）——轮询运行时"主动参与"输出通道，把准备好的话投成独立 turn。

三个都在 `dsh.profile.bundles` 里注册，依赖声明为 `link:` 指向仓库 `dsh/` 对应目录。

> **加载时机**：persona 与 MCP 桥随当前运行进程已生效（本会话 system prompt 已出现
> `[VIRTUAL_LIFE_COMPANION]` 四块）；proactivity 是最后补进 `bundles` 的，配置已就位，
> 但需下一次 dsh-web 进程重载/重启后才会激活（systemd 托管，`Restart=always`，
> 掉线会自动拉起）。不要用 `pkill` 反复手动重启——改完配置让它在下次自然重载时生效即可。

## 2. 三个 bundle

| Bundle | 仓库目录 | 做什么 | 关键文件 |
| --- | --- | --- | --- |
| MCP 桥 | `dsh/virtual-life-mcp/` | 契约驱动，`@deepseek-ai/dsh-mcp-client` 走 Streamable HTTP 连 `127.0.0.1:8039/mcp` | `cordis.patch.yml` |
| persona | `dsh/virtual-life-persona/` | 定时拉 personality/state/slow-variables/inner-life，注册 `virtual-life:companion` context（`interpolate:false`） | `index.js`、`cordis.patch.yml` |
| proactivity | `dsh/virtual-life-proactivity/` | 轮询 `/proactivity/output`，`ctx.schedule.create` 投 turn；提供 `virtual_life_proactivity_status` / `_deliver` | `index.js`、`cordis.patch.yml` |

## 3. 服务与端口

| 服务 | 端口 | 托管 | 说明 |
| --- | --- | --- | --- |
| virtual-life 运行时 | 8038 | `virtual-life-runtime.service`（systemd user，Restart=always） | `uv run python -m virtual_life`，token `local-dev-token` |
| MCP bridge | 8039 | `virtual-life-mcp-bridge.service` | `uv run python -m virtual_life.mcp_bridge --base-url http://127.0.0.1:8038 --listen 127.0.0.1:8039` |
| dsh web | 3080 | `dsh-web.service` | `dsh web --trusted-host 192.168.1.126:3080` |

unit 文件在 `/home/wjqawa/.config/systemd/user/`。

```bash
systemctl --user status dsh-web          # 只读查看
journalctl --user -u dsh-web -f         # 日志
# 注意：重启 dsh 后登录 token 会变，从日志里捞最新的 URL
```

> **不要**用 `pkill`/裸 `systemctl restart` 反复重启 dsh web——它由 systemd 托管
> （`Restart=always`），进程掉了会自己拉起；频繁手动重启只会制造竞态。

## 4. 工具面

- **MCP**：106 个 operation（含 6 个 proactivity 工具），命名 `mcp__virtual_life__<path工具名>`。
- **proactivity 插件**：`virtual_life_proactivity_status`（companionId → policy/decisions/schedule/deliveredEventCount/cursors）、
  `virtual_life_proactivity_deliver`（companionId/nodeId/accessId/sessionId?/afterSeconds?/maxPerPoll?/dryRun?/skipLease?）。
- **persona**：不提供工具，只提供 system prompt context。

工具名规则（桥层）：路径去 `v1`、段下划线连接、`{param}` 去花括号、动词
`get/post/put/patch/delete → get/create/replace/update/delete`、集合启发式、超 64 字符截断（语义以描述里的 `Upstream:` 为准）。

## 5. 验证清单

| 项 | 方法 | 预期 |
| --- | --- | --- |
| 运行时健康 | `curl -H "Authorization: Bearer local-dev-token" http://127.0.0.1:8038/healthz` | `{"status":"ok",...}` |
| 桥工具表 | `uv run python -m virtual_life.mcp_bridge --list-tools` | 106 个工具，含 `companions_companion_id_proactivity_output_get` |
| MCP 端到端 | 会话里调 `mcp__virtual_life__healthz_get` | 200 `status ok` |
| persona 注入 | 会话 system prompt 出现 `[VIRTUAL_LIFE_COMPANION]` 等四块 | 四块齐全，`{{char}}` 为字面量 |
| proactivity 工具 | 会话里调 `virtual_life_proactivity_status` | 返回 policy/decisions 等 |
| proactivity 投递 | `virtual_life_proactivity_deliver`（dryRun） | 列出待投递消息而不投 |

## 6. 已知边界

- **persona context 是数据，不是指令**：`[VIRTUAL_LIFE_*]` 块只描述"她是什么样"，不含"她该做什么"；`interpolate:false` 保证 `{{char}}`/`{{user}}` 保留字面量。
- **proactivity access 会过期**：`/v1/nodes/access/web` 开的 web access 有 TTL，过期后 `poll` 返回 `node_access_expired`；需重新开一个并更新 `cordis.patch.yml` 的 `accessId`（当前 `proact-access-3`）。
- **投递语义（P2）**：当前 proactivity 插件把运行时生成的 `assistant_message` 塞进 prompt 再投——这会让 DSH 再生成一句。设计上已规划改为"冲动投递"（见 fusion-plan §6.3），P1（`prepare`/`commit` 两段式）落地后一并处理。
- **MCP 工具面与运行时 `tools.py` 割裂（P3）**：DSH 工具面是超集；运行时内置 5 个领域工具 `max_calls_per_turn=1` 保持现状，归一在 P3。
- **persona 刷新失败静默**：runtime 不可达时缓存保留旧快照，首启失败则块为空，不抛错。
