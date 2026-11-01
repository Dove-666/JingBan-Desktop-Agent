# AURA nanobot worker

## Direct Agent entry (current desktop / Android mode)

`AgentRequest(task=original_user_text, kind="agent")` goes straight into the
real nanobot native tool loop. It does not call ordinary chat to classify the
turn or produce a routing JSON object. Normal answers and clarification
questions without tool calls are valid Agent results. The older `tools` and
`computer` kinds below remain compatibility entry points with their older
completion requirements.

The host passes `context={api, messages, gui_api_key?, conversation_id?,
current_image_url?}`. `conversation_id` must be a canonical UUID belonging to
the host's conversation. `messages` accepts only text role/content references;
it cannot import tool results or action permissions. A current explicit image
can be a PNG/JPEG/WebP data URL up to 4 MiB. It is sent as a native image block
only on that turn; prior replay replaces pixels with a text placeholder.

The enabled native tool schemas share one loop:

- `read_file`, `write_file`, `search_files`, `web_fetch`, and read-only
  `system_info` follow `tools_enabled`. Files stay inside `agent_workspace`;
  `web_fetch` reads a supplied public URL, with no internet search.
- `open_application` follows `computer_enabled` on Windows. The model selects
  an exact installed shortcut name explicitly present in the latest user
  task. Existing shortcut, executable, UAC, process/window verification and
  single-dispatch guards still apply. This selection does not route chat.
- `computer_use` follows `computer_enabled` plus a configured GUI endpoint,
  model and key. Screenshot acquisition happens only inside this actual tool,
  with no startup capture prerequisite. The original user goal is bound to
  the GUI bridge; model arguments cannot replace it. A turn uses at most one
  desktop tool path, with cached terminal results preventing duplicate actions
  or a fallback to the other desktop tool after an uncertain outcome.

`AgentService.readiness(settings, key, context)` returns
`available/reason_code/reason/tools/capabilities` without starting a worker or
observing the desktop. Disabled permissions are reported and never enabled
implicitly. `system_info` reports OS/release/architecture, Python version,
workspace and its free disk bytes; it does not enumerate Windows services or
execute commands.

Native assistant tool calls and tool observations travel over a private
`session` protocol frame. Only the host-owned transcript for the same UUID is
replayed as native roles on the next turn. This memory-only cache retains at
most 64 conversations, 12 whole turns and 256 KB per conversation, with
explicit truncation markers. Keys and image pixels are removed. It is cleared
on application shutdown; `clear_session(id)` / `clear_session()` supports host
conversation deletion and clear. Imported phone/chat messages stay reference
text and cannot inject native roles or authorize an old action. Interrupted
tool calls retain an explicit uncertain outcome; they are never replayed as
new calls or automatically retried. Only the latest user turn authorizes work.

`max_steps=0` keeps the true unlimited SDK iteration adapter. Overall timeout,
Stop, bounded protocol frame size and process-tree cleanup still apply; there
is no cumulative output byte limit imposing a hidden iteration cap.

Offline direct-entry acceptance:

```powershell
agent_backend/nanobot/.venv/Scripts/python.exe -m unittest discover -s agent_backend/nanobot/tests -p test_agent_mode.py -v
```

An explicit real-provider smoke test uses a fresh synthetic QA workspace and
reads configured credentials only in memory. It writes/reads a file, then
continues by appending to the same file on the next UUID-bound turn. GUI is
disabled and production settings are verified unchanged:

```powershell
.venv/Scripts/python.exe agent_backend/nanobot/verify_native_agent.py --run-real-provider
```

The real-provider run on 2026-11-01 passed both exact-byte checks: 3 + 4 native
model turns and 2 + 3 completed tool calls. Evidence is in
`agent_workspace/qa-direct-agent-437856bfd5d4451aa594b2db5885fca4/verification-real-agent.json`.
This validates file-tool execution and continuation; it does not establish
general GUI or office-application task quality.

The subsequent real GUI acceptance used only a verified opaque, foreground,
single-monitor QA window. The native Agent selected `computer_use`, then the
configured GUI model clicked its input, entered a unique token containing
Chinese text, clicked Save and observed its success label. The final run
completed in 17.547 seconds with exactly one save callback and exact matching
widget text, label and UTF-8 file bytes. All production settings remained
unchanged. Evidence:
`agent_workspace/qa-gui-agent-f4dae0a2ac5f487ba3518d9a37ac64e3/verification-real-gui.json`.

This acceptance exposed and fixed two Windows text-input defects: the pinned
operator's clipboard read could fail before typing, and its alternative
libnut `typeString` corrupted non-ASCII input. The adapter now uses a fixed
standard-library Python `SendInput` helper with UTF-16 Unicode events, native
Enter events, foreground identity checks and exact dispatched-event counts.
The host's actual interpreter path travels in validated private options;
typed text stays in stdin, with no shell or clipboard access. Cancellation
kills and waits for that owned helper, with no automatic replay.

An additional owned `QPlainTextEdit` probe passed exact comparison of leading
and trailing spaces, Chinese characters, emoji and an internal newline:
`agent_workspace/qa-gui-agent-759f1e6562b2405fa1f157cbbf5a252a/verification-real-gui.json`.
Both probes are opt-in via `verify_gui_agent.py --run-real-gui`, with
`--native-typing-probe` selecting the text-input probe. They refuse unsafe
desktop coverage before capturing any pixels. These results validate the QA
window task, without claiming arbitrary application or Office task quality.

Final direct-entry regression: 72 backend tests, 39 UI-TARS Node tests and 18
host runtime tests passed. Image-provider rejection remains explicit; the
adapter prevents the SDK's default fallback from silently removing images.

使用真正的 `nanobot-ai==0.4.0` Python SDK 执行一次已派发的任务。它保留 AURA 原有界面与聊天数据库，不启动 nanobot Gateway、Cron、Heartbeat、第二个聊天窗口或长期记忆服务。

## 运行

本目录 `.venv` 使用 Python 3.12，与桌宠现有 Python 3.10 环境分开。安装是部署步骤；worker 运行时不会下载依赖。

```powershell
uv venv --python 3.12 agent_backend/nanobot/.venv
agent_backend/nanobot/.venv/Scripts/python.exe -m ensurepip --upgrade
agent_backend/nanobot/.venv/Scripts/python.exe -m pip install -r agent_backend/nanobot/requirements.txt
```

`requirements.lock` 记录本机已验证的 Windows/Python 3.12 依赖版本。当前环境安装的是校验过 PyPI SHA256 的官方 0.4.0 源码发行包，成功构建并真正安装；未修改上游代码。这样无需下载约 54 MB、附带本任务不用的终端 UI 的 Windows wheel。重新部署也可使用 `pip install --no-binary=nanobot-ai -r agent_backend/nanobot/requirements.lock`。部署时允许包构建；运行任务时不执行安装。

宿主以参数数组启动：

```text
E:\github\AURA-Desktop-Agent\agent_backend\nanobot\.venv\Scripts\python.exe -I -u E:\github\AURA-Desktop-Agent\agent_backend\nanobot\worker.py
```

所有输入输出使用 UTF-8 JSON Lines。任务与凭据只放 stdin，不放命令参数、环境变量或日志。stdout 仅输出协议；第三方 stdout/stderr 日志不会被转发。

```json
{"type":"run","id":"task-1","kind":"tools","task":"在工作目录创建一份待办清单","context":"宿主提供的相关上下文","config":{"base_url":"https://example.invalid/v1","api_key":"placeholder","model":"your-tool-capable-model","reasoning_effort":"medium"},"options":{"timeout_seconds":0,"max_steps":0,"workspace":"E:\\github\\AURA-Desktop-Agent\\agent_workspace"}}
```

一个进程仅接受一个任务。额外 run 返回 `BUSY`，不会新建第二个 Agent。启动输出 `ready`；进度只有 `planning/tool/computer/finishing` 等固定阶段；完成输出 `final {id,reply,emotion,status,tools_used}`，错误输出 `error {id,code}`，不输出原始服务端异常。

```json
{"type":"cancel","id":"task-1"}
```

`abort` 同义；stdin EOF 也会取消。超时会取消 SDK 请求和其拥有的 GUI 子进程。GUI 先发送 abort，等待 2.5 秒后清理自己的进程树；已经发出的物理动作不会被撤销。宿主还应使用自己的进程树截止机制。

## 工具边界

- `tools` 模式仅注册 `read_file`、`write_file`、`search_files`、`web_fetch`。文件工具支持 UTF-8 文件与工作目录内搜索，不是全盘或互联网搜索。
- `web_fetch` 只读公开 HTTP(S) 网页（80/443 端口），无需密钥。拒绝 URL 凭据、file 协议、localhost、私网/保留地址；所有 DNS 结果均校验并将连接固定到已验证的公网 IP，HTTPS 仍验证原主机名的证书，每次重定向重新检查。仅 GET 文本，不带 Cookie、Authorization 或执行网页脚本；8 秒连接/读等待、20 秒读取期限、256 KiB 响应和 16000 字符提取上限。这些是单次网页工具的资源预算；默认整项任务没有总时限，用户停止仍会结束宿主拥有的进程树。`options.timeout_seconds=0` 表示不建立整项任务计时器；显式正数保留用于兼容和隔离测试。
- 当前没有互联网搜索工具；需要来源链接时会明确请求提供，不能假装已经搜索。网页结果标记为不可信参考数据，不能作为额外操作授权。
- 至少一次成功的真实工具执行后才可返回 `completed`；模型只输出文字、没有实际调用工具时返回 `needs_user`，不会把“口头已完成”交给聊天界面。
- 文件路径必须位于宿主授权的 `agent_workspace` 或其子目录；拒绝越界、绝对外部路径和解析后越界的符号链接。写入默认不覆盖；覆盖需要模型明确选择 `overwrite=true`，任务本身也应包含用户授权。
- 未注册 Shell、任意命令执行、删除、Registry、Cron、子 Agent、消息投递、用户记忆或自定义 MCP 启动命令。
- SDK 状态使用本目录 `runtime/task-*` 的临时空目录，任务结束清理。调用 `Nanobot.run(..., ephemeral=True)`，不读取 `~/.nanobot/config.json`、旧 Skills、用户记忆或既有会话；委托文件输出与这个临时 SDK 状态分开。
- SDK AgentLoop 的默认工具发现被关闭。实际列给模型的工具是本地显式白名单，不能通过配置添加任意工具。

## 电脑模式

`kind="computer"` 时额外注册 `computer_use(task)`，它调用相邻目录的 [UI-TARS worker](../ui_tars/README.md)。单独打开已安装应用的明确请求使用下述 `open_application` 受控通道。电脑模式另需：

```json
{"gui":{"base_url":"https://example.invalid/v1","api_key":"placeholder","model":"your-gui-model","provider":"ui-tars-1.5"}}
```

这段放在 run 的 `config` 中。GUI provider 是 UI-TARS 动作解析格式，允许 `ui-tars-1.5`、`ui-tars-1.0`、`doubao-1.5-15b`、`doubao-1.5-20b`。规划模型必须支持 OpenAI 兼容 Chat Completions 的工具调用；GUI 模型还必须输出对应 UI-TARS 动作格式，二者可以使用不同端点与凭据。

每个任务最多派发一次完整 GUI 目标，最终发送的始终是宿主提供的原始用户任务；规划模型的工具参数不能改写或扩大授权。内部由 UI-TARS 执行多步，防止规划模型失败后自动重复已有副作用。规划模型在同一任务中再次调用 `computer_use` 时，复用首次终态的状态、摘要和步数；并发调用也只启动一个 GUI 子进程，不重复输入，也不再因 `GUI_ALREADY_USED` 把已有结果覆盖为失败。首次失败保留原错误码；取消、超时和需要用户接手的结果不会被重试或升级为成功。

仅工具任务不会导入原生 GUI 模块或启动 Node。普通电脑任务若没有实际调用 GUI 工具，返回 `COMPUTER_NOT_USED`；`needs_user/cancelled/timed_out` 均保留原状态，不视作成功。SDK 到达步骤上限也返回 `needs_user`，不会把自动生成的总结当作已完成。此桥接不负责提升管理员权限。

### 打开应用

原始用户请求完整符合“打开/启动 + 单一简短应用名”（可带“在桌面上”）时，computer worker 使用同一个 nanobot `open_application` 受控工具直接处理，不再请求规划模型或启动 UI-TARS。宿主普通聊天模型仍先决定是否派发 computer 任务，因此普通对话不会因为这条规则自行启动 Agent。解释、示例、路径、命令、多应用和附加动作不会进入此快捷通道。

工具只枚举 Windows 已知的个人/公用桌面与开始菜单 Programs 文件夹中的 `.lnk`，跳过 junction、符号链接，限制深度与文件数量，按 Unicode、空白与大小写归一后的**完整名称**匹配。不同目标的同名快捷方式需要用户澄清；同目标重复链接只启动一次。当前仅支持本地现有 `.exe`、无附加参数的链接，拒绝命令解释器和脚本宿主；不提供模型可指定的执行路径或命令参数。

启动通过**已运行的 Explorer 桌面** `Document.Application.ShellExecute` 完成，并核实该桌面窗口属于 Windows 的 `explorer.exe`，不使用 worker 中直接的 `ShellExecuteW` 或 subprocess 后备方式。这样应用归 Windows 桌面管理，不会在任务 worker 的 Job 回收时被一起终止。启动前重新检查快捷方式，最多派发一次；之后只读核验目标进程的**完整可执行路径**与可见窗口，最长等待 12 秒。未找到、歧义、启动异常或未验证窗口均返回 `needs_user`，不会改开别的应用或再调用视觉工具重复启动。验证窗口表示应用或其启动器已经打开，不代表已进入游戏、完成更新或通过登录。

Explorer 的 COM 调用在独立初始化 COM 的 daemon 线程内执行，接口不跨线程传递；等待 UAC 时不会阻塞任务的 async 取消和状态检测，也不使用退出时会等待线程的 ThreadPoolExecutor。启动前若已有 `consent.exe` 或输入桌面不可访问，直接返回 `needs_user`，不排队发送另一条启动请求。本次请求发出后若出现新的 UAC/受保护桌面，也立即说明需要用户手动在系统弹窗选择“是”或“否”，不自动授权、不提高权限、不重复启动；已归 Explorer 所有的系统弹窗与应用不作为任务辅助进程清理。

`gui.coordinate_profile` 支持 `native`（默认）与 `pixel`；后者只适用 `ui-tars-1.5`，供使用截图像素坐标的兼容视觉模型使用。该字段仍由 UI-TARS 桥处理，不影响上述明确应用打开通道。

## 离线验证

```powershell
agent_backend/nanobot/.venv/Scripts/python.exe -m unittest discover -s agent_backend/nanobot/tests -v
```

已通过 16 项测试（9.828 秒），使用真实 nanobot SDK、回环 HTTP SSE 假模型及临时工作目录，检查工具写/读回环、白名单、默认推理参数和 64000 字上下文、系统任务策略、凭据脱敏、互斥、进行中的模型请求取消、超时、无工具执行门槛、步骤上限、公开网页网络限制与路径限制。授权锚定修复后的 GUI 两项检查另已通过（0.029 秒），确认模型改写参数无法改变传给 Node 的原始用户目标。另已通过 `uv pip check`：117 个已安装包兼容。没有读取用户 API 配置，没有真实云模型请求，没有桌面截图或鼠标键盘输入。GUI SDK 的独立验证见 UI-TARS 目录。见 [验证记录](verification.json)。

官方依据：[Python SDK](https://github.com/HKUDS/nanobot/blob/v0.4.0/docs/python-sdk.md)、[Nanobot facade](https://github.com/HKUDS/nanobot/blob/v0.4.0/nanobot/nanobot.py)、[AgentLoop](https://github.com/HKUDS/nanobot/blob/v0.4.0/nanobot/agent/loop.py)。nanobot 使用 MIT 许可证。

应用打开通道与 GUI 错误码修复后的离线回归：**39 项通过（13.297 秒）**。新增 14 项应用解析、精确匹配、原任务绑定、去重、单次派发、Explorer 所属桌面、完整进程路径、可见窗口门槛及取消测试，启动均用假对象；真实 worker 的未知应用测试只读检索，不调用模型、不截图、不启动应用。真实应用启动验收由宿主统一执行，不能用这些离线结果替代。

UAC 处理修复后回归更新为 **45 项通过（13.100 秒）**，包含已有权限确认时不派发、新权限确认时手动接手、后台 COM 等待可取消等 6 项测试。真实本机已触发管理员确认，完整聊天链路已验证 `needs_user` 提示；等待用户处理系统权限不等于启动成功。
