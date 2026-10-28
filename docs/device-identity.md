# 设备节点与访问方式

节点表示设备，WebUI 表示访问这台设备的一种方式。2026-10-15 起，新页面不再注册 `web-随机ID` 节点；用户点击接入时解析设备，并建立有独立期限的 Web access。只连接、查看资料或选择伙伴仍不会创建节点。

## 如何认出设备

原生宿主继续使用持久的 node_id，可在注册时附带设备身份：

```json
{
  "node_id": "my-desktop",
  "label": "书房电脑",
  "kind": "desktop",
  "capabilities": ["avatar.render", "text.display"],
  "device": {"device_id": "host-installation-id", "mac_addresses": ["02:11:22:33:44:55"]}
}
```

设备 ID 应由宿主持久保存，多进程共享同一个设备身份；不要每次启动生成。MAC 接受六组冒号、六组连字符或十二位十六进制，统一为小写冒号格式并去重；拒绝全零、广播和多播地址。最多 16 个地址。MAC 是宿主提供的线索，可能随网卡或地址随机化改变，服务端不会扫描网络或推测远端网卡。

强身份已关联另一 node_id 时返回 `409 device_already_registered`，消息指出应复用的节点；多个身份分别指向不同节点时返回 `device_identity_conflict`。拒绝不会创建重复设备。已有原生 SDK 的 node_id 与本地输出 gate 绑定，因此不在注册时偷偷替换它；应使用已有设备的 node_id 配置宿主。

Web 接入依次采用：

1. 用户在“本机设备”中明确选择的已有设备。
2. 已持久保存的 browser_id 与设备关联。
3. 服务端近期直接观测到的唯一局域网 IP 对应设备。
4. 没有依据时创建新的设备节点，并持久关联 browser_id。

browser_id 存于同源 localStorage，失败时使用 Cookie；两者都不可用时只允许浏览，不无声生成临时设备。此标识是浏览器安装退路，换网络或刷新页面仍可复用；它不是硬件指纹。localStorage 以 origin 隔离，隐私窗口的存储也可能随会话结束清除，参见 [MDN 存储说明](https://developer.mozilla.org/en-US/docs/Web/API/Window/localStorage)。

IP 自动判断只限直连 RFC1918 IPv4 或 IPv6 ULA，且只使用一个节点 TTL 内的观测；公网、回环、链路本地地址不作为自动合并依据。IPv4-mapped IPv6 会归一化。多个节点匹配时返回 `device_ambiguous`，要求明确选择，不靠猜测创建或合并设备。原生设备心跳会刷新可用的地址线索；网关替其他目标注册时不把所有目标都认作网关自身。

包含 Forwarded 或 X-Forwarded-For 的请求不参与 IP 推断，服务端不信任请求体指定的 IP，也不自行解释转发头。公网/NAT/反向代理、不同浏览器首次访问或地址有歧义时，可在下拉框明确关联已有本机设备。不要把下拉框当成远程操作目标选择器。

浏览器的标准 Network Information API 提供网络连接类型等信息，不能据此读取网卡 MAC，参见 [MDN 接口说明](https://developer.mozilla.org/en-US/docs/Web/API/Network_Information_API)。跨浏览器的可靠设备关联需要宿主稳定身份或明确绑定。

## Web 会话与设备能力

Web 每次接入生成 access_id，采用服务器返回的 node.node_id 作为设备节点。多页面共享同一设备时只允许一个活跃 Web access；第二页返回 `device_access_busy`，可以继续查看资料。原页停止、隐藏、断开、切换伙伴或离开时立即停止本地输出，并尽力释放自己的 access。释放失败由 TTL 回收活跃资格，设备记录保留。

Web access 使用节点 TTL，心跳仅刷新自身 context 和有效期。过期或已释放的 access 不能心跳复活，重新接入必须使用新 ID；旧页的延迟释放不能撤销新页会话。同一活跃 ID 的合法重试刷新期限，不新增设备、会话或重复打开事件。历史详细会话每台设备最多保留 64 项，已用 access_id 的精简墓碑保留以拒绝旧 ID 重用；设备和运行事件不自动删除。

原生注册能力与原生 heartbeat context 不被 Web 覆盖。活跃 Web 临时贡献 `text.display`，其过期后不残留；原生宿主在线时仍提供自己的能力与状态。原生宿主已离线、仅浏览器在线时，不再宣称旧的音频或采集能力可用。NodeSnapshot 的 device 显示设备 ID、MAC 和观测 IP；IP 显示是线索，不等于硬件身份。accesses 仅列出当前活跃 Web 访问。

原生协议的注册/心跳仍整体替换自身能力/context；同设备的多个原生进程应由统一宿主聚合为一个写入者，或各自维护独立 `session_id` 让服务端按 `device_state` 的会话/序号仲裁旧写入者（见[设备状态跟踪](device-state-tracking.md)）。此轮已分离 Web 访问并增加 device_state 会话仲裁，任意原生进程的完整合并聚合仍属于宿主职责。

## API 与输出校验

全部接口使用现有 Bearer 鉴权。

| 路由 | 请求与结果 |
| --- | --- |
| `POST /v1/nodes` | 设备注册，可附 device；新的 kind=web 注册返回 web_access_required |
| `POST /v1/nodes/access/web` | `{browser_id, access_id, label?, kind?, bind_node_id?}`，返回 `{node, access_id, expires_at, resolution}` |
| `POST /v1/nodes/{node_id}/access/{access_id}/heartbeat` | `{context}`，返回最新 WebAccessSnapshot |
| `DELETE /v1/nodes/{node_id}/access/{access_id}` | 幂等释放指定访问，返回设备快照，不删除设备 |

设备 kind 为 desktop/android/other；Web 的访问种类在 accesses.kind 表示。resolution 为 bound/browser_id/lan_ip/created。未知、释放、过期或绑定不匹配的 access 心跳返回 `409 node_access_expired`。只读查询不写入状态。

Web 对话增加可选 access_id，服务端在接受、模型派发和输出提交前校验访问仍绑定当前 node_id 且有效。生成中关闭旧页并用新页接入同一设备，也不能让旧回复借同一 node_id/epoch 输出；已执行模型用量仍结算，原命令回执保留。省略 access_id 的旧原生请求保持原协议和命令指纹。

页面的单调输出期限同时受访问 TTL 和设备输出租约限制；完成回执呈现前还核对当前 accesses。设备间迁移仍执行原来的 released/ready、epoch、revision 与租约规则；同设备重开页面只重新取得访问和租约，不虚构一次设备迁移。过期访问不能恢复旧迁移的 target_ready。

SDK 同步提供设备身份/访问类型及 openWebAccess、heartbeatWebAccess、releaseWebAccess。AndroidNode 使用宿主明确配置的稳定设备 node_id；表情、采集和权限仍由宿主负责。

## 历史数据

旧库通过新增表迁移，已有 node_id、伙伴落脚点、迁移、事件和记忆来源引用保持有效。没有可靠设备依据的历史 web-UUID 不批量合并或删除；WebUI 标明其为历史访问记录，新浏览器不再创建这种节点。设备关联仅使用新观测、稳定身份或用户明确选择。

本地规则与假提供商测试验证身份复用和会话隔离；真实跨浏览器、网络和 Android 设备表现仍需联调。
