# 客户端节点程序分发调研

调研日期：2026-10-15。本文保留实现前的源码核查与方案，建议先交付 Linux／NAS 轻客户端安装包，再交付桌面宿主和 Android APK；管理入口继续复用 WebUI。下文“当前”“尚未”描述调研时的状态。随后已实现 Linux 源码/`.pyz` 打包、systemd 安装器、WebUI SSH 分发及可撤销的自身上报凭据，实际使用见 [Linux 安装与分发](linux-deployment.md)。通用设备配对、桌面宿主和自动更新仍属后续工作。

建议的第一份交付物是 **Linux 轻客户端源码归档 + `.pyz` + systemd 模板 + 示例配置**。已有 Python 3.12+ 的设备可直接使用，不需要安装主服务器。面向普通用户的桌面宿主优先验证 **Kotlin/JVM + Compose Desktop + jpackage**，复用现有节点协议库；Android 使用同一协议库接原生宿主，独立生成签名 APK。

## 现有基础与缺口

| 部分 | 源码现状 | 分发前还缺什么 |
| --- | --- | --- |
| [通用轻客户端](../clients/system-node/README.md) | Python 3.12+ 标准库；Linux 指标、TCP/HTTP 网关探测；配置和 token 环境变量；退出码和退避 | 独立发布包、初始化向导、稳定版本号、安装/卸载/升级脚本；尚无独立 pyproject |
| [systemd 模板](../clients/system-node/systemd/virtual-life-system-node.service) | 有固定目录、非 root 用户和退出码策略 | 安装过程创建用户/目录、检查 Python 路径、配置权限和升级目录；当前没有实际安装器 |
| [Android 节点 SDK](../clients/android-node/README.md) | Kotlin/JVM JAR、HTTP/WS、租约 gate、迁移、cursor、流式聊天；可发布到 Maven Local | Android application 模块、界面、权限与生命周期、签名 APK；JAR 不能直接安装到手机 |
| 桌面宿主 | 没有应用工程；SDK 本身是 JVM 库，可以作为复用起点 | 桌面入口、托盘/窗口、平台信号、开机启动、安装器和平台验证 |
| [WebUI](webui.md) | 同源页面，浏览器作为设备的访问方式 | 可增加客户端下载入口；页面或 PWA 并不因此获得原生采集/常驻能力 |
| [主项目 wheel](../pyproject.toml) | wheel 只打包 `backend/virtual_life`，包含 API 和静态页面 | 不是节点客户端安装包，不应让每台设备安装完整主服务器 |
| 接入与版本 | `/healthz`、`/v1/capabilities` 有 protocol_version；全部 `/v1` 共用管理 Bearer token | 每设备凭据、撤销、配对、细分能力兼容检查；尚无发布流水线和下载目录 |

两个需要在桌面化前处理的具体细节：

- [AndroidNode](../clients/android-node/src/main/kotlin/dev/virtuallife/node/AndroidNode.kt) 自动注册没有传入 kind/device，沿用 [NodeRegistration](../clients/android-node/src/main/kotlin/dev/virtuallife/node/Protocol.kt) 的 `kind=ANDROID` 默认值。复用到 Windows／Linux 前，应让宿主显式传入设备种类和持久身份，不能只换安装包名字。
- [FileCursorStore](../clients/android-node/src/main/kotlin/dev/virtuallife/node/ReplayEventRouter.kt) 文件名只根据 node_id 生成。一个安装连接多个服务器时，需要按服务器分别设置存储目录；否则同名节点可能带着另一台服务器的 cursor 跳过事件。

## 各平台交付形式

| 目标 | 建议第一种形式 | 后续形式 | 判断依据与范围 |
| --- | --- | --- | --- |
| Linux 服务器／NAS | 源码 `.tar.gz` 与 `.pyz`，systemd 可选 | 明确发行版后提供 deb/rpm；没有 Python 的常用架构再提供自包含包 | 最接近现有代码；不引入图形栈；非 systemd NAS 保留前台运行方式 |
| 家庭网关 | 同一轻客户端包，配置 gateway 模式 | 可选容器镜像或 NAS 应用包 | 目标设备仍独立建节点；网关只观察明确配置的端点 |
| Windows 桌面 | Kotlin/JVM 桌面宿主，自带裁剪运行时的 exe/msi | 需要系统管理分发时再评估 MSIX/App Installer | 能复用协议、回放和租约逻辑；仍需实现桌面桥接与指标适配 |
| Android | 原生宿主 + 现有 SDK，私有下载页分发签名 APK | 需求稳定后再选应用商店渠道 | 必须补宿主，不是把 JAR 改成 APK 后缀；安装/升级由系统确认 |
| macOS | 桌面宿主稳定后提供签名、公证的 dmg/pkg | 后续再接自动更新 | 增加 Mac 构建、签名与真机验证，不作为首个交付目标 |
| 只能用浏览器的设备 | 继续访问现有 WebUI | 可研究 PWA 的安装体验 | 保留 Web access；不宣称已安装原生节点或获得后台采集能力 |

Linux 容器仅适合作为可选部署模式。当前采集器读取运行环境的 `/proc` 和文件系统，容器中可能混合宿主与容器视图；应继续标注 `scope=runtime_environment`，不能把它展示成已验证的完整宿主指标。路由器资源有限或缺 Python 时，可先在 NAS／小主机运行网关观察，不要求路由器安装整个 Python 运行时。现状见[设备状态合同](device-state-tracking.md)。

## 技术选择

### 轻客户端：先 zipapp，按需冻结运行时

当前客户端不依赖第三方模块，适合标准库 zipapp：发布 `.pyz`，用户仍使用 Python 3.12+ 启动。它减少要复制的源码文件，不能消除 Python 运行时要求。保留源码归档作为可审阅、可诊断的替代交付。[Python zipapp 官方说明](https://docs.python.org/3/library/zipapp.html)

打包要设置归档根目录的 `__main__.py`，执行 `raise SystemExit(system_node.cli.main())`，保留 2/3/4 等退出码；不能因为包装入口丢掉返回码，破坏 systemd 对配置或鉴权错误的重启策略。只加入运行模块、入口和必要许可证，排除 token、个人配置、缓存、构建输出；配置与状态放在包外。建议新增独立版本来源，解决当前客户端 User-Agent 固定 `1.0`、项目与 SDK 为 `0.1.0` 的版本含义混淆。

目标没有 Python 时，再用 PyInstaller 提供 **onedir** 归档。其可包含 Python 运行时，但需要按目标系统/架构构建；Linux 不捆绑 glibc，应在声明支持的较老兼容环境构建并验证。首版采用 onedir 便于服务部署和替换，避免 onefile 每次启动解包及临时目录执行条件。Alpine/musl 与 glibc 目标分开验证，不承诺一个 Linux 二进制通用。[PyInstaller 工作方式](https://pyinstaller.org/en/stable/operating-mode.html)、[Linux 兼容限制](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible)

### 桌面：协议复用优先，WebUI 继续负责管理

| 方案 | 对当前仓库的收益 | 代价 | 建议 |
| --- | --- | --- | --- |
| Kotlin/JVM + Compose Desktop | 直接依赖现有 JVM SDK，复用 HTTP/WS、事件回放、迁移与 LeaseGate | 新增桌面界面与桥接；附带 Java 运行时；更新机制需另选 | 优先做小型宿主原型，验证托盘、单实例和租约停播 |
| Tauri 2 | 可复用前端布局与交互思路；有桌面安装和签名更新方案 | 新增 Rust/JS 工程；现有 JVM SDK不能直接在 WebView 中调用，需要重实现协议或额外进程 | 若完整复用 WebUI 成为首要目标再选择，不与 Kotlin 宿主同时开工 |
| Python 冻结包 | 最容易复用现有遥测程序 | 当前非 Linux 指标不完整，也没有角色输出宿主/托盘生命周期 | 可交付遥测工具，不把它当成已经完成的桌面陪伴客户端 |

Compose 的原生分发插件通过 jpackage/jlink 打包所需 Java 运行时，用户无需另装 JDK，可生成 Windows exe/msi、Linux deb/rpm、macOS dmg/pkg。安装包需在对应操作系统构建，因此应有 Windows、Linux、Mac 各自的构建与验证任务；现有 Linux 工作站不能直接证明 Windows 安装成功。[Compose 原生分发](https://kotlinlang.org/docs/multiplatform/compose-native-distribution.html)、[JDK 21 jpackage](https://docs.oracle.com/en/java/javase/21/docs/specs/man/jpackage.html)

建议先将当前 SDK 中与平台无关的部分整理为可共享节点核心，保留旧包与 Maven 坐标兼容入口；桌面和 Android 各实现宿主桥接。桌面首版只做连接设置、状态、显式开始/停止呈现、托盘和打开服务器 WebUI，不复制人格/记忆/日志五套管理页。需要完整桌面聊天体验时，再决定是否嵌入或重用 WebUI。核心抽取属于后续实现，不能假设今天的 AndroidNode 已是可直接交付的桌面客户端。

Windows 角色显示和与用户有关的采集应运行在登录用户会话。Windows Service 不能直接与用户桌面交互，不能只把角色程序装成系统服务就期待悬浮窗出现；若未来需要独立遥测服务，使用受限 IPC 汇入同一设备宿主，不新增第二个节点写入者。[Microsoft Interactive Services](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)

若采用 Tauri，其 updater 支持桌面平台并要求更新签名；官方插件目前不支持 Android/iOS，不能拿它统一 APK 更新。远程管理页面也不应因此得到任意本地文件或命令权限，应按原生桥接能力单独授权。[Tauri updater](https://v2.tauri.app/plugin/updater/)、[官方平台支持表](https://raw.githubusercontent.com/tauri-apps/plugins-workspace/v2/plugins/updater/README.md)、[Tauri 权限机制](https://v2.tauri.app/security/permissions/)

### Android：先可前台使用，再验证后台宿主

建议新增 Android application 工程，依赖现有 SDK。首版完成服务器设置、设备绑定、当前状态、用户主动启停及前台文本呈现，保证未授权时不声明相关能力。悬浮窗、通知、屏幕感知分别按需申请，不在安装完成时默认为全部可用。

私有分发使用签名 APK；用户按系统要求允许该安装来源。签名密钥与 applicationId 应稳定保存，升级通常要求同一签名或平台支持的合法签名轮换链，并增加 versionCode。商店渠道另行准备 AAB，不能把 AAB 当普通可直接安装的单文件 APK。[Android 站外分发](https://developer.android.com/distribute/marketing-tools/alternative-distribution)、[应用签名](https://developer.android.com/studio/publish/app-signing)、[版本管理](https://developer.android.com/studio/publish/versioning)

后台持续连接是宿主能力验证项，而非打包选项：Android 14 起前台服务须声明合适类型及对应权限；以 Android 15+ 为目标时，后台 dataSync/mediaProcessing 有每类 24 小时内累计 6 小时等限制，不能把 dataSync 当作无限常驻心跳方案。WorkManager 的周期工作也不适合提供当前约 20 秒的在线心跳和短租约。系统停止宿主时应停止输出、撤去能力并让节点到期离线；后续按真实用途选前台服务类型、通知/重连方式并做真机验证。[前台服务类型](https://developer.android.com/develop/background-work/services/fgs/service-types)、[前台服务超时](https://developer.android.com/develop/background-work/services/fgs/timeout)、[周期工作时序](https://developer.android.com/develop/background-work/background-tasks/persistent/getting-started/define-work)

分发规则也要按实际地区核对：截至调研日，Android 官方页面说明 2026-09-30 起在部分地区、认证设备的参与商店开始启用开发者验证保护，并计划 2027 年继续扩展；也介绍最多 20 台设备的 limited distribution 账户。不能把旧的“所有站外 APK 永久无额外要求”当作发布前提，也不能推断所有地区的私人 APK 安装已统一受限。个人内测和公开渠道分别按目标设备的当前流程验收。[Android 开发者验证当前说明](https://developer.android.com/developer-verification)

## 从下载到接入

统一入口建议放在 WebUI 的设备清单附近：用户选择 Linux／Windows／Android／macOS 和运行模式，看到版本、架构、运行时要求、下载链接与安装说明。用户可手动修正平台选择；下载动作本身不注册节点、不申请输出租约。

首版可使用静态发布目录或对象存储，WebUI 展示下载目录；没有必要让长期模型任务进程承担所有安装包传输。下载目录按不可变版本存放，例如：

```text
releases/<version>/
  virtual-life-system-node-<version>-source.tar.gz
  virtual-life-system-node-<version>-py3.pyz
  virtual-life-system-node-<version>-linux-x86_64-glibc.tar.gz  # 按需构建
  virtual-life-desktop-<version>-windows-x86_64.msi            # 桌面工程完成后
  virtual-life-node-<version>-android.apk                     # Android 宿主完成后
  checksums.txt
  release-notes.md
```

这只是命名草案，目前没有对应的已发布下载地址。架构/发行版/运行时基线以实际构建测试为准，不按文件名推断兼容。

初次启动按以下顺序进行：

1. 输入服务器地址，验证 TLS 和实际身份；本机开发 HTTP 与可对外部署的 HTTPS 明确区分。
2. 读取健康与 capabilities，确认节点协议及本次要启用的功能；app 版本相同不代表能力一定相同。
3. 关联已有设备或建立稳定设备身份，确认这台设备唯一的原生写入者。
4. 完成凭据设置/配对，保存当前服务器绑定；只领取已授权功能需要的权限。
5. 注册实际能力，开始心跳；用户明确选择呈现时才准备资源、确认 ready 和申请/续输出租约。

### 凭据分发分两步

当前 [api.py](../backend/virtual_life/api.py) 的全部 `/v1` 路由和节点 WS 使用同一管理 token，没有设备级 scope。自己的受控设备可以沿用本地配置和 token 环境变量做第一轮安装验证，但安装包、配置示例、下载链接和二维码均不包含实际管理 token。源码发布包无需等待全套配对实现；面向更多设备的便捷接入则需要补设备凭据。

建议下一阶段采用“客户端发起 → 已登录 WebUI 确认 → 发放受限设备凭据”的配对方式。无屏网关可显示短码，由另一台设备确认；参考 OAuth 2.0 Device Authorization Grant 的用户码/设备码分离、有效期、轮询节流和批准步骤。可以借鉴交互，并不声称现有服务已实现 OAuth。[RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)

配对确认页显示目标设备、申请角色和绑定服务器。二维码只携带服务器地址与短期配对用户码；秘密 device_code 保留在发起客户端。授权后由同一受 TLS 保护的会话领取设备凭据，凭据可单独撤销/轮换，并绑定允许的节点与操作：

- reporter 只能上报自身；网关凭据只可上报显式授权的目标集合，不能借此覆盖其他节点。
- 呈现宿主增加自己需要的事件、迁移、租约及对话权限；访问其他伙伴/内容的范围由服务端校验。
- 记忆删除、人格管理、配对批准等管理操作继续留给管理身份，不因为“装了节点”自动开放。

这需要修改服务器授权层、设备凭据存储、WS 握手和测试，不能仅在客户端换一个 token 字段就完成。现有单所有者运行时也不会因此自动变成多用户系统。

### 身份和单实例必须跨升级保留

遵循[设备身份合同](device-identity.md)：安装包不是设备；卸载重装、换浏览器或换访问方式也不应无依据增加节点。node_id、device_id、服务器绑定、cursor 与凭据存放于包外专用目录；更新不重新生成身份。重新安装后若身份已丢失，应提示关联已有设备，不只依赖 IP/MAC 静默创建或合并。镜像/批量复制安装包不能包含已生成的设备 ID 或凭据。

同一设备的桌面界面、遥测和事件连接由一个宿主管理完整能力和心跳，使用进程锁避免两个版本同时启动。已运行轻客户端时，新增桌面宿主要先接管其身份并停止旧写入者，或通过本地 IPC 汇聚遥测；不能另开同 node_id 的第二个心跳循环。WebUI 继续使用独立 access，网关目标继续保留目标与观察者身份。

cursor 按服务器与 node_id 隔离；切换服务器保留各自记录。服务器绑定初期可采用规范 origin 对应独立配置目录，未来有稳定 server_id 后支持地址变化和备份恢复识别。任何本地“清空身份”都不能被解释为已经撤销服务器凭据或删除历史节点。

## 发布与升级

第一版先采用用户确认的整包升级，不新增远程执行命令入口。构建机固定工具链与依赖，将版本、目标平台、架构、运行时要求、协议/功能兼容、文件长度、SHA256 和发行说明写入发布目录。SHA256 用于校验完整性；从同一站点取到一个 hash 并不能独立证明发布者身份。

Windows 安装包的系统签名、macOS 的 Developer ID/公证和 APK 签名与更新元数据各有用途，不能相互替代。MSIX 需要有效且在目标设备受信任的签名；App Installer 可支持站外更新，但应在确定 Windows 分发路线后接入。Mac 对外发布要实际验证签名、公证与 Gatekeeper 安装体验。[MSIX 签名](https://learn.microsoft.com/en-us/windows/msix/package/signing-package-overview)、[App Installer 更新](https://learn.microsoft.com/en-us/windows/msix/app-installer/auto-update-and-repair--overview)、[Apple Developer ID](https://developer.apple.com/developer-id/)

需要自动更新时，桌面根据最终框架选择成熟的更新器；独立轻客户端的发布仓库可采用 TUF 来验证目标文件与带版本/有效期的元数据，并管理根信任和密钥轮换。不要只给自写 latest.json 加一个可替换的公钥，就认为解决了旧版本回放、元数据过期与密钥轮换。TUF 负责更新发现与下载验证，安装/停止/切换/恢复仍需本项目实现。[TUF 官方概述](https://theupdateframework.io/docs/overview/)

轻客户端建议采用 `versions/<version>/` 和独立 config/state 目录，下载到暂存区并验证后，再停止旧进程、切换 current、启动并检查新版本。失败时保留旧包与兼容配置；默认不在正在迁移、输出或执行未决对话时强制更新。必须停更时先停播并尽力完成释放，重启后重读当前 owner/epoch/lease，而非从历史 cursor 直接恢复输出。旧版和新版不能短暂同时持有原生写入职责。

Android 通过系统安装流程更新，不承诺普通应用可静默替换自己。恢复旧实现通常应发布带更高 versionCode 的修复包，而不是要求用户覆盖安装低 versionCode APK；卸载再装会影响本地身份/凭据，不能作为默认回滚方案。每次本地配置迁移保留版本和可恢复备份，二进制能回退不代表状态格式一定可回退。

## 建议实施顺序与验收

| 阶段 | 可评审的交付 | 验收边界 |
| --- | --- | --- |
| 1：受控设备内测 | 独立版本、源码/pyz 打包脚本、离线配置校验、systemd 安装/卸载模板、发布目录说明 | 没有 Python 时给出明确要求；干净目录可运行；不含个人 token；升级不改 node_id；退出码保留；不触发额外服务器实例 |
| 2：简化多设备接入 | 配对与设备凭据、撤销、下载目录与 WebUI 入口 | 一次性/过期/并发领取；未批准不能注册；网关目标授权；凭据不能管理人格/记忆；HTTP 和 WS 同样约束 |
| 3：可安装宿主 | Windows 桌面原型与 Android 前台宿主；选定主力平台先做安装包 | 设备类型正确；可复用旧节点；单写入者；实际权限投影；断网/锁屏/退出立即停播；迁移与流式回执功能不退化 |
| 4：可靠升级与更多平台 | 签名发布、适合所选框架的更新器、Mac/其他架构 | 首装/升级/失败恢复；签名或哈希不符拒绝；原配置/身份/cursor 保留；服务器兼容性检查；Android 签名与 versionCode 连续 |

优先落地阶段 1，随后配对与 Windows/Android 宿主可按需求安排；不先建全平台商店与自定义自动更新服务。阶段 1 建议新增 `clients/system-node/packaging/` 与发布示例；宿主阶段分别新增 `clients/desktop-node/`、`clients/android-app/`，共享核心从现有 SDK 演进。上述目录均是建议，当前尚未创建。

验证应使用临时服务器和测试凭据，包含干净安装、重复安装不重复建节点、版本替换不双写、网络中断、凭据撤销、升级期间租约失效、换服务器的 cursor 隔离，以及不同架构/系统的实际安装。现有后端和浏览器测试不能替代 Windows 安装、Android 真机后台生命周期或 macOS 公证验证。

调研轮次只完成源码核查、官方资料调研和方案文档。后续 Linux 实现与验证单独记录在 [安装文档](linux-deployment.md) 和 [实施记录](implementation-plan.md)；尚未发布外部下载版本或连接实际设备安装。
