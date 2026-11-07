# 鲸伴 nanobot worker

鲸伴使用实际的 `nanobot-ai==0.4.0` SDK，保留自身桌宠、聊天窗口与数据库。电脑和 Android 的持续“电脑 Agent”直接构造 `AgentRequest(..., kind="agent")` 并进入原生工具循环，不调用普通聊天分类器，不要求特定动作词。文字回复可以保留显示，实际 Agent 任务的完成状态仍必须通过证据门；`tools` / `computer` 兼容入口保留各自的工具/电脑执行条件。

## 统一目录、依赖与执行范围

[tool_catalog.py](tool_catalog.py) 是工具名称、schema、公开标签、权限、平台和副作用类型的单一目录，当前 **42 项**。worker 注册与宿主能力列表使用同一目录；SDK 的默认插件/工具发现关闭。按本机权限和依赖返回实际集合，不能把目录总数或 `readiness` 配置检查当作全部工具已运行成功。

| 条件 | 工具 |
| --- | --- |
| 任务工具权限 | `read_file`、`write_file`、`edit_file`、`search_files`、`read_document`、`create_document`、`describe_artifact`、`verify_artifact`、`calculate`、`run_code`、`analyze_csv`、`web_fetch`、`web_search`、`inspect_image`、`system_info` |
| 任务工具权限及已安装的受支持 Edge/Chrome | `browser_navigate`、`browser_observe`、`browser_act`、`browser_tabs`、`browser_switch` |
| Windows 任务工具权限 | `inspect_system`、`inspect_disk_usage`、`inspect_directory`、`render_document` |
| Windows 任务工具权限、当前可用的独立渲染配置 | `inspect_document_page` |
| Windows 任务工具权限、有效宿主任务/会话身份 | `delegate_research` |
| Windows 任务工具权限、有效宿主任务/会话身份及明确启用的本机执行配置 | `compute_project_artifact`（本机尚未启用） |
| 任一任务工具/电脑操作权限 | `update_plan`、`finish_task`、`recall_context`、`ask_user_question`、`request_file_access` |
| Windows，任一权限 | `discover_applications` |
| Windows 电脑操作权限 | `open_application`、`desktop_windows`、`desktop_activate`、`desktop_view`、`desktop_observe`、`desktop_act`、`desktop_verify_state` |
| 电脑操作权限和有效 GUI 配置/组件 | `computer_use`、`verify_desktop_goal` |

`desktop_view(question)` 使用固定的只读 GDI 截图 helper，向已配置的主模型发送一次图片观察请求。它不需要 GUI 动作模型，主模型须支持图片。只返回有界事实描述、采集时间、显示器范围与图片指纹；原始 PNG 仅用于当次请求，不进入规划器上下文存储或公开执行过程。`scope=visual_observation_only`、`task_verified=false` 不能创建操作回执或通过最终 GUI 验收；当前一帧也不能独自证明“第二条”等导航顺序。截图、模型调用和停止清理保留各自的有界等待；截图 helper 退出不明会阻止父任务继续并保留明确原因。

只读观察用于补充信息，`computer_use` 用于执行具体输入。视觉子任务返回 `needs_user` 时不再按成功工具清空失败链；无关的成功观察、计划更新或上下文回读也不会抹掉已有失败记录。仍保留不同有效方案的恢复能力与不限总步数、总时长的配置。

文档读取/生成还依赖已部署的 `pypdf`、`python-docx`、`openpyxl`、`python-pptx` 等格式库。宿主可捕获本机 `runtime/office_renderer.json` 中的四项 LibreOffice 渲染配置；缺少该文件保留原 COM 路径，文件存在但无效则明确失败，不切换另一引擎重复执行。选定引擎仍需实际启动及进程收束核验。缺少浏览器会从实际可用工具集中排除；渲染、桌面会话、网络和模型的运行失败仍会由具体调用报告。没有运行时自动安装、任意 shell、删除工具、Cron、插件自动发现、任意权限子 Agent 或新的长期记忆服务。

`delegate_research` 只分配父任务已可读的 UTF-8 来源，每个子任务仅有 `read_source`；主任务复核引用后才能采用资料。`inspect_document_page` 用当前 PDF 的不透明页面引用核对一页，大图可使用明确标识的宿主传输预览；不将像素放入主任务历史，不据此宣称整份文档或任务完成。接线与实际验证范围见 [资料子任务](../../docs/RESEARCH_RUNTIME_INTEGRATION.md)和[渲染](../../docs/LIBREOFFICE_WORKER_INTEGRATION.md)。

`compute_project_artifact` 的宿主、工作进程及产物验收接线已完成专项检查，但本机启用文件仍未创建。固定可信测试发现 MXC 能读取自有隐藏窗口的标题；该桌面元数据边界正在修复，尚未运行模型生成的 Python。它只允许显式文件副本的离线计算和单个产物发布，没有任意终端、联网或包安装。详见 [项目计算工具](../../docs/PROJECT_EXECUTION_TOOL.md)。

应用启动、UIA 和视觉步骤可以在同一任务中顺序组合。`desktop_lock` 只串行化实际桌面操作，不再整轮禁止不同工具路径。模型可以提出原用户目标及用户纠正之内的具体下一步；动作需要新观察和当前引用。效果未知时保留账本状态，先读回/验收，不能改用另一个工具重复同一次副作用。应用发现用真实当前目录及不透明候选 ID；已确认的多个不同应用可分别启动，歧义、陈旧候选或 UAC 不自动重试。

## 文件、文档与产物

宿主提供默认工作区和已批准的 `file_grants`。`request_file_access(path, access, directory, reason)` 必须经过宿主展示精确路径并获得真实用户的布尔答复；模型不能自行改 `file_grants`。外部只读授权不变成写授权，文件、CSV、文档和验收工具共享同一授权对象，链接解析越界仍拒绝。

`read_file` 返回结构化页：`text`、`sha256`、`total_characters/total_lines`、`offset`、`next_offset` 和显式截断信息。读取页前验证范围；后续可带 `expected_hash` 防止版本漂移。`search_files` 提供分页和命中上下文。`write_file` 使用原子替换；替换需明确 `overwrite` 及已观察版本或 `expected_hash`。`edit_file` 替换精确文本片段，默认拒绝零匹配和多处匹配。用户明确的空文件可以通过验收。

`read_document(path, offset, limit, section, pages, expected_hash)` 读取 PDF/DOCX/XLSX/PPTX；`pages` 仅用于 PDF，例如 `1-3,5`；XLSX `section` 是工作表名，PPTX 是 1 基幻灯片序号。

`create_document` 的内容使用结构化 `spec` 对象：

```json
{"path":"report.docx","kind":"docx","spec":{"title":"验收记录","sections":[{"heading":"结果","paragraphs":["实际读回的内容。"],"table":{"headers":["项目","结果"],"rows":[["正文","通过"]]}}]}}
```

DOCX/PPTX 使用 `title` 和 `sections`；XLSX 使用 `sheets:[{name,columns,rows}]`。生成工具检查真实格式包和读回内容；字符串不暗中变为电子表格公式。`verify_artifact` 在最终写入后重新核对格式、版本和已声明的来源契约；`csv_summary` 必须按真实源文件、列及分组重新计算。可选契约不等于可以绕过最低格式校验或真实来源证据。

`render_document` 使用宿主捕获的明确渲染配置：已配置时运行独立 LibreOffice；缺少配置时才保留 Microsoft Office 路径。两者均不附着或关闭用户的 Office/WPS 实例。源版本在导出后复核；输出受写授权和原子发布保护，`expected_hash` 对应被替换的目标 PDF。包结构/内容读回、PDF 可导出、预览布局和用户最终目标是不同验证范围。较早批次的本机 Microsoft Office 检查失败：Word 显示修复提示，PowerPoint 要求启动交互，Excel 无可绑定安全文档对象；[原失败记录](../../artifacts/agent-upgrade-20261102/office/summary.txt)保持不变。后续独立 LibreOffice 路径已通过三种格式的固定样例，以及真实模型生成中文 DOCX、导出 PDF、观察页面和读回正文的受控任务；不据此宣称复杂 Office 排版或原 Microsoft Office 故障已修复。

最终 `artifacts` 仅包含宿主重新验收并复制保存的不可变交付副本，按会话隔离。元数据为 `reference/name/mime/bytes/sha256/verification_scope`；路径字符串本身不是交付证明。Android 用 `artifact.list/get` 按需分块取得文件，核对完整哈希后由系统另存为。详见 [远程协议](../../docs/REMOTE_CONNECTION.md)。

## 会话、上下文与历史图片

只有宿主同一规范 UUID、会话代际和安全范围的原生调用/观察可以作为工具历史回放；导入的手机聊天是参考文本。生产持久检查点 schema 2 保存待派发意图，执行事件及 ObservationStore 保存可恢复资料。热回放仍为最近 12 个完整轮次及约 256 KB，但更早事实通过来源摘录和归档引用恢复，不再只有丢弃标记。`recall_context` 使用会话/权限范围内的不透明引用分页，不能读其它会话。

活动任务会投影大结果并维护可恢复工作集。明确的模型容量/实际用量或真实超窗错误触发有界收缩与有限模型重试，不重放工具，也不静默去图；隐藏推理、密钥和图片字节不进入事实档案。图像目录只含当前会话允许的附件 ID。`inspect_image` 经 `resource_request/resource_reply` 向宿主按需取像素，再请求配置的图像模型；它不把旧图片重新挂到本次用户消息。不存在、已删除、跨会话或接口不支持的图片明确失败。

`update_plan` 可按实际证据取消或替代路线，保留稳定 ID 和理由；未做的步骤不能标完成。探索失败可由后续真实证据替代；必需材料、产物失败和未知副作用仍是阻塞。`finish_task` 先检查实际验收，不根据模型文字宣布成功。

实际 worker 使用 `TaskHarness(require_evidence=True)`：只有计划、`finish_task` 或纯文字、没有实际工具证据时，不把任务标为 `completed`；保留正文并使用已有 `needs_user` 状态。手机普通 direct API 对话不受此门限制，没有新增 `answered` 协议或关键词路由。证据存在也不免除具体目标、来源和产物验收。

跨模态验收仅由真实 `DesktopControls.observe` 写入 GuiBridge 内部有界记录（8 状态 / 64 KiB），剔除密码、私有 refs 与密钥；模型或任意工具文本不能伪造它。相邻相同状态忽略不同 snapshot UUID 后合并，保留首末真实捕获序号/时间及省略计数。独立 judge 联合这些 UIA 前后状态与 fresh 截图检查目标；捕获序号不是业务序号。没有 UIA 记录时保留原严格验收，目标身份、顺序和高信心门槛不降低，内部记录不作为公开 `agent_trace` 输出。

## 网络与匿名浏览器

`web_search` 使用已部署 SDK 的公开搜索，`web_fetch` 读取有来源哈希、链接和显式分页的公开 HTTP(S) 文本。读取拒绝 URL 凭据、非 HTTP(S)、私网/保留地址；校验所有 DNS 结果并固定连接 IP，HTTPS 仍验证原主机，每次重定向再检查。GET 不带用户 Cookie/Authorization，不执行脚本。原始来源最多 2 MiB，单次连接/读取和整体抓取分别有独立期限，页面 `limit` 最大 24000。

完整网页快照任务内 LRU 最多 8 份；更早正文留在 ObservationStore，元数据放任务私有临时索引，旧页不重新 HTTP 获取。无 store 的兼容调用读取已淘汰 URL 返回 `WEB_SOURCE_EVICTED`。输入分页参数先于任何网络访问验证。

DOM 工具启动独立匿名 Edge/Chrome，会话和进程由本任务拥有，不使用用户浏览器 Profile、Cookie、登录或已有标签。页面内容为不可信参考数据，元素引用需来自最新观察；已消费引用和未知提交不能盲目重放。没有承诺登录网站、验证码、用户账号任务或所有公开站点可用。

## 运行与私有控制协议

独立 `.venv` 的安装是部署步骤；任务运行不 pip/npm 安装或读取 `~/.nanobot` 旧配置。依赖见 `requirements.txt` / `requirements.lock`：

```powershell
uv venv --python 3.12 agent_backend/nanobot/.venv
agent_backend/nanobot/.venv/Scripts/python.exe -m ensurepip --upgrade
agent_backend/nanobot/.venv/Scripts/python.exe -m pip install -r agent_backend/nanobot/requirements.lock
```

宿主以参数数组启动 `python.exe -I -u worker.py`。一个进程仅接受一个任务；任务、凭据和已批准路径只经私有 stdin，不进入命令参数或公开日志。stdout 是 UTF-8 JSON Lines，不转发第三方原始诊断。示例中的参数须由宿主验证：

```json
{"type":"run","id":"task-1","kind":"agent","task":"创建并核对一份文档","conversation_id":"b80d4b08-0dc4-43c7-8f6d-e84f15bad50f","session_history":[],"context":"宿主提供的参考资料","config":{"base_url":"https://example.invalid/v1","api_key":"placeholder","model":"tool-capable-model"},"options":{"timeout_seconds":0,"max_steps":0,"workspace":"E:/github/AURA-Desktop-Agent/agent_workspace","tools_enabled":true,"computer_enabled":false}}
```

`ready/progress/activity/session/checkpoint/final/error` 记录生命周期；`checkpoint_ack` 必须在宿主持久提交后发出，才能派发下一步。`interaction` / `interaction_reply` 绑定任务 ID 与问题 ID，问答结果是实际用户输入；文件许可中只有宿主验证的 grant 可以扩展范围。`resource_request/resource_reply` 只取当前会话允许的图片。`steer` 绑定正在执行的任务，在下一次模型迭代加入真实用户纠正。普通聊天或外部资料不能伪造这些私有帧。

手机的 `agent.answer/agent.steer` 还核对已配对电脑、CID 和当前 pending/task；它们不进入 outbox，不自动重放授权或纠正。过期/stale 的答复被拒绝。公开状态和执行过程只投影有界安全标签，不输出隐藏推理、工具参数、文件正文和密钥。

默认 `max_steps=0` 和 `timeout_seconds=0` 表示真正不限整项步数/总时长，单次模型、采集、网络和工具仍有独立预算。`cancel` / `abort`、stdin EOF 或宿主取消会停止本任务及其拥有的进程树；已完成效果不回滚，未知结果不自动再做一次。锁屏、安全桌面、UAC、不可访问会话和明确的运行故障仍需停止或由用户处理。

## 当前验证边界

[真实跨层回归](../../artifacts/agent-upgrade-20261102/runtime-controls/handoff.txt)覆盖实际 Host→隔离 SDK→localhost 模型的问答、外部文件授权/拒绝/过期、版本编辑、交付哈希、运行中纠正、历史图片和真实 DOCX 包读回，另有跨端公开标签契约。合成响应不是实际云模型任务质量证明。

跨模态证据补齐前，三次独立 QA 尝试虽保存一次且字节正确，仍因无法证明时序而超时。修复后首轮[实机 GUI 验收](../../artifacts/agent-upgrade-20261102/real-gui-structured-verified.txt)使用当前配置模型，在自有全屏不透明 QA 窗口完成 8 次工具执行，约 33.781 秒；保存回调一次，控件和磁盘字节相符，parent verification 为 `verified` 且 finish 被接受。这不是任意应用、Office 或跨网手机链路通过的结论；Office 原生渲染仍受上文本机状态阻塞。

```powershell
agent_backend/nanobot/.venv/Scripts/python.exe -m unittest discover -s agent_backend/nanobot/tests -v
.venv/Scripts/python.exe -m unittest tests.test_agent_runtime_controls -v
```

## 历史验收记录（旧批次，不作当前能力说明）

以下保留原先批次的时间与验证范围。旧工具数量、整轮互斥、单一路径或旧上下文规则已由本文现行说明取代；测试数量不跨批次累加。

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


### 更早 SDK / UAC 回归批次


```powershell
agent_backend/nanobot/.venv/Scripts/python.exe -m unittest discover -s agent_backend/nanobot/tests -v
```

已通过 16 项测试（9.828 秒），使用真实 nanobot SDK、回环 HTTP SSE 假模型及临时工作目录，检查工具写/读回环、白名单、默认推理参数和 64000 字上下文、系统任务策略、凭据脱敏、互斥、进行中的模型请求取消、超时、无工具执行门槛、步骤上限、公开网页网络限制与路径限制。授权锚定修复后的 GUI 两项检查另已通过（0.029 秒），确认模型改写参数无法改变传给 Node 的原始用户目标。另已通过 `uv pip check`：117 个已安装包兼容。没有读取用户 API 配置，没有真实云模型请求，没有桌面截图或鼠标键盘输入。GUI SDK 的独立验证见 UI-TARS 目录。见 [验证记录](verification.json)。

官方依据：[Python SDK](https://github.com/HKUDS/nanobot/blob/v0.4.0/docs/python-sdk.md)、[Nanobot facade](https://github.com/HKUDS/nanobot/blob/v0.4.0/nanobot/nanobot.py)、[AgentLoop](https://github.com/HKUDS/nanobot/blob/v0.4.0/nanobot/agent/loop.py)。nanobot 使用 MIT 许可证。

应用打开通道与 GUI 错误码修复后的离线回归：**39 项通过（13.297 秒）**。新增 14 项应用解析、精确匹配、原任务绑定、去重、单次派发、Explorer 所属桌面、完整进程路径、可见窗口门槛及取消测试，启动均用假对象；真实 worker 的未知应用测试只读检索，不调用模型、不截图、不启动应用。真实应用启动验收由宿主统一执行，不能用这些离线结果替代。

UAC 处理修复后回归更新为 **45 项通过（13.100 秒）**，包含已有权限确认时不派发、新权限确认时手动接手、后台 COM 等待可取消等 6 项测试。真实本机已触发管理员确认，完整聊天链路已验证 `needs_user` 提示；等待用户处理系统权限不等于启动成功。
