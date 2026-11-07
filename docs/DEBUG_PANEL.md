# 桌宠调试面板

继续使用已有 Python 3.10+、PySide6、透明鲸鱼娘 PNG 与动画控制器。提供角色控制、API 设置、聊天测试、屏幕感知第四选项卡，以及独立聊天窗口。R7 增加持久会话、屏幕评论关联和可选 Mem0 长期记忆，不使用 Live2D，也没有改动原始图片。屏幕权限与流程见 [屏幕感知说明](PERCEPTION.md)，持久化、记忆配置及预算见 [R7 会话与记忆说明](SESSIONS_MEMORY.md)。

当前修订 `2026.10.30-r7` 显示在调试面板标题栏，面板右上角有“会话与记忆版 R7”，用于核对运行版本。气泡沿用四方向指向、至少 14 号字、默认最大宽度 460 逻辑像素、自动换行增高与不足时分页。普通反馈气泡鼠标穿透；保存成功的屏幕评论气泡接受点击，打开对应会话和情境摘要，仍不抢键盘焦点。

前轮本机验证汇总：[R2 修复报告](../artifacts/r2_preview_fix_report.json)（`artifacts/` 不提交 Git）。R3 新增评论功能的实际执行结果以本轮最终验证记录为准。

## 安装和启动

Windows PowerShell，在仓库目录执行：

```powershell
# 首次使用时创建环境；已经存在 .venv 时跳过这一行
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 可选 Mem0 长期记忆；不需要单独启动数据库或 Docker
.\.venv\Scripts\python.exe -m pip install -r requirements-memory.txt
.\.venv\Scripts\python.exe main.py

# 同时打开调试面板
.\.venv\Scripts\python.exe main.py --debug-panel
```

本机依赖已安装。也可双击 `start_pet.bat`，不显示控制台。依赖固定为 PySide6 6.12.0、OpenAI SDK 2.54.0、keyring 25.7.0。

角色不透明部分的右键菜单提供：

1. 打开调试面板。
2. 打开聊天窗口。
3. 退出桌宠。

面板是普通独立窗口，关闭时隐藏。再次打开沿用同一实例，不重置角色尺寸、表情或本次会话。两个聊天视图共享同一会话；单击角色也可打开独立聊天窗口。退出桌宠会关闭所有窗口并取消在途请求。关闭面板时，临时密钥输入恢复密码遮罩。

## 角色控制

滑块范围 50%～200%，提供 50%、75%、100%、125%、150%、200% 和恢复默认按钮。100% 指启动参数定义的基础尺寸，滑块不会把上次保存值再当作新的基准叠加。

窗口、角色区域、动画位移和气泡空间一起调整，保持图片比例，命中测试继续跟随变换。屏幕工作区不足时会自动限制实际绘制尺寸，避免裁切或覆盖任务栏。尺寸设置写入本地非敏感配置，重启恢复。

### 表情与素材

当前所有独立表情 PNG 均未提供，只有 `assets/whale.png` 原图。逻辑表情能切换，面板始终说明素材缺失；当前反馈不是实际面部表情变化。

| 状态 | 预留素材 | 缺图时反馈 |
| --- | --- | --- |
| neutral 正常 | `assets/expressions/neutral.png` | 原图，正常气泡 |
| happy 开心 | `assets/expressions/happy.png` | 原图，开心弹跳与气泡 |
| surprised 惊讶 | `assets/expressions/surprised.png` | 原图，短跳与气泡 |
| sleepy 困倦 | `assets/expressions/sleepy.png` | 原图，困倦气泡 |
| angry 生气 | `assets/expressions/angry.png` | 原图，生气气泡 |
| thinking 思考 | `assets/expressions/thinking.png` | 原图，思考气泡 |
| talking 说话 | `assets/expressions/talk_open.png`、`talk_closed.png` | 原图，说话气泡 |

添加完整角色透明 PNG 后，重新选择表情或点击“测试当前表情”即可加载。缓存会根据文件更新时间刷新，损坏或删除的图片自动降级。说话素材目前静态显示，优先 open、其次 closed，不宣称实现嘴型动画。原图不会被覆盖。

## API 配置

1. 打开“API 设置”。
2. 输入服务商的 Base URL，例如 `https://api.openai.com/v1`；也可以使用其他兼容服务商的根地址。
3. 输入该服务商提供的 API Key 和确实可用的模型名称。
4. 点击“保存配置”。空密钥框会保留已有密钥；删除请使用单独的“清除 API Key”。
5. 点击“测试连接”。按钮会真正发起一次短聊天请求，验证认证、端点和当前模型；可能产生服务商计费。
6. 收到成功结果后切到“聊天测试”，输入消息并发送。

接口使用可配置 `base_url` 的官方 SDK 和 `/chat/completions`。未强制专门的 JSON 输出模式，也不添加额外模型专属参数。模型名称由用户填写；测试不会把非空字段当作连接成功。[官方 Python SDK 文档](https://developers.openai.com/api/reference/python)、[Chat Completions 接口](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)

错误显示会区分认证失败、访问权限不足、模型/端点不存在、限流/额度不足、网络/证书问题、超时和其他服务商错误。默认整体请求时限 30 秒；不自动重试，避免重复请求。修改配置后，先前测试结果不会被当作新配置已通过。

### 设置与密钥存储

- 普通设置使用 `QSettings` INI，默认在 Qt 当前用户 `AppConfigLocation/settings.ini`；通常为 `%LOCALAPPDATA%\AURA Desktop Pet\settings.ini`，以当前系统实际路径为准。保存 Base URL、模型、超时、缩放及用户选择的屏幕感知参数，不保存截图、情境、聊天或 API Key。
- API Key 明确使用 Windows Credential Manager 的 WinVault 后端，服务名 `AURA Desktop Agent`，账户名 `chat-api-key`，不会使用可能配置成明文的自动后端。
- 重启后请求会读取系统凭据；界面不自动把已存密钥回填成明文。清除按钮会删除系统对应凭据并再次读取核验。
- 凭据存储不可用时明确提示，不降级保存为明文。输入框中的临时密钥仍能用于连接测试；正常聊天使用已保存的设置和系统密钥。
- 禁用 SDK/HTTP 的敏感调试日志，不转发原始响应错误或异常追踪。已知密钥从回复、用户误粘贴的消息及错误文字中脱敏；R7 聊天正文保存到独立的本地 SQLite，不写入 INI 或普通日志。
- Base URL 不接受用户名密码、查询参数和片段，避免将认证信息混进普通配置；API Key 应单独填写。
- `.gitignore` 已排除 `.env*`、本地设置、日志、运行时目录和测试输出。仓库不含真实 API Key。

## 聊天与情绪联动

Enter 发送，Shift+Enter 换行；中文输入法预编辑期间 Enter 不会误发。空输入和请求期间重复发送会被禁止。失败有明确提示和“重试”。“清空对话”需确认，仅清空当前会话的消息、屏幕事件及概况，长期记忆不受影响。

左侧“历史”提供新建、搜索标题和消息、打开旧会话、重命名及确认删除，按最近更新时间排列。每次启动使用新 UUID 和空上下文，首次消息才落库；主动打开旧会话后可继续原记录。当前标题明确显示正在使用的会话，关闭窗口不丢失会话，重启后历史仍保留。

完整历史保存在 `settings.ini` 同目录的 `conversations.sqlite3`，不会全部发送给模型。每次只使用当前会话概况、最多 16 条近期消息、最多 3 条相关屏幕事件与最多 3 条相关记忆，统一受 Token 预算约束；默认总量 8192、输出预留 1024。单条输入最多 8000 字符，问题无法完整放入预算时拒绝并提示，不静默截断。侧栏“上下文预算”可以调整总量与输出预留，页面显示本轮估算用量。详情见 [预算说明](SESSIONS_MEMORY.md#有限上下文与预算)。

侧栏“长期记忆”打开独立管理窗口，支持查看、搜索、删除、清空、暂停自动提炼，以及模型和 Embedding 配置。默认不启用；向量数据在本机，提炼和检索仍会向所配置的 API 发送必要短文本。记忆失败时普通聊天继续，删除会话也不默认删除 Mem0 记忆。完整 [安装和配置步骤](SESSIONS_MEMORY.md#mem0-配置与用户控制)。

Mem0 的内部提炼提示较大，使用独立于聊天的预算：默认总量 65536、输出预留 1024，可分别配置为 4096～262144 和 32～1024；预算不足会明确拒绝提炼模型请求。SDK 可能在此前已调用 Embedding 检索旧记忆，不能把预算拒绝理解为完全没有网络请求。较长候选和已有记忆仍可能超过默认预算，报错后普通聊天继续。管理列表和本地文本搜索当前最多处理 1000 条，聊天最终最多注入 3 条相关记忆。

网络请求在 Qt 线程池工作任务内执行，内部异步 HTTP 有整体超时及退出取消。等待回复期间，角色拖动、尺寸、表情和界面操作继续响应；关闭面板不取消会话。退出程序主动取消在途请求，避免后台线程拖住退出。

请求开始时进入 thinking。模型可以返回 `{"reply":"你好呀！","emotion":"happy"}`；emotion 只允许面板中的七种状态，经统一 `PetController` 切换。普通文本、无效 JSON 或未知表情安全回退为普通文本/neutral，不执行模型输出。

屏幕感知复用 API 和角色接口。R7 将被决策选为发言的屏幕评论保存为正式 assistant 消息，标记“来自屏幕感知”，并关联同会话的文字事件摘要；点击气泡或消息“查看情境”可进入对应对话。成功归档后若用户操作优先而抑制气泡，正式消息仍保留。截图不持久化，不会自动加入普通聊天。聊天请求与近期用户聊天仍优先于自动反馈；感知默认关闭，远程图片发送需要第四选项卡独立确认和保存。

本地预览使用“3 秒后本地观察”：点击后切换到目标应用，等待采集后返回面板。鲸伴自身窗口不截图，面板会明确标记保留的上次安全预览；隐私或未知窗口仍清空画面及标题。窗口采集已改为只复制目标的可见屏幕区域，遮蔽其他窗口的遮挡区域，近全黑画面在本地跳过，不再交给模型解释。详情显示实际截图方式。完整机制和边界见 [屏幕感知说明](PERCEPTION.md)。

R2 将手动倒计时设为优先：预约期间自动轮询直接跳过，既不采集也不发起新的屏幕请求，避免自动分析使手动观察在结束前被取消。完成或取消后恢复原有轮询，不更改已保存的本地/远程开关、请求冷却或已有分析状态。

### 屏幕评论与排查

R3 修复有情境分析却长期不评论的问题：此前提示将普通视频判为 `none`，策略首先保持安静；其余事件还需达到 0.65 重要度。现在“屏幕感知”提供“互动风格”，默认“陪伴评论”：清晰的 `media` / `interesting` 情境在重要度至少 0.10、置信度至少 0.60 且候选回复非空时可自动评论。选择“只在重要时说话”保留 0.65 重要度门槛。修改后需“保存参数”。无有效画面、`none` 或低置信度仍保持安静。

自动评论继续受默认 180 秒冷却、每小时最多 6 次及事件去重限制，聊天和拖动优先，不会每帧都评论。事件区域显示分类、候选评论、中文动作和中文原因，并保留原原因码；例如“主动发言冷却中（cooldown）”说明有候选内容，但此次没有发言。

“评论当前画面”用于立即测试：先使用“3 秒后本地观察”取得预览，再回到鲸伴检查内容并点击。该按钮将缓存的最近安全预览及有限元数据发送给 API，不新截图；本地观察和远程发送必须已启用并保存，按钮不会自行开启权限。手动请求可覆盖 quiet 风格对普通内容的静默要求，并绕过自动重要度、去重、冷却和配额；不消耗或重置自动发言计数。

手动评论仍需实际可评论内容和有效回复。隐私、排除、未知或其他窗口切换、权限关闭、请求/聊天占用及拖动仍会阻止或取消反馈；鲸伴前台可等待对旧安全预览的明确评论请求。API 失败会显示实际错误，不会用固定台词冒充评论。完整规则见 [屏幕感知说明](PERCEPTION.md#手动评论安全预览)。

## 实际验证与边界

R7 的实际执行结果见 [交付记录](R7_DELIVERY.md)，新增可复跑测试与限制见 [会话与记忆验证](SESSIONS_MEMORY.md#自动测试与证据边界)。下文 R2～R6 均为历史阶段记录，旧版不持久化聊天/记忆的描述不适用于 R7。

R6 相关 57 项测试通过，其中新增 11 项方向测试，覆盖四方向、边缘偏移、负坐标、拖动、缩放、分页和裁切。四方向测试窗口预览已目视检查；Windows 原生鼠标穿透、焦点、任务栏边界、拖动和菜单退出检查通过。本轮未重复全仓测试或外部 API 检查，以下为已有记录。

R5 气泡、桌宠与角色控制相关 46 项测试通过（其中 18 项气泡测试），Windows 原生鼠标穿透、焦点、拖动、工作区和退出检查通过。新增换行/空行分页和较小工作区重排计时回归。160 字、14 号字体、75% 角色的 460×275 逻辑像素预览已目视检查。本轮未重复全仓测试或外部 API 验证，以下为前轮记录。

R4 完整回归 **310 项通过**（28.687 秒），其中新增 16 项气泡测试；Windows 原生检查验证气泡不透明正文的跨进程鼠标穿透、键盘焦点、拖动和任务栏边界。160 字评论在 75% 角色尺寸下以 13 号字体完整显示为 8 行，气泡为 420×230 逻辑像素；目视检查了仅渲染测试窗口的预览。本轮未重跑外部模型语义识别，以下 R3 API 结果为前轮证据。

R3 本轮完整回归 **294 项通过**（26.887 秒），包含 18 项评论集成、19 项分析器和 25 项感知界面检查；感知 smoke 32 项和面板 smoke 29 项重新运行通过。语法、依赖、空白检查通过，61 份源码/文档的真实密钥精确扫描为 0 命中。

生产应用保持用户原有配置，使用真实抖音窗口与已配置外部 API，记录到新的自动陪伴评论和成功的手动评论；没有替换观察器、保存图片或将回复正文写入文件。汇总见 [R3 评论验证报告](../artifacts/r3_commentary_report.json)。原生自动化的早期控件选择和前台保持检查有未完成记录，已保留原始失败报告；最终真实自动评论另行通过。不同内容、服务商以及长时间准确率尚未全面验证。

以下为前轮 R2 及更早阶段的验证记录。

前轮 R2 完整回归为 **260 项全部通过**，用时约 22.965 秒，包含 30 项感知集成和 19 项感知界面测试。感知 smoke 沿用前轮 32 项通过记录，真实会话面板 smoke 沿用前轮 29 项通过记录，R2 未重跑两套 smoke；Python 语法、依赖完整性和 Git 空白检查均通过。原生桌面交互沿用前轮通过记录，退出采用真实菜单项的 UI Automation 调用，物理鼠标点击退出菜单项未验证。

| 验收项 | 本机证据 |
| --- | --- |
| 1. 正常启动 | 真实 ApplicationController、透明桌宠及动画已运行 |
| 2. 面板打开/关闭/再开 | 同一面板实例保留角色状态，不退出桌宠 |
| 3. 实时大小/比例 | 六档比例、透明命中、动画边界及配置重读回归通过 |
| 4. 表情与缺素材 | 七状态降级；临时 PNG 的加载、缓存、热替换、损坏和删除均验证 |
| 5. API 设置恢复 | 临时 INI 新实例恢复；前轮真实 WinVault 使用唯一临时 service 完成写、跨实例读、删与清理 |
| 6. 测试连接 | 实际 SDK 请求本地 HTTP，成功及 401/403/404/429/400/500、拒绝连接与超时分类均验证 |
| 7. 聊天 | 本地兼容接口的真实 SDK/HTTP 对话通过；前轮已配置服务的实际外部文本请求成功 |
| 8. 未配置/失败降级 | 界面错误可见，桌宠继续缩放、表情和动画 |
| 9. 异步不阻塞 | 慢回复期间界面操作及 15 ms Qt 定时器正常运行；最新实测指标见对应 smoke JSON |
| 10. 情绪驱动 | 实际 HTTP 返回 JSON，thinking→happy/surprised 经统一控制器联动 |
| 11. 桌面行为 | Qt 与原生回归覆盖拖动、透明穿透、焦点、置顶、面板重开和独立聊天；实际“退出桌宠”菜单项经 Windows UI Automation InvokePattern 验证退出，物理鼠标点击该退出项未验证 |
| 12. 密钥安全 | 明文配置不含测试密钥、日志/异常脱敏、系统删除核验与取消退出测试通过 |

前轮凭据写读/删除测试使用随机临时 service 和合成标记并完成清理，不读其他应用凭据，结果见 `artifacts/vault_smoke.json`。面板和感知 smoke 使用隔离 INI 和内存凭据，不改用户设置。截图中的 localhost 成功代表本地兼容接口验证；前轮实际外部配置另完成文本及合成蓝色图片请求，均成功，见 `artifacts/external_api_smoke.json`。外部 API 和 WinVault 检查本轮未重跑。前轮密钥对 57 个源码/文档文件的精确扫描为 0 命中；报告没有输出密钥、实际地址或模型名称。

前轮原生感知验证覆盖专用 GDI QWidget 和 NVIDIA RTX 4060 OpenGL 窗口：预览分别为 634×467、634×432，蓝色占 93.2%，GPU 窗口蓝/橙占 47.7%/52.3%；均只采集自建合成窗口、零网络请求，见 `artifacts/native_perception_smoke.json`。前轮本地抖音采集对照也通过：旧 HWND 窗口 DC 黑色像素占 100%，新可见区域采集占 2.6%，预览 640×400；未保存截图或调用模型，见 `artifacts/black_capture_fix_live.json`。抖音的模型语义识别、持续观察准确率和受保护内容未实测。

R2 在实际生产应用中使用 UI Automation 点击真实观察按钮，确认版本标识、倒计时、切到抖音采集后返回面板的预览，以及下一次自动自身窗口观察后仍保留预览。没有使用替代观察器、修改用户配置或保存图片，证据见 `artifacts/live_r2_ui_report.json`。手动观察不调用 API；此轮保留用户原有远程开关，不声称整个生产会话零网络请求。

实机范围为当前 Windows 单屏、150% DPI 和自动隐藏任务栏；多屏、混合 DPI 和其他机器未实测。

复跑：

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe tests\panel_smoke.py
.\.venv\Scripts\python.exe tests\perception_smoke.py
.\.venv\Scripts\python.exe tests\native_perception_smoke.py
# 下一项会移动真实鼠标，请暂时停止操作
.\.venv\Scripts\python.exe tests\windows_smoke.py
```

测试报告和图片保存在 `artifacts/panel_smoke_report.json`、`artifacts/panel_smoke_*.png`、`artifacts/perception_smoke_report.json`、`artifacts/perception_smoke_*.png`、`artifacts/native_perception_smoke.json`、`artifacts/native_perception_probe.png`、`artifacts/live_r2_ui_report.json` 和 `artifacts/windows_smoke.json`，不提交 Git。

## 本次文件变更

新增源文件：`desktop_pet/application_controller.py`、`pet_controller.py`、`expression_manager.py`、`settings_manager.py`、`api_client.py`、`chat_controller.py`，以及 `desktop_pet/ui/__init__.py`、`styles.py`、`debug_panel.py`、`chat_panel.py`。

修改源文件：`desktop_pet/app.py`、`pet_window.py`、`animation_controller.py`、`bubble.py`、`main.py`、`requirements.txt`、`.gitignore`、`README.md`。

新增测试：`tests/mock_api.py`、`test_settings.py`、`test_api_client.py`、`test_chat_controller.py`、`test_debug_ui.py`、`test_pet_controller.py`、`test_application.py`、`panel_smoke.py`。修改 `tests/windows_smoke.py` 以检查新菜单入口及退出。

新增文档：`docs/DEBUG_PANEL.md`、`assets/expressions/README.md`。`assets/whale.png` 及此前的美术原图均未修改。

后续屏幕感知阶段扩展了上述 API、设置、角色控制及面板模块，并新增 `desktop_pet/perception/`、`ui/perception_panel.py` 和相关测试；包括 `tests/native_perception_smoke.py`，以及 `test_settings.py` 的参数重载/损坏授权安全降级、`test_pet_controller.py` 的无素材静默动作回归。本轮黑屏修复新增 `desktop_pet/perception/windows_capture.py`、`capture_quality.py` 和 `tests/test_windows_capture.py`、`test_capture_quality.py`，修改观察器、控制器、感知面板与相关回归。完整清单见 [屏幕感知文件清单](PERCEPTION.md#本阶段文件清单)。

R2 另修改 `desktop_pet/__init__.py`，并在已有 `app.py`、`ui/debug_panel.py` 中接入修订号；修改 `perception/controller.py`、`ui/perception_panel.py` 及对应的集成/界面测试，修复手动倒计时与自动轮询竞争。

R3 更新已有感知设置、情境分析器、主动决策器、控制器和感知面板，加入 companion/quiet 模式、手动评论安全预览、分类与候选回复、中文动作和原因；同步更新修订标记、相关回归与三份使用文档。
