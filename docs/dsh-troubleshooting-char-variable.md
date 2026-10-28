# dsh 插件故障排查：`{{char}}` 变量 & profile reload 问题

> 日期：2026-10-28
> 场景：virtual-life 插件接入 dsh web 时遇到两个报错，本文记录根因与修复。

---

## 问题一：`unknown prompt variable "{{char}}"`

### 报错原文

```
unknown prompt variable "{{char}}" in context "virtual-life:companion"; registered variables: provider, model
```

### 根因

`virtual-life:companion` 是 persona 插件注册的一个 system-prompt context。
它的 `text` 内容来自 runtime 的 personality 数据（`description` / `example_dialogue` 等字段），
这些数据是 **character card 风格**的，包含字面量占位符：

- `{{char}}` —— 角色（character）
- `{{user}}` —— 用户

dsh 的 `dsh-system-prompt` 模块在组装 system prompt 时会用 `interpolate()` 遍历所有 `{{...}}`，
把它当作**模板变量**解析。但该 context 注册时只注册了 `provider`、`model` 两个变量，
没有 `char`，于是抛错。

这不是 dsh 的 bug，而是 persona 插件把 character card 模板原样塞进了 system prompt。

### 修复

在 persona 插件的 context 注册处加 `interpolate: false`。

文件：`dsh/virtual-life-persona/index.js`

```js
ctx.systemPrompt.context({
  id: 'virtual-life:companion',
  order: cfg.order,
  interpolate: false,   // ← 关键：保留 {{char}}/{{user}} 字面量，不做变量解析
  text: () => (cache && cache.text ? cache.text : ''),
})
```

`dsh-system-prompt` 的渲染逻辑明确支持 `context.interpolate === false`：
此时 `{{...}}` 会被当作普通文本保留，不参与变量替换。

### 验证

- `node --check dsh/virtual-life-persona/index.js` 语法通过。
- 修改后 profile 重新加载（`systemctl --user restart dsh-web`，或让运行中的进程重读配置），`journalctl --user -u dsh-web` 中**不再出现** `unknown prompt variable`。
- 本会话 system prompt 已实际出现 `[VIRTUAL_LIFE_COMPANION]` 四块，且 `{{char}}` 保留为字面量——`interpolate:false` 生效。
- 三端口（8038 runtime / 8039 bridge / 3080 dsh web）全部监听正常。

---

## 问题二：`profile reload requires the root include entry`

### 报错原文

```
启用失败: dsh: profile reload requires the root include entry
```

### 根因

`@virtuallife/dsh-virtual-life-proactivity` 在 `package.json` 的 `dependencies` 里，
但**不在** `dsh.profile.bundles` 列表里。

它是独立 bundle（自带 `dsh.bundle.patch`）。当 UI 插件管理器尝试单独启用它并触发 profile reload 时，
根 include 组合不完整（bundle 未在根注册），于是报错。

### 修复（2026-10-28 晚，配置已改）

proactivity 是**随 profile 启动加载**的独立 bundle，必须在 `dsh.profile.bundles` 里注册，
插件管理器才能找到它并让它的两个工具（`virtual_life_proactivity_status` / `_deliver`）在会话中可用。
之前"按需启用、不要加进 bundles"的结论是错的：加进 bundles 后 `node -e` 确认
`dsh.profile.bundles.includes('@virtuallife/dsh-virtual-life-proactivity') === true`，
依赖也解析正常。

```js
// ~/.dsh/profiles/web/package.json
dsh: {
  profile: {
    bundles: [
      // ...,
      "@virtuallife/dsh-virtual-life-mcp",
      "@virtuallife/dsh-virtual-life-persona",
      "@virtuallife/dsh-virtual-life-proactivity",  // ← 加入
    ]
  }
}
```

三个 Virtual Life bundle 全部在 `bundles` 里且依赖已声明为 `link:` 指向仓库 `dsh/` 下对应目录。

> 注：若改用 UI 插件管理器「按需启用」仍报 `profile reload requires the root include entry`，
> 那是 dsh 独立 bundle 与 reload 机制的另一个兼容问题；随 profile 启动加载可完全绕开该路径。
> 本次采用的就是随 profile 启动加载。

---

## 附：systemd 托管（避免进程掉线）

三个服务统一用 wjqawa 用户的 systemd unit 托管，`Restart=always`：

| 服务 | 端口 | unit |
|---|---|---|
| virtual-life 运行时 | 8038 | `virtual-life-runtime.service` |
| MCP bridge | 8039 | `virtual-life-mcp-bridge.service` |
| dsh web | 3080 | `dsh-web.service` |

unit 文件在 `/home/wjqawa/.config/systemd/user/`。

常用命令：

```bash
# 切到 wjqawa 用户
systemctl --user status dsh-web
systemctl --user restart dsh-web
journalctl --user -u dsh-web -f
```

> 注意：dsh 重启后登录 token 会变，从 `journalctl --user -u dsh-web` 里捞最新的 URL。
