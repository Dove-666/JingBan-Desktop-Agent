# 手机远程连接（电脑端）

AURA 已加入可选的加密设备通信模块，可接收桌宠指令、文字聊天和图片，并提供聊天记录同步接口。手机 App 尚未实现；目前可在电脑上开启服务、管理配对，并用开发客户端验证协议。

## 开启和配对

入口为「控制台 → 远程连接」，也可以右键系统托盘图标，选择「手机远程连接」。新配置默认关闭，不会因为打开设置页就启动连接；主动开启后会保存开关，之后启动 AURA 时恢复。关闭开关会停止本次服务、取消未完成的配对。

通信组件单独放在 `requirements-remote.txt`，不会要求安装额外的组网 App。本项目当前 `.venv` 已安装 `iroh==1.2.0`。在其他环境中，若页面显示「可选通信组件未安装」，在项目目录执行后重启 AURA：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-remote.txt
```

开启并显示「远程连接已开启」后，点击「生成配对码」。它会生成一个仅可使用一次、有效期 5 分钟的随机配对码，以及可复制的配对信息。取消、过期、配对成功或关闭连接后，配对码失效；界面会清除显示，且仅在剪贴板仍是本页复制的临时配对信息时清除该内容。当前提供的是复制功能，没有二维码界面。

配对成功后，电脑将加密连接中验证的对端公钥加入本地白名单。后续连接需持有对应私钥，不能只凭设备名称或请求 JSON 中填写的身份连接。最多保存 16 个已配对设备；在列表中选择设备并点击「撤销选中设备」后，它需要重新配对。电脑端的长期设备私钥保存在系统凭据库，Windows 上使用 Windows 凭据管理器，不写入普通设置文件；无法访问凭据库时不启用明文回退。

「复制连接信息」包含公开的设备标识与连接票据，不包含配对码，也不能替代配对。临时配对信息包含授权所需的秘密，只交给自己的设备。

## 网络和当前边界

两台设备可以处在不同网络，例如手机流量与家中的 Wi-Fi。通信模块配置了公共中继发现，能够尝试直连，并在需要时通过加密中继转发。无须自己部署服务器；这仍可能借助第三方公共基础设施。电脑需保持开机、联网并运行 AURA。

公共中继没有本项目提供的可用性保证。本轮已通过本机两个真实加密端点之间的配对、指令、图片收发、聊天回复同步，以及在原连接中撤销授权的测试，SDK 实测路径为 `direct`。手机流量到家庭宽带的实际可达性，以及公共中继实际转发的可用性，尚未验证；本机直连结果不能代表跨网测试通过。当前不提供延迟、运行内存或所有网络均可达的保证。

手机 App、移动端锁屏后台连接、系统推送、手机离线消息队列及移动端缓存界面尚未实现。当前电脑负责 AI 请求和回复生成；同步接口以电脑保存的记录为准，不接收手机数据库文件或任意历史记录导入。收到图片会进入本地图片收件箱，不会自动调用视觉模型或自动加入聊天。

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
| `status` | `{}` | 返回协议版本、状态、聊天是否忙碌、当前会话 ID 和能力列表 |
| `pair` | `code`；可选 `name`，默认为「手机」 | 使用临时码授权该加密连接的真实设备身份；名称为 1～80 个字符，不得含控制字符 |
| `pet.command` | `action`、`value`、`expires_at` | 执行有限的桌宠操作，见下表 |
| `chat.send` | `text`；可选 `conversation_id` | 接受 1～8000 字符的非空文字；会话 ID 须为规范 UUID，不传时使用电脑当前会话 |
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

传入 `chat.send.conversation_id` 可以向指定会话发消息，不切换电脑端当前查看的会话。聊天忙碌或配置不可用时会拒绝；目标会话不可用也可能导致接纳或后续保存失败，应使用同步记录中的有效会话 ID。成功响应中的 `accepted: true` 仅表示已接受请求，不表示 AI 回复已经完成；随后通过 `sync.pull` 获取用户消息的 `request_status`、AI 回复和 `reply_to`。AI 在电脑端运行，手机端不额外生成同一条回复。

## 请求去重和断线处理

`pet.command`、`chat.send`、`image.upload`、`image.delete` 在执行前持久登记 `(设备 ID, 请求 ID)` 及操作摘要。同一 ID 重发相同操作不会再次执行，返回的 `result` 会带 `duplicate: true` 和既有操作状态；同一 ID 改用不同内容会被拒绝。登记是「最多接纳一次」，不是把 GUI、文件、数据库与 AI 服务合成一个原子事务。

请求超时或响应丢失时，先调用 `operation.get`，不要直接生成新 ID 再发同一条聊天。`state: complete` 的 `result` 内还包含该操作自己的 `ok`；`state: pending` 可能表示操作仍在处理，或电脑在登记后退出，不能据此自动再次调用 AI。结合原 `message_id` 和同步记录确认结果，必要时由用户明确决定后续操作。操作 ID 的去重记录会保留，重启后不自动回放未完成操作。

`chat.send` 返回的稳定 `message_id` 根据设备 ID 和原请求 ID 生成。收到聊天接受确认后应保存这个 ID，持续同步到对应消息变为完成、失败、取消或中断。`operation.get` 中的操作完成，只能证明聊天投递流程结束，不能替代对 AI 最终消息的检查。

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
.\.venv\Scripts\python.exe -m unittest tests.test_remote_controller tests.test_remote_images tests.test_remote_sync tests.test_remote_ui tests.test_remote_transport -q

# 显式允许连接公共网络，运行真实加密端点和独立开发客户端测试
$env:AURA_REMOTE_NETWORK_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_remote_transport tests.test_remote_integration -v
Remove-Item Env:AURA_REMOTE_NETWORK_TESTS

# 真实 Qt 页面、开启/取消配对/关闭，并生成截图与单次工作集观测
.\.venv\Scripts\python.exe tests\remote_desktop_smoke.py
```

本机验证记录和截图位于 `artifacts/remote_connection/`。隔离桌面单次观测中，开启通信前后工作集增加约 13 MiB；当前安装的通信发行包文件合计约 15.3 MiB。这些数字不包含完整应用安装体积，也不代表传图峰值或内存保证。首次加载 SDK 后其共享运行时会常驻，关闭连接不会卸载整个库。
