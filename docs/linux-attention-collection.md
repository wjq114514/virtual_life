# Linux 桌面状态自动采集

2026-10-15，Linux 设备客户端增加可选的桌面会话采集。它复用已有设备节点、心跳进程和注意力接口；浏览器仍是设备的访问方式，不新增一个感知节点，不自动开启主动陪伴。

## 开启

WebUI「Linux 分发」中勾选“采集桌面状态”，安装请求会带 `attention_enabled=true`，为本次节点凭据额外授予自身 `attention.observe`。默认不勾选，旧凭据仍只能上报设备状态。重装保留设备身份；从未开启的旧凭据不能靠修改配置自提权，需要重新分发取得授权。

手动运行可参考[桌面配置](../clients/system-node/examples/desktop.json)，加入：

```json
{
  "attention": {
    "enabled": true
  }
}
```

也可用 `attention.session_id` 明确选择一个 logind 会话；这不是上传到服务端的注意力 `session_id`。采集开启时 `interval_seconds` 不超过 30 秒，配置须满足额外探测与请求预算，注意力有效期为 90 秒。`--check-config` 只校验，不读取凭据、采集或联网。

## 采集依据

系统会话来自只读 `loginctl list-sessions/show-session`，每轮探测总预算 2 秒，最多检查 16 个会话，命令输出有界。自动选择必须是唯一活跃的本地 `x11/wayland`、`Class=user` 会话；SSH、终端、登录界面、远程或含混的多个桌面不作为用户桌面。显式选择也必须通过类型与来源检查。

`Active` 表示会话是否在前台，`IdleHint` 和 `LockedHint` 是由会话组件上报的提示；后两者不是本项目独立验证的传感器。旧桌面可能不正确更新提示，因此不会仅凭它们授予主动发言权限。[systemd 官方接口](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml)

- 明确锁定或失活会话可给出不在场建议；活跃、未锁定且明确非空闲时给出在场建议。
- 空闲但未锁定时，在场仍是未知；静止阅读不能被当成人离开，更不能当作“可以打断”。
- 同一会话从明确锁定/失活恢复到上述在场条件时，产生一次 `user_returned`；启动、会话切换、空闲结束或探测失败后的恢复不伪造返回边界。不会自动产生任务完成或休息边界。
- `meeting`、`screen_sharing` 保留 null，`focus`、`availability` 保留 unknown；没有会议识别、专注分类器或主观心流检测。

X11 应用类别只在客户端以桌面用户身份运行、已有本地 `DISPLAY`，并且与所选会话的 UID 和 Display 匹配时读取。使用 `_NET_ACTIVE_WINDOW` 与 `WM_CLASS` 白名单映射到 code/docs/terminal/browser/media，仅上传类别；未知应用仍是 unknown。前台窗口机制来自 [EWMH 规范](https://specifications.freedesktop.org/wm/latest-single/)。不读取窗口标题、URL、键盘内容、剪贴板或截图；不寻找、复制其他用户的 Xauthority。

SSH 安装的系统服务仍以 `virtual-life` 用户运行，通常只能取得 logind 提示，无法读取另一个桌面用户的 X11。Wayland 此版只采会话提示，不使用 Xwayland 冒充整个桌面的前台状态。若要以桌面用户直接运行客户端，须与现有系统服务保持单写入者，不能让两进程覆盖同一节点。

## 上报与显示

开启后仅客户端自身注册额外能力，不给网关目标添加注意力能力。每次本机心跳成功后采集，并向自身 `/v1/nodes/{id}/attention` 提交新的命令和递增序号；失败的旧样本不进入重播队列。真实采集失败会提交 unknown，避免保留旧的“可用”建议。新的桌面来源用新的随机注意力会话 ID，不上传系统 UID、用户名或原始窗口类。

下次完整心跳在 `context.attention_collection` 附上上一轮诊断：`status/source/sampled_at/session_type/reason?` 和可空的 `idle_hint/active/locked_hint`。它明确保留自己的采样时间，不应当作心跳时刻的新探测。WebUI 设备清单和后台面板只读展示来源与未知原因，原始命令输出不会展示或保存。

节点凭据只允许已授权能力的自身注册、心跳、观察及注意力写入；跨节点、读取、管理与输出租约仍不允许。撤销在写事务中复核；新旧安装凭据各保留固定授权，使安装失败后旧配置和凭据仍能恢复。没有主动权限的信号会被现有[自触发规则](proactivity.md)保守抑制，用户主动对话仍可正常发起。

## 验证

专项测试覆盖无桌面、歧义、缺字段、权限/命令失败、探测预算、隐私字段、空闲阅读、锁定返回、会话切换、默认关闭、完整心跳和新鲜上报。真实 X11 隔离联调与实际模型测试的环境、结果及限制见[本轮验证记录](linux-attention-validation.md)；无桌面容器中的 unknown 结果不能代替真实 GNOME/KDE 锁屏或 Wayland 验证。
