# 设备节点与 Web 访问实施合同

2026-10-15。每台设备一个 node，WebUI 是访问方式；页面随机 ID 只能是 access_id。保留已有节点/事件/迁移引用，不按地址批量合并历史。

## 模型（主代理负责 models.py/responses.py）

DeviceIdentity: device_id: Identifier|null=None, mac_addresses: list[str]=[]，最多16；MAC统一为小写冒号六组，拒绝全0、广播、多播、无效值；空identity拒绝。
NodeRegister 新增 device: DeviceIdentity|null=None。
WebAccessRequest: browser_id: Identifier, access_id: Identifier, label: DisplayName="本机设备", kind: desktop|android|other="other", bind_node_id: Identifier|null=None。
WebAccessHeartbeat: context: JsonObject={}（沿用 bounded_json）。
NodeDevice: device_id:str|null=None, mac_addresses:list[str]=[], ip_addresses:list[str]=[]。
NodeWebAccess: access_id:str, kind:Literal["web"]="web", last_seen_at:str, expires_at:str。
NodeSnapshot 新增可缺省 device:NodeDevice|null=None, accesses:list[NodeWebAccess]=[]。
WebAccessSnapshot: node:NodeSnapshot, access_id:str, expires_at:str, resolution:bound|browser_id|lan_ip|created。

## 服务（状态执行者负责 devices.py、coordinator.py 与独立测试）

Coordinator.__init__ 内本地 import DeviceService，建立 self.devices，在 self.reconcile 前初始化。
Coordinator._node(row, now, connection) 增加必需 connection 参数，内部所有调用更新。主代理更新 dialogue/proactivity 跨模块调用。
Coordinator.register_node(payload, *, peer_ip=None)：旧非web稳定注册保留行为，在同一事务调用 devices.record_registration(connection,data,now,peer_ip)。检查必须先于任何注册写入；可拆 devices.check_registration 然后 record_registration。强identity已属于别的node时409 device_already_registered（消息包含可复用的canonical node_id），不创建重复设备，也不偷偷改旧客户端ID。多个强keys指向不同node时409 device_identity_conflict；已有node重报的新keys只能添加、不替换他人身份。新kind=web注册拒绝409 web_access_required；旧web节点允许原ID重报，作为历史兼容。

DeviceService新表在自身初始化创建，无旧表破坏性迁移：身份key、节点已观测IP、browser到设备映射、web accesses。身份与接入必须事务保存，采用coordinator._write(reconcile_first=False)；校验完成再变更。所有IP来自已认证HTTP transport，body不接受IP。有效直连RFC1918/IPv6 ULA才允许辅助自动匹配，排除公网/回环/链路本地/多播/未指定，normalize IPv4-mapped IPv6。peer_ip=None不推断。主代理在带forwarded/x-forwarded-for的请求上不提供peer_ip（代理场景使用稳定绑定）。不读取ARP、不扫网、不声称服务器能读远端MAC。

record_registration只有desktop/android（或明确device身份的other）记录peer_ip，避免网关替多个目标注册时把所有目标当自己；新WebAccess也记录当前peer。旧原生心跳不覆盖设备身份；IP候选只取最近node_ttl_seconds内观测，避免无限期认DHCP地址。

Web resolve顺序：显式bind_node_id（须存在）→已持久browser_id关联→最近唯一LAN IP候选→新device-{uuid}。多个IP候选409 device_ambiguous，要求绑定，不能默默创建重复节点。显式绑定可更新browser关联。新节点kind是设备kind，不是web，label不带浏览器会话随机串；base capabilities=[]，base context={}。browser_id是访问安装的稳定退路，不声称跨浏览器硬件指纹。

同一device只允许一个未过期web access；第二个不同access_id返回409 device_access_busy，不抢占。相同access_id重复注册只能复用同一browser/节点，不能重绑定到其他设备，释放/过期后不得重用旧access_id（409 node_access_expired，UI下次生成新ID）。TTL沿用node_ttl_seconds，heartbeat不能复活过期/释放access，旧access不能撤销新access。历史access同设备最多保留64条（裁剪仅已关闭/过期记录），不会无限积累页面。

DeviceService方法：
- open_web(payload, *, peer_ip=None) -> WebAccessSnapshot
- heartbeat(node_id, access_id, payload, *, peer_ip=None) -> WebAccessSnapshot
- release(node_id, access_id) -> NodeSnapshot，幂等关闭指定会话；不删除设备，不影响原生客户端。
- validate_access(connection,node_id,access_id,now)：过期/释放/节点不匹配/未知均409 node_access_expired。
- project(connection,row,now,snapshot)：返回扩展NodeSnapshot。原生basecap/context/last_seen不被Web写入替换；当前活跃Web贡献text.display，context不合并原生context（可见信息保留在access自己的表），只在其TTL内有效。online/last_seen取原生和live web的最新；原生过期但web活跃时不得宣称旧audio/reading等能力仍可用。Web退出后不残留text.display（除原生自己具有）。只读投影不写库。

## API/生成复核（主代理负责）

POST /v1/nodes/access/web -> WebAccessSnapshot，201
POST /v1/nodes/{node_id}/access/{access_id}/heartbeat -> WebAccessSnapshot
DELETE /v1/nodes/{node_id}/access/{access_id} -> NodeSnapshot
全部现有Bearer鉴权。native POST /nodes把transport peer传给coordinator。
DialogueRequest增加可缺省access_id，提供时在_snapshots每次验证此access绑定request.node_id且有效，含模型提交前复核。无access_id保留原客户端行为。主代理保持旧请求fingerprint兼容（access_id=None不参与旧fingerprint）。

## WebUI/SDK（客户端执行者负责 WebUI、Protocol.kt、RuntimeApi.kt、Dialogue.kt、SDK README）

WebUI保留只读连接和选择伙伴不注册。browser_id使用localStorage一个固定key，存储不可用用cookie同源退路；均不可用停止接入并提示，不能每次无声生成新设备。不保存Token。每次接入生成新access_id（同一次HTTP重试保持ID），POST新route，必须采用返回node.node_id；session.node初始空，不能再web-uuid。页面隐藏/切伙伴/断开/offline/pagehide同步停止输出，尽力DELETE关闭原access（不要阻塞本地停止、不要删设备），不能误关闭后来新access。

增加“本机设备”下拉：默认自动识别，列出已有节点可绑定（GET /nodes，过滤历史kind=web），说明它是关联实际设备，不是远程控制目标。设备列表展示IP、访问方式，避免把Web访问显示为独立硬件。正常刷新/多tab复用设备；活跃第二页显示占用，不能抢同设备输出。接入流程仍保留迁移、epoch、lease和异步generation/id guards。心跳改为access heartbeat，租约前检查access未过期；send body增加access_id，完成前检查当前node.accesses包含本access。建议session.deadline同时上限access剩余TTL。UI会话ID不写进node名。旧未决命令保留原请求形状，过期回执不自动重播。

SDK增加DeviceIdentity、NodeDevice、NodeWebAccess、WebAccessRequest/Heartbeat/Snapshot、NodeRegistration.device?、NodeSnapshot新默认字段、DialogueRequest.accessId?及RuntimeApi 3方法。保持旧调用构造兼容。注释stable nodeId属于设备；Web access用返回canonical ID，native device重复返回409时复用现有nodeId。不要重构AndroidNode lifecycle/gate（它使用明确稳定nodeId）。

## 测试与整合

状态执行者：独立 test_devices.py 覆盖重开/多浏览器LAN、稳定browser跨IP、MAC归一化/冲突、NAT/代理无法误合并（None/public）、多候选歧义、过期IP、活跃会话排他/TTL不复活/旧release不影响新会话、原生cap/context不覆盖与能力过期、重启/有界历史/回滚、原生旧节点迁移兼容。
主代理：API/对话交叉测试、docs/protocol/device-state/architecture/webui/README、OpenAPI、完整pytest、Ruff、JS、SDK。
