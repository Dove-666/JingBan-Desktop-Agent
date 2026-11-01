# 手机远程连接（电脑端）

AURA 的可选加密设备通信模块支持持续的电脑 Agent、桌宠指令、图文聊天、图片传送和聊天记录同步。Android 伴侣当前为 1.6.0（versionCode 9），安装、配对和构建说明见 [android/README.md](../android/README.md)。「电脑 Agent」直接进入电脑实际工具后端；「手机聊天」可同步电脑已有的 API 配置和人格提示词，直接请求模型，再将完成的对话异步回写电脑，电脑离线仍可聊天。手机也可手动配置独立 API。

升级时，手机使用同签名 1.6.0 APK 覆盖安装，保留已有配对、模型配置和聊天记录。电脑源码更新后需要从系统托盘退出 AURA，再重新启动；只关闭聊天窗口不会替换正在运行的旧进程。新版手机采用鲸鱼娘图标与 Liquid Glass 界面，API 31 起非低内存设备使用原生背景模糊，API 26 起和低内存设备保留半透明回退；独立手机 API、离线聊天和草稿行为保持。

## 开启和配对

入口为「设置 → 远程连接」，也可以右键系统托盘图标，选择「手机远程连接」。新配置默认关闭，不会因为打开设置页就启动连接；主动开启后会保存开关，之后启动 AURA 时恢复，包括 Windows 开机启动和静默启动，无需先打开设置页。关闭开关会停止本次服务、取消未完成的配对和自动重试。

开机时 Wi-Fi、Clash 或网卡可能尚未准备好。网络故障会按 2、5、10、30、60 秒退避自动重试，之后最多每 60 秒重试一次，成功后重置等待时间；仍可点击「立即重试」。缺少组件、设置损坏或无效身份会显示具体类别，不会在后台无限空转。退出桌宠不会清除已保存的连接开关、设备身份或已配对白名单。

通信组件单独放在 `requirements-remote.txt`，不会要求安装额外的组网 App。本项目当前 `.venv` 已安装 `iroh==1.2.0`。在其他环境中，若页面显示「可选通信组件未安装」，在项目目录执行后重启 AURA：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-remote.txt
```

开启并显示「远程连接已开启」或「局域网已就绪」后，点击「生成配对链接」，再点击「复制配对信息」，将完整的 `aura://pair/…` 链接粘贴到手机。无需手输短码；短码本身不含电脑地址。链接仅可使用一次，有效期 5 分钟。取消、过期、配对成功或关闭连接后，链接失效；界面会清除显示，且仅在剪贴板仍是本页复制的临时链接时清除该内容。当前提供复制功能，没有二维码界面。

配对成功后，电脑将加密连接中验证的对端公钥加入本地白名单。后续连接需持有对应私钥，不能只凭设备名称或请求 JSON 中填写的身份连接。最多保存 16 个已配对设备；在列表中选择设备并点击「撤销选中设备」后，它需要重新配对。电脑端的长期设备私钥保存在系统凭据库，Windows 上使用 Windows 凭据管理器，不写入普通设置文件；无法访问凭据库时不启用明文回退。

「复制连接信息」包含公开的设备标识与连接票据，不包含配对码，也不能替代配对。临时配对信息包含授权所需的秘密，只交给自己的设备。

## 网络和当前边界

同一 Wi-Fi 优先使用局域网加密直连。电脑完成本地端点绑定并取得有效物理网卡地址后就开始监听，公共中继不可达不再阻止本地配对或通信。界面显示「局域网已就绪 · 公网中继未连接」时，同网设备仍可尝试连接；它表示电脑服务就绪，不代表手机已经连接。连接票据会过滤 Clash 的 `198.18.0.0/15` 虚拟地址、回环/链路本地地址和 VM/虚拟网卡的私有候选。

电脑服务固定使用 **UDP 43123**，开发客户端和隔离测试仍可使用临时端口。同一电脑地址和身份下，重启服务不再因随机监听端口导致旧局域网票据失效。首次升级这项修复时，旧版本手机保存的地址可能还是随机端口：请重新复制电脑生成的配对链接到手机，更新一次连接地址；已有设备授权不会因此清除。电脑局域网 IP 改变后也可能需要更新地址。

局域网直连要求两台设备之间的 UDP 可达；同一 Wi-Fi 若开启客户端隔离也可能无法互联。Clash 的 DIRECT 规则只绕过代理节点，不能解除路由器隔离或让不可用的公共中继恢复。不要为了此功能关闭 Windows 防火墙；应检查当前解释器的入站许可及局域网 UDP 43123。项目不会自动改动系统防火墙。

两台设备可以处在不同网络，例如手机流量与家中的 Wi-Fi。通信模块配置了公共中继发现，能够尝试直连，并在需要时通过加密中继转发。无须自己部署服务器；这仍可能借助第三方公共基础设施。控制桌宠、更新配置和回写记录时，电脑需开机、联网并运行 AURA；手机已有有效的缓存 API 配置时，可在电脑离线期间直接与模型对话，稍后再回写记录。

公共中继没有本项目提供的可用性保证。本轮已通过本机两个真实加密端点之间的配对、指令、图片收发、聊天回复同步，以及在原连接中撤销授权的测试，SDK 实测路径为 `direct`。手机流量到家庭宽带的实际可达性，以及公共中继实际转发的可用性，尚未验证；本机直连结果不能代表跨网测试通过。当前不提供延迟、运行内存或所有网络均可达的保证。

Android App 提供本地历史缓存，前台静默同步。可直接在手机填写模型 API 配置，无需先配对电脑；电脑临时不提供配置时保留手机已有缓存，手动配置优先。没有可用 API 时保留输入和图片并显示配置入口，新聊天不会静默转入电脑待发队列。旧队列中确认未尝试投递的普通图文消息可由用户明确选择「手机直接发送」；电脑 Agent 请求不能转为手机普通生成。可能已送达的请求先查询原状态，避免再次生成。切到后台停止轮询，并保留设备连接 45 秒供短暂切换时复用；超过后再关闭，不提供后台常驻或系统推送。手机直连生成的完整一轮使用受限的 `chat.import` 回写，电脑不会再次调用 AI；电脑 Agent 使用独立的 `agent.send`，旧 `chat.send` 保留兼容。同一轮不能同时使用两种生成路径。同步接口不接收手机数据库文件；图片上传本身只存入收件箱，须显式随消息提交，模型接口须支持图片输入。

## 接入协议

以下为后续手机端和开发工具使用的第 1 版协议，实际定义位于 `desktop_pet/remote/`。

- 传输：iroh 加密 QUIC，ALPN 为字节串 `aura-remote/1`。
- 每个双向流只承载一个请求和一个响应；发送一帧后须结束发送方向。不要在同一流追加第二个请求。
- 帧结构：4 字节大端无符号长度，后接 UTF-8 JSON 对象；长度为 JSON 的字节数，不含长度头。每帧最多 8 MiB，未配对设备的请求帧最多 16 KiB。
- 当前最多 4 个并发连接、4 个并发请求，单连接最多 4 个双向流；客户端应限制并发，在繁忙或限流时退避。
- 请求必须且仅含 `id`、`method`、`params`。`id` 为规范小写带连字符 UUID，`method` 为方法字符串，`params` 为对象。
- 身份取自已验证的加密连接，不能通过 `params` 自报。除 `pair` 外，下列方法均须已配对。

请求示例：

```json
{
  "id": "91e374fc-5c45-44e0-b4c3-bf03b7c05470",
  "method": "status",
  "params": {}
}
```

成功响应为 `{"id":"…","ok":true,"result":{…}}`；失败响应为 `{"id":"…","ok":false,"error":{"code":"…","message":"…"}}`。客户端应根据 `error.code` 处理失败，中文 `message` 用于展示。

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `status` | `{}` | 返回协议版本、状态、聊天忙碌状态、当前会话 ID、能力列表，以及 Agent 可用性、已启用工具和运行会话 |
| `pair` | `code`；可选 `name`，默认为「手机」 | 使用临时码授权该加密连接的真实设备身份；名称为 1～80 个字符，不得含控制字符 |
| `pet.command` | `action`、`value`、`expires_at` | 执行有限的桌宠操作，见下表 |
| `agent.send` | `conversation_id`、`text`；可选 `image_id` | 向规范 UUID 会话直接提交 1～8000 字符的非空任务文字；显式图片须先上传；进入原生工具循环，不分类、不要求启动截图 |
| `agent.cancel` | `conversation_id` | 停止该规范 UUID 会话正在运行的 Agent；返回 `cancelled` 布尔值，不触发新任务 |
| `chat.send` | `text`；可选 `conversation_id`、`image_id`、`desktop_context` | 旧电脑聊天兼容入口；接受 1～8000 字符非空文字；显式图片与 `desktop_context:true` 互斥；1.5.0 起电脑 Agent 使用独立的 `agent.send` |
| `desktop.snapshot` | `{}` | 显式读取一次符合屏幕感知授权与隐私规则的前台画面，返回标准图片元数据，不调用模型 |
| `chat.config` | `{}` | 仅向已授权设备返回电脑当前有效 API 配置、人格提示词和配置摘要；不登记持久操作，不进入聊天同步 |
| `chat.import` | `conversation_id`、`messages`；可选 `title` | 原子保存手机已生成的一条用户消息和一条完整回复，不调用 AI；详细限制见下文 |
| `sync.snapshot` | 可选 `limit`（默认 100，1～100）、`cursor`（首次为空） | 分页获取一个一致的完整记录快照 |
| `sync.pull` | 可选 `since`（默认 0）、`limit`（默认 100，1～100） | 按递增序列读取变化及删除标记 |
| `operation.get` | `request_id`，为原操作的规范 UUID | 查询当前设备发起的持久操作状态 |
| `image.upload` | `data_base64`；可选 `filename`、`mime_type`、`sha256` | 校验后接收图片，返回内容寻址 ID 和图片信息 |
| `image.list` | 可选 `offset`（默认 0，非负整数）、`limit`（默认 50，1～100） | 返回图片元数据、`has_more`、`next_offset` |
| `image.get` | `id`，为图片 SHA-256 的 64 位小写十六进制值 | 返回图片元数据及 `data_base64` |
| `image.delete` | `id`，同上 | 删除该图片；重复删除已不存在的图片仍返回成功 |

`pet.command.expires_at` 是 Unix epoch 秒，可带小数，必须满足电脑当前时间 `< expires_at <= 当前时间 + 60`；进入应用线程实际执行前会再次检查过期。客户端应在发送前生成有效期，不能在断线恢复后回放过时的桌宠动作。

| `action` | `value` |
| --- | --- |
| `expression` | `neutral`、`happy`、`surprised`、`sleepy`、`angry`、`thinking`、`talking` |
| `scale` | 50～200 的整数百分比 |
| `animation` | 已安装角色动作目录中的动作 ID；当前协议未提供动作目录查询方法 |
| `visibility` | JSON 布尔值；系统托盘不可用时拒绝隐藏，避免桌宠无法找回 |
| `say` | 1～1000 字符的非空气泡文字 |

传入 `chat.send.conversation_id` 可以向指定会话发消息，不切换电脑端当前查看的会话。聊天忙碌或配置不可用时会拒绝；目标会话不可用也可能导致接纳或后续保存失败，应使用同步记录中的有效会话 ID。成功响应中的 `accepted: true` 仅表示已接受请求，不表示 AI 回复已经完成；随后通过 `sync.pull` 获取用户消息的 `request_status`、AI 回复和 `reply_to`。使用这个方法时，由电脑生成回复；手机直接 API 生成的回复应使用 `chat.import` 回写。

## 持续电脑 Agent

手机「电脑 Agent」模式按会话保存在本地，重开该会话仍保持；后续消息使用相同 UUID，直接进入电脑端 ChatController、AgentService 和 nanobot 原生工具循环。“继续”不经过普通聊天分类器、关键词判断、情绪 JSON 或 `agent_request` 分派。Agent 根据当前模型和已启用的工具处理，也可以直接回答，不为进入模式强制操作。

能力列表须包含 `agent.send`；停止入口使用 `agent.cancel`。`status` 返回以下独立 Agent 字段：

| 字段 | 含义 |
| --- | --- |
| `agent_available` | 当前配置及本地运行组件能否接收 Agent 请求 |
| `agent_unavailable_reason` | 不可用时的静态安全原因；不携带模型异常或凭据 |
| `agent_capabilities` | 电脑当前已配置、已允许的 Agent 工具名称 |
| `agent_busy` | 当前是否有 Agent 请求运行 |
| `agent_conversation_id` | 正在运行的会话 UUID；空闲为 `null` |

提交示例：

```json
{"id":"91e374fc-5c45-44e0-b4c3-bf03b7c05470","method":"agent.send","params":{"conversation_id":"b80d4b08-0dc4-43c7-8f6d-e84f15bad50f","text":"继续修改刚才的文件"}}
```

`image_id` 是可选的本次显式附件，须先通过稳定的 `image.upload` 请求确认上传。新图片之外的历史图片不会自动再次作为本轮新附件。成功响应包含 `accepted:true`、`conversation_id`、稳定 `message_id`、`execution_mode:"agent"` 和 `reply_delivery:"sync.pull"`；仅代表接纳，最终结果仍以同步消息为准。

Agent 开始前不调用 `desktop.snapshot`，不依赖屏幕感知开关、前台为 AURA 或电脑空闲时间。工作区文本文件读写与文件名查找、公开网页读取、只读系统信息、应用目录查找、当前候选 ID 应用打开和 `computer_use` 均按电脑既有任务助手权限开放；没有任意 shell、插件自动发现或自发子 Agent。GUI 模型只在真正执行已允许的电脑操作时启动。工具边界与配置见[桌面 Agent 说明](ON_DEMAND_AGENT.md#实际工具范围)。配对或切换模式不会自动更改权限。

`discover_applications` 只读列出真实 Desktop / Start Menu 快捷方式、桌面 EXE 以及已安装应用标签，最多返回 80 项及截断标记。返回稳定的不透明候选 ID、显示名称、可执行文件名和描述，不外发绝对路径；已安装标签仅作线索。`open_application` 只接受本轮最新发现结果中可启动的候选 ID；同一会话中的名称澄清可结合先前实际结果继续查找，再读取当前目录确认目标。只有明确应用仍有歧义时才询问用户，不要求先报游戏平台。只读目录需要任务工具或电脑操作任一权限；实际启动仍需电脑操作权限。

每个会话 UUID 的原生工具调用和结果在宿主内存中隔离保存，供后续轮次继续；清空、删除会话或退出电脑进程时清除运行态记录，存档聊天文本仍保留。手机普通历史导入不能伪装为已执行的工具结果。

停止请求为 `{"method":"agent.cancel","params":{"conversation_id":"…"}}`，实际传输仍须补齐新的规范 UUID `id`。只停止目标会话的运行任务；无匹配任务时 `cancelled:false`。手机不离线排队停止操作，也不自动重发；停止或超时保留已完成的写入、点击等效果，不回滚任务。

同步消息公开 `execution_mode`（`"agent"` 或 `null`）、`request_status`、`agent_error_code`、由已知错误码映射的静态 `agent_error` 及固定公开阶段或工具标签 `agent_activity`。活动仅用于运行中消息。1.8.0 另提供下面的 `agent_trace`，用于折叠过程；原始异常、模型隐藏推理、执行上下文、工具参数、截图字节、密钥和私有路径不会作为这些元数据外发。客户端应白名单显示这些字段，不渲染任意内部执行数据。

### 可折叠过程投影（1.8.0）

只有 `role:"user"` 且 `execution_mode:"agent"` 的请求消息携带 `agent_trace` 对象；其它消息为 `null`，避免回复和请求各显示一份相同过程。运行中、完成、失败、取消、等待接手及意外中断都可以保留过程。对象只从电脑已存档的真实执行事件派生，不从模型输出或任意客户端字段构造：

```json
{
  "agent_trace": {
    "version": 1,
    "state": "running",
    "started_at": "2026-11-01T10:00:00.000000+00:00",
    "duration_ms": 3500,
    "events": [
      {"id":"step-0","kind":"progress","label":"正在规划任务…","status":"completed","elapsed_ms":0},
      {"id":"step-1","kind":"tool","label":"检查磁盘占用","status":"running","elapsed_ms":2000}
    ],
    "omitted_count": 0
  }
}
```

`state` 仅接受 `running/completed/failed/cancelled/needs_user/interrupted`；事件 `kind` 仅接受 `progress/tool/computer/summary`，`status` 仅接受 `running/completed/failed`。`label` 必须属于固定的公开阶段、工具/动作或终止摘要白名单。事件 ID 使用原数组序号生成的 `step-N`，不外发原始工具/消息标识。所有用时均为经过范围检查的非负整数；事件用时不得超过该过程用时，开始时间须为有效的带时区 ISO 日期。

投影最多返回最近 80 项有效事件，`omitted_count` 表示此前未展示的有效事件数量。底层电脑归档仍保留完整过程；这是移动端传输和显示范围，不能据此自动重放任务。过程没有 `context`、任意 `detail`、模型推理、工具参数或文件内容，也不新增模型请求来编造步骤。界面的展开/收起状态保存在本机，任务结束后自动收起，点击仍可查看已有事件。

电脑远程同步内部 schema 7 会刷新已有数据库（包括 v6）的消息 INSERT/UPDATE 投影触发器，保留聊天、执行预留及设备授权。此前 schema 5/6 的截图错误、应用查找和只读检查活动标签继续保留。此内部版本不改变 `aura-remote/1` 传输协议。Android 本地缓存升级到 schema 5 后，在下次连接进行一次完整快照以补齐已有终止消息的过程；快照完成前保留可见的离线记录，不删除历史、草稿或配对。仅关闭电脑聊天窗口不会加载新投影，更新源码后仍须从托盘退出并重新启动。

旧电脑没有 `agent_trace` 时，手机可以继续显示已有的简短活动和最终回复；不会把单条活动补成不存在的步骤历史。手机独立 API 的过程记录来自手机本次实际请求/完成阶段，保存在本机，不把模型隐藏推理作为“过程”保存。

Windows 电脑操作截图优先走原生 Win32，采集图片与鼠标输入坐标按 DPI 对齐。单次观察最多做两轮采集；只有可访问的主显示器允许使用独立 SDK 截图回退，采集前后核对显示器。锁屏/安全桌面或会话不可用直接停止，不走回退。回退只重新读取画面，不重新发送模型请求、点击、输入或整项任务。近黑帧和显示器变化也不会被当作有效观察。

| 同步的 `agent_error_code` | 手机应提示的处理 |
| --- | --- |
| `gui_desktop_locked` | 电脑锁屏或安全桌面；请在电脑解锁或处理系统提示 |
| `gui_desktop_session_unavailable` | 桌面会话断开或不可用；恢复 Windows 桌面会话 |
| `gui_screenshot_display_unavailable` | 显示器不可用或发生切换；检查显示器后再发任务 |
| `gui_screenshot_black_frame` | 近乎全黑的无效画面；恢复可见桌面后再发任务 |

无法进一步区分的采集失败仍使用 `gui_screenshot_failed`。这些状态不包含原始异常，不会触发自动解锁、UAC 确认或动作重放；独立 `desktop.snapshot` 仍使用自己的授权和前台窗口隐私检查。

电脑 Agent 仅在连接时提交。离线或配置未就绪时保留草稿；已开始或结果未知的任务只查询原请求，不自动重放，也不能转换为手机普通 API 生成。「查看电脑桌面」继续独立遵守屏幕感知的本地观察、远程发送、暂停和隐私规则；失败不会阻断其它 Agent 工具。

1.6.0 已确认首轮 117 项 Android 单元测试与 Release Lint，以及应用发现、原生 Agent SDK 和宿主兼容性 98 项定向测试；最终界面、截图专项、同步升级和 APK 验收见 [当前 Android 版本记录](../android/README.md#160-验证范围)。手机流量与公共中继本轮未实测。

1.5.0 历史上通过 117 项 Android 单元测试、34 项界面/启动测试、两项真实 Keystore/QUIC 检查及 8 步 Agent 联调。桌面原生工具连续任务和独立窗口的真实 GUI 输入/保存也在当时验证；四份签名 APK 通过检查。这些记录保留在 `artifacts/direct_agent/verification.json` 和 [1.5.0 历史版本记录](../android/README.md#150-历史验证范围)，不代替 1.6.0 验收。

### 1.4.0 历史电脑协助路径

以下为旧版单轮入口和修复记录，1.5.0 起的持续 Agent 不使用此聊天分类与截图上下文路径。1.4.0 手机每轮明确选择「电脑协助」，能力列表使用 `chat.desktop_context`；截图能力为 `desktop.snapshot`。当时请求重新检查屏幕感知权限，权限不足返回 `desktop_permission_denied`，画面不可读取时返回 `desktop_capture_unavailable`。原协议保留兼容；当前手机应升级 1.6.0 并选择「电脑 Agent」。

2026-11-01 电脑端修复：前台为 AURA 自身窗口时，继续拒绝读取其画面；明确选择的电脑协助请求可以在不附图的情况下进入现有聊天与任务分派。模型会收到静态的“本轮未提供截图”说明，不能假称看到了桌面；只有本轮用户原文中的明确指令才可触发操作，问号或普通讨论不会因这条说明而启动任务。其它隐私阻止、授权撤销、暂停和无法确认前台的情形仍拒绝。独立 `desktop.snapshot` 不作这个降级，仍须取得合法画面。

手机主动请求画面属于明确的用户操作，不再因为电脑长期没有键鼠输入而触发自动观察的空闲暂停。无效活动数据及其它窗口、隐私和黑屏检查继续生效。此修复需要退出并重新打开电脑端 AURA，兼容现有 1.4.0 APK。

该修复通过 211 项回归及隔离的自身窗口任务联调。另使用当前模型接口发送一次公开合成任务文字，在合成的旧“无法操作”回复之后，确认新任务仍正确分派为电脑操作；实际操作由 FakeAgent 接收，没有发送用户画面、历史或执行真实输入。证据见 `artifacts/desktop_capture_fix/verification.json`。本轮没有重启正在运行的桌宠，以保留其内存中的未发草稿；升级后应从托盘退出再重开。

## 手机直接 API 与配置同步

`status.capabilities` 包含 `chat.config` 和 `chat.import` 时，已配对手机可在每次连接后调用 `chat.config`。有效配置返回：

```json
{"version":1,"revision":"64 位 SHA-256 摘要","available":true,"base_url":"https://provider.example/v1","model":"配置中的模型","api_key":"仅在授权加密响应中传递","timeout_seconds":30,"reasoning_effort":"default","system_prompt":"电脑实际使用的 SYSTEM_PROMPT"}
```

`system_prompt` 是电脑聊天实际采用的人格和输出要求，包含情绪策略；不打包会话摘要、长期记忆、屏幕记录或日志。`revision` 由有效配置的规范 JSON 计算，模型、密钥或提示词变化都会改变摘要。无有效模型、缺少密钥或凭据库不可用时，只返回 `version:1`、`available:false`、`revision`，不返回残缺密钥或底层错误。

配置只在已验证身份且已授权的 iroh 连接中返回；不写入操作记录、同步事件或聊天正文。手机须将缓存配置中的密钥加密存储，不得写入日志或普通历史数据库。撤销配对会阻止后续连接和配置刷新，不能远程删除手机已经缓存的 API 配置。直连 API 使用手机网络和该密钥对应的服务额度。

`chat.import` 第 1 版每次仅接收完整的一轮对话：

```json
{
  "conversation_id":"b80d4b08-0dc4-43c7-8f6d-e84f15bad50f",
  "title":"手机上的对话",
  "messages":[
    {"id":"3d6ad64f-a06d-4b41-ae79-45297d6505fb","role":"user","content":"你好","created_at":"2026-10-31T10:00:00Z"},
    {"id":"63c7d33b-1667-4a35-a1c1-edbfc30ae587","role":"assistant","content":"你好！","created_at":"2026-10-31T10:00:01Z","reply_to":"3d6ad64f-a06d-4b41-ae79-45297d6505fb"}
  ]
}
```

会话和消息 ID 均为稳定的规范 UUID，须在手机发出 API 请求前保存。第一条必须为 `user`，正文 1～8000 字符；第二条必须为 `assistant`，正文 1～24000 字符。时间须为带时区的 ISO 8601，电脑规范为 UTC，回复不能早于问题。`reply_to` 可省略，省略时自动关联第一条用户消息；不能指向别的会话或问题。整个规范化导入不得超过 128 KiB，不接受任意元数据、`system` 消息或外部文件路径。可选标题为 1～120 字符，仅创建新会话时使用，后续回写不会覆盖电脑已保存的标题。

用户消息可带 64 位小写十六进制 `image_id`：先 `image.upload` 确认图片已在电脑收件箱，再回写引用；导入正文和同步事件只保留 ID。电脑离线时，手机可先保留本地图片及对话，联网后按这个顺序回写。API 失败或结果未知时，不自动重调模型；尚未形成完整回复的单条用户消息不导入。

成功返回 `{"imported":true,"conversation_id":"…","message_ids":["用户 UUID","回复 UUID"],"duplicate":false}`。两个消息及持久操作回执在同一 SQLite 事务中提交；丢失响应可使用原请求 ID 和原参数重试，也可通过 `operation.get` 核实。完全相同的消息再次回写返回 `duplicate:true`，不会增加消息或再次调用模型。相同消息 ID 对应不同内容、会话或关联时返回 `import_conflict`，不修改原记录；会话或原消息已删除时返回 `conversation_deleted`，不会复活。缺图返回 `image_missing`，非法图 ID 返回 `invalid_image`；目标会话正在保存时可返回 `busy`，稍后沿用原 ID 重试。

导入会产生正常的同步变化，并异步刷新电脑正在查看的同一会话；不会替用户切换会话、触发桌宠回复或再调用模型。

## 请求去重和断线处理

`pet.command`、`chat.send`、`agent.send`、`agent.cancel`、`desktop.snapshot`、`image.upload`、`image.delete` 在执行前持久登记 `(设备 ID, 请求 ID)` 及操作摘要。同一 ID 重发相同操作不会再次执行，返回的 `result` 会带 `duplicate: true` 和既有操作状态；同一 ID 改用不同内容会被拒绝。登记是「最多接纳一次」，不是把 GUI、文件、数据库与 AI 服务合成一个原子事务。

请求超时或响应丢失时，先调用 `operation.get`，不要直接生成新 ID 再发同一条聊天。`state: complete` 的 `result` 内还包含该操作自己的 `ok`；`state: pending` 可能表示操作仍在处理，或电脑在登记后退出，不能据此自动再次调用 AI。结合原 `message_id` 和同步记录确认结果，必要时由用户明确决定后续操作。操作 ID 的去重记录会保留，重启后不自动回放未完成操作。

`chat.send` 与 `agent.send` 返回的稳定 `message_id` 根据设备 ID 和原请求 ID 生成。收到接受确认后应保存这个 ID，持续同步到对应消息变为完成、失败、取消或中断。`operation.get` 中的操作完成，只能证明投递流程结束，不能替代对 AI 最终消息或工具结果的检查。更换电脑或会话已删除后，原待处理请求不会自动转投其它电脑或新会话。

## 聊天记录同步流程

不要复制正在运行的 SQLite 文件。接口提供的是数据库中的公共记录投影，不包含 API Key、设备私钥或任意内部元数据。流程如下：

1. 从 `sync.snapshot` 的空 `cursor` 开始，保存返回的 `watermark`，按 `next_cursor` 获取所有页，直到 `has_more` 为 `false`。首次快照期间先暂存完整结果，再替换手机缓存。
2. 快照条目包含 `entity`、`id`、`conversation_id`、`deleted`、`data`；实体包括会话、消息和屏幕事件。以实体类型和 ID 去重保存。
3. 完整快照完成后调用 `sync.pull(since=watermark)`。变化条目额外包含递增的 `seq`；按顺序应用，`deleted: true` 为删除标记。
4. 持久保存每页的 `next_since`，下一次继续从该值拉取。只有本页已经写入手机缓存后才推进游标；通过 `has_more` 判断是否继续。不要用手机本地时钟代替序列游标。
5. 遇到 `sync_reset_required`，丢弃未完成快照或旧同步缓存，重新从空游标获取完整快照，再衔接增量。这样不会把电脑上已经删除的记录恢复回来。

每份快照最多 100000 条记录或 16 MiB，有效期 15 分钟，同时最多保留 2 份。超过容量会返回 `sync_error`，不会为了同步删除用户聊天历史。变化日志最多保留 10000 条或 32 MiB，按批压缩旧记录；过旧或超过当前历史的游标、过期/被替换的快照，以及快照期间所需日志已被截断的情况，均要求重新完整同步。

这些限额约束快照和变化日志的逻辑数据量，不是整个应用或 SQLite 文件的体积上限。聊天历史、保留的操作去重信息和删除标识仍会随使用增长。

## 图片收件箱

单张原文件最多 4 MiB，最多 1600 万像素，仅接收可解码的静态 PNG、JPEG、WebP。文件类型以内容检测为准；若提供 `mime_type` 或 `sha256`，必须匹配内容。`filename` 仅作显示标签，不能控制保存路径。

图片按内容哈希去重，收件箱图片内容总额为 64 MiB。超过限额时返回 `storage_full`，可用 `image.list` 查找，再用 `image.delete` 清理。`image.get` 按需获取原图。当前接收图片不会自动识图、自动回复或触发文件执行。

## 图文聊天

能力列表包含 `chat.image` 时，先 `image.upload` 获取图片 ID，再发送 `chat.send`：

```json
{"text":"这张图里写了什么？","conversation_id":"已有会话 UUID","image_id":"上传结果中的 64 位图片 ID"}
```

电脑在后台读取并校验图片，将当前问题和 `image_url` 的内嵌图片数据一起交给已有聊天模型，不需要新建 OCR 服务或另一个 API。聊天记录仅保存图片 ID，不保存 base64 或本地文件路径；公共同步消息的顶层 `image_id` 可供手机按需调用 `image.get` 显示图片。

普通图文聊天明确追问“上面这张图”时，可在原历史图片消息中提供仍可读取的图片上下文；当前文字消息不重复附上旧图，新话题和单独问号不重新携图，新会话不继承其它会话的图片。电脑 Agent 仅把本次显式添加的图片传入原生请求，后续轮次不自动再次携带历史图片字节。模型的图像 Token 不包含在界面显示的文本 Token 估算中。

非法图片 ID 会返回 `invalid_image`；引用不存在或已删除的图片会明确失败。图片在接纳聊天后被删除、无法校验或被模型接口拒收时，消息变为 `failed`，同步字段 `image_error` 返回下列白名单代码之一，不携带服务商原始响应：

| `image_error` | 含义 |
| --- | --- |
| `image_missing` | 图片不存在或已删除，需重新发送图片 |
| `image_unavailable` | 无法读取、校验图片或图片源不可用 |
| `vision_rejected` | 图文请求未被模型接口接受，请确认模型支持图片并检查接口配置 |

成功或没有图片错误时，`image_error` 为 `null`。缺图或接口拒图不会静默改成纯文字请求。请求超时、断线重发仍须沿用原请求 UUID 和全部原参数，并通过 `operation.get` 与同步消息确认状态，避免重复调用 AI。

## 开发客户端

`tools/remote_peer.py` 是用于验证协议的 Python 客户端，不是手机 App。它与电脑端建立真实的加密连接。连接票据和配对码从交互输入、标准输入或文件读取，不提供把秘密写进命令行参数的方式。控制台只输出请求 ID、成功与否、连接路径等摘要；完整聊天/图片结果需显式保存到新的 `--output-file`，不会覆盖已有文件。

在终端交互输入票据和配对码：

```powershell
.\.venv\Scripts\python.exe tools\remote_peer.py pair --name "Development peer"
```

客户端先询问连接票据，再询问配对码。票据是页面「复制连接信息」JSON 中 `ticket` 的字符串值，不能把整个 JSON 当票据粘贴。

也可将原始票据、配对码分别保存在当前账户的私有目录中，仅把文件路径传给工具：

```powershell
.\.venv\Scripts\python.exe tools\remote_peer.py --ticket-file "$env:LOCALAPPDATA\AURA\remote-peer-input\ticket.txt" pair --pair-code-file "$env:LOCALAPPDATA\AURA\remote-peer-input\code.txt"
.\.venv\Scripts\python.exe tools\remote_peer.py --ticket-file "$env:LOCALAPPDATA\AURA\remote-peer-input\ticket.txt" status
.\.venv\Scripts\python.exe tools\remote_peer.py --ticket-file "$env:LOCALAPPDATA\AURA\remote-peer-input\ticket.txt" image.upload --image-file .\sample.png --output-file "$env:LOCALAPPDATA\AURA\remote-peer-input\upload-result.json"
```

标准输入方式：预先准备一个两行的私有文件，第一行是原始票据，第二行是临时配对码，再执行：

```powershell
Get-Content -LiteralPath "$env:LOCALAPPDATA\AURA\remote-peer-input\pair-input.txt" | .\.venv\Scripts\python.exe tools\remote_peer.py pair
```

普通方法可通过 `--params-file` 提供 JSON 参数。若要验证超时重发的去重行为，使用 `--request-file` 提供完整请求，保留原 UUID 与全部原参数重发；工具默认每次生成新的请求 UUID，重复运行普通命令会被视为新操作。

开发客户端默认把自己的持久身份保存在 `%LOCALAPPDATA%\AURA\remote-peer\identity.key`，可用 `--identity-file` 指定其他位置。**这是开发工具的私钥文件，与桌面端使用系统凭据库的实现不同。** 同一测试设备须继续使用同一身份文件；勿把身份文件、配对文件或聊天结果提交到仓库、放入共享目录或写入公开日志。换用新的身份需重新配对。

开发过程中若看到 `connection_type: direct`，仅说明这次测试当时的选中路径为直连；`relay_configured: true` 仅说明票据包含中继地址，不代表已实际通过中继传输。跨网直连和中继可用性需要分别用真实网络测试。

## 验证

定向测试使用临时数据库和内存凭据，不读取生产设置或模型密钥：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_remote_direct_chat tests.test_remote_controller tests.test_remote_images tests.test_remote_sync tests.test_remote_ui tests.test_remote_transport tests.test_chat_images tests.test_chat_image_protocol -q

# 显式允许连接公共网络，运行真实加密端点和独立开发客户端测试
$env:AURA_REMOTE_NETWORK_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_remote_transport tests.test_remote_integration -v
Remove-Item Env:AURA_REMOTE_NETWORK_TESTS

# 真实 Qt 页面、开启/取消配对/关闭，并生成截图与单次工作集观测
.\.venv\Scripts\python.exe tests\remote_desktop_smoke.py
```

本机验证记录和截图位于 `artifacts/remote_connection/`。隔离桌面单次观测中，开启通信前后工作集增加约 13 MiB；当前安装的通信发行包文件合计约 15.3 MiB。这些数字不包含完整应用安装体积，也不代表传图峰值或内存保证。首次加载 SDK 后其共享运行时会常驻，关闭连接不会卸载整个库。

1.8.0 的已授权 `status` 返回 `agent_trace_version: 1`。手机按已配对电脑身份记录首次见到的新过程投影，在电脑晚于手机升级时也会重新获取一次历史快照；失败可重接，不清空可见缓存，也不重放任务。
