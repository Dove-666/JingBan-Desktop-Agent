# AURA Desktop Agent · 鲸鱼娘桌宠原型

Agent 执行链新增任务计划、Decimal 精确计算、CSV 源数据核算、独立产物验收和 SQLite 执行检查点。缺少材料或校验失败会保留待处理状态；重启后可在原会话继续，未知副作用不会自动重放。实现移植并借鉴了 [DeepSeek Harness 的计划与检查点机制](third_party/deepseek_harness/README.md)，保留现有 Python/PySide6、nanobot 和 GUI 后端；无需新增依赖或更换模型。详细范围见 [Agent 说明](docs/ON_DEMAND_AGENT.md#任务计划计算与验收)。源码更新后须完整退出桌宠再启动。

新增[桌面 Agent 模式](docs/ON_DEMAND_AGENT.md)：电脑和手机可按会话持续选择「电脑 Agent」，每条消息直接进入 nanobot 原生工具循环，后续“继续”沿用同一会话的工具结果，不经过普通聊天分类器或关键词分派，也不要求先截图。按电脑已启用的权限使用工作区文件、公开网页、只读系统和磁盘检查、应用打开和 UI-TARS 电脑操作；不提供任意 shell。可在「设置 → 任务助手」调整开关和电脑操作模型，任务默认不设总时长与步数上限，聊天中的「停止」终止当前任务。文本读写限于 `agent_workspace/`，本地目录元数据可通过独立只读工具检查；不另开常驻网关或定时任务。

聊天左下角的「设置」现为同窗页面：左侧选择常规、角色、模型与 API、屏幕感知、摄像头、长期记忆或远程连接，右侧直接编辑。返回聊天保留原会话与未发送草稿，密钥仍由原生控件及系统凭据管理。桌宠右键和托盘也使用此设置页，开发用的 `--debug-panel` 入口保留。

新增 Edge / Chrome 的“问问鲸鱼娘”右键入口：选中网页文字，打开带来源预览的提问框，确认后结合当前页面文字在原聊天中回答，支持后续追问和重试。使用现有模型配置，不新增付费服务。安装方法见 [网页划词提问](docs/DAILY_USE.md#选中网页文字问问鲸鱼娘)；扩展源代码位于 `browser-extension/whale-ask`，本地连接桥使用 `tools/install_browser_ask.ps1 -Apply` 登记。

新增可选的[手机远程连接](docs/REMOTE_CONNECTION.md)及[安卓伴侣 App 1.9.0](android/README.md)（versionCode 12）：在「控制台 → 远程连接」开启，支持一次性配对、撤销设备、桌宠指令、图文聊天与记录同步。「电脑 Agent」通过 `agent.send` 直接进入电脑实际工具后端，模式随会话保存，可查看固定阶段进度和安全错误，并用 `agent.cancel` 停止任务；可显式添加本次图片。「查看电脑桌面」移至顶部更多菜单，仍为独立截图，不是 Agent 启动条件。「手机聊天」保留独立 API 和配对配置同步，电脑离线仍可直接聊天，完整记录重连后回写；手动配置优先。安装包位于 `android/deliverables`，使用原签名覆盖安装并保留原数据。电脑源码更新后须从托盘退出 AURA 再重新启动，关闭聊天窗口不会重启旧进程。设备通信组件默认关闭；手机流量到家庭 Wi-Fi 和实际公共中继转发仍需实机测试。

1.9.0 恢复手机聊天顶部的当前会话主题，用可点击的小图标呈现电脑连接状态；减轻玻璃与控件视觉重量，精简连接/模型设置，步骤时间使用相对任务起点的 `+00:12`。桌面聊天合并详情入口，原生设置与托盘统一名称和状态。

1.8.0 新增可折叠执行过程：运行中显示动态状态图标、实际阶段、工具步骤和耗时，结束后自动收起，点击可展开历史。手机直聊记录请求与回复阶段，电脑 Agent 同步安全的操作记录；不读取模型隐藏推理、工具参数或请求上下文，不新增模型调用。保留单行电脑在线状态与无底部说明的布局。

1.7.0 保留可爱鲸鱼娘图标和 Liquid Glass 手机界面，顶部简化为单行「我的电脑（在线）」或「我的电脑（离线）」，不再显示会话标题与模型副标题；会话标题仍在历史列表中。模式选择和输入框下方不再放置说明或重复状态文字，任务进度与错误留在对应消息中。Android 12 / API 31 起非低内存设备采用原生背景模糊，Android 8 / API 26 起及低内存设备使用半透明回退；图标原图与提示保存在 `assets/branding/android/`。

电脑 Agent 现在可只读查找真实 Desktop / Start Menu 应用、桌面 EXE 和已安装应用标签，再使用当前候选 ID 打开目标；“好像叫 launcher.exe”这类后续说明可结合本会话继续查找，无需用户先报精确快捷方式或游戏平台。有实际歧义时才需澄清。Windows 截图优先使用原生 Win32 采集，统一图片与输入的 DPI 坐标；只对采集做有界回退，锁屏、会话断开、显示器变化或近黑帧会分别提示并停止，已派发动作不会因截图失败自动重放。

1.7.0 新增 `inspect_system`、`inspect_disk_usage`、`inspect_directory` 三个只读工具：直接取得内存、固定本地盘容量和有界目录元数据。磁盘分类检查采用 6 秒扫描预算，超出预算、权限不足或条目上限时保留部分结果；测得的逻辑文件大小不等于可回收空间，不执行删除。实际失败记录显示，原 C 盘检查在 9 次 GUI 动作后于约 123 秒被总超时终止；该任务现可先使用结构化只读数据。该历史版本的 GUI 子任务按剩余总时间扣除 10 秒收尾余量；当前版本已取消默认整项任务总超时，保留单次模型请求、工具操作和主动停止的保护。最终验证见 [1.7.0 验证范围](android/README.md#170-验证范围)，旧版结果按原批次保留。

支持 API 按当前语境选择表情：聊天、屏幕和摄像头分析共用七种表情规则，随原有请求返回选择，不增加调用。模型也可以只更新表情而不发表评论；用户聊天、拖动和隐私规则优先。可在「调试面板 → 角色控制 → 表情与动作」关闭「API 按语境选择表情」，改用手动选择，开关会保存。

新增独立的摄像头识别：在「调试面板 → 摄像头」打开开关即可查看本地预览，勾选独立的远程发送许可后，复用现有视觉模型进行画面识别与评论。托盘也提供摄像头开关，聊天工具菜单可打开摄像头设置。默认关闭，不录制声音，画面不落盘；关闭、暂停或设备断开时停止采集并取消后续结果。详见 [视觉感知说明](docs/PERCEPTION.md)。

气泡已更新为简洁的浅色卡片：按实际可见角色贴近定位，字号和卡片随桌宠同比缩放，新消息会在侧方、斜侧方及上下空间换位，长文自动分页。同一消息跟随动作保持方向，优先避开脸部；详见 [气泡体验调整](docs/SPEECH_BUBBLE_REFRESH.md)。定位使用 MIT 开源 [PyQt Tooltip](third_party/pyqttooltip/README.md) 的几何实现。

普通聊天的真实回复现在也会同步到桌宠气泡，保留段落与完整分页；点击气泡可回到对应消息。此前只切表情、不传正文的链路已接通，详见 [聊天气泡同步修复](docs/CHAT_SPEECH_DELIVERY.md)。

七种表情均支持[耳尾点击互动](美术资产/鲸鱼娘_全表情耳尾互动/README.md)：点击左右蓝色鳍耳或右侧尾巴会轻摆回弹，拖动优先，已有眼睛跟随保留；右键开关跨表情保持。开心保留闭眼效果，闭嘴备用图复用正常分层；详见 [全表情说明](docs/ALL_EXPRESSION_PARTS.md)。此前的 [正常立绘试版](docs/PART_INTERACTION_TRIAL.md)保留原批次记录。

已修复桌宠不能拖到边缘的问题：拖动与位置恢复现在按可见角色范围计算，透明画布留白不再挡住拖动，任务栏仍受保护。138 项相关测试及 Windows 四方向真实拖动检查通过，详见 [边缘拖动修复](docs/EDGE_DRAG_FIX.md)。

最新素材修复已接入：正常与傲娇立绘的左侧发丝白块已清除，两段动画重新处理了发丝孔洞和内缘白边，并采用固定蓝色校正改善偏暗。完整文件在 [美术资产修复包](美术资产/鲸鱼娘_抠图修复_v2/README.md)，本轮 **77 项定向测试及正式素材播放检查通过**，没有新增视频生成。方法、备份与验证范围见 [抠图修复说明](docs/ASSET_MATTE_REPAIR.md)；下方首批 640 项记录为修复前快照。

首批角色素材已发布：1024 透明原角色、抱胸傲娇静态图，以及「思考 · 挠头」「趴下 · 奶凶呲牙」两段真实视频分割动作。两动作各为 **62 张 RGBA 帧、12 FPS、5167ms**，已放入 `assets/character/animations/` 并通过真实素材播放 smoke。提交预算 **2/2 已用完，实际合计 3 元，无额外重试**。奶凶实际偏蹲跪而非完全趴卧；问号淡出 Alpha 包含估计，原生索引44的极小问号未独立分割。完整来源、操作、性能和限制见 [角色资产说明](docs/CHARACTER_ASSETS.md)，原始 `assets/whale.png` 保留。

真实资产阶段完整回归 **640 项通过，237.607 秒，零跳过、零失败**，见 [本轮报告](artifacts/character_assets_real_v1/unittest_report.json)；[发布素材播放 smoke](artifacts/character_assets_real_v1/real_playback/result.json)另行验证两段成品、缩放、拖动冻结、恢复和结束策略。Windows 原生 UIA 还实际触发了[挠头与蓝问号](artifacts/character_assets_real_v1/native_pet_think_middle.png)；原生奶凶播放及物理拖动尚未确认，双动作与拖动证据为隔离 Qt 测试。此前 [627 项](artifacts/character_assets/unittest_report.json)属于更早快照。运行 `.\.venv\Scripts\python.exe main.py --debug-panel`，到「角色控制 → 角色素材与序列帧」选择静态图或播放动作，日常无需安装 SAM2。若仍运行旧版本，先右键退出再启动。

R8 持续改善日常体验：更易读的聊天、独立草稿和快捷键、请求阶段提示、完整会话导出、托盘控制、窗口位置恢复、即时撤销屏幕权限，以及无需 Embedding 密钥的可选本地中文记忆检索。另提供不联网的只读启动诊断。操作见 [日常使用](docs/DAILY_USE.md)，实际文件清单、进度和限制见 [持续体验改进](docs/CONTINUOUS_IMPROVEMENT.md)。本地模型准备不会自动开启记忆或屏幕权限。

本项目为 Python + PySide6 的 2D 动画桌宠，包含独立调试面板、角色缩放、表情管理、可配置的 OpenAI 兼容 API、异步聊天，以及默认关闭的屏幕感知与主动互动。R7 增加 SQLite 独立聊天会话、可关联屏幕评论和可选的 Mem0 开源长期记忆；沿用已有透明 PNG、浮动/呼吸/弹跳与拖动交互，不使用 Live2D。

R7 / R8 的数据关系、数据库迁移、Mem0 安装配置、历史会话、可点击评论及上下文预算验收见 [会话与长期记忆说明](docs/SESSIONS_MEMORY.md)。完整对话、会话概况与长期记忆分别管理；普通聊天无需启用 Mem0。

最新完成的 R8 日常功能完整回归为 **565 项通过，227.156 秒，零跳过、零失败**，见 [完整快照报告](artifacts/r8_daily_unittest_report.json)。另外完成 [44 项隔离体验检查](artifacts/r8_experience_smoke.json)、真实本地 ONNX / Mem0 验证，以及后续诊断 11 项、窗口 / 记忆 43 项定向复核；专项与完整数不累加。R8 第一批的 [514 项结果](artifacts/r8_experience_unittest_report.json)是较早快照。下一批“运行时暂停自动记忆”仍在开发，尚未纳入已完成验收。

本轮未实测原生通知区注册/点击、物理鼠标、真实多屏热插拔和外部生产模型；隔离 Qt 渲染和 localhost 响应不等于这些原生或外部能力通过。R7 的 [463 项历史报告](artifacts/r7_credentials_unittest_report.json)、[后续验证](artifacts/r7_followup_validation_summary.json)及 [R7 文件与验收清单](docs/R7_DELIVERY.md)保留原批次标签，不作为 R8 新功能证据。

已[实际启动 R8](artifacts/r8_live_startup_report.json)，只读确认自身桌宠和控制台窗口可见。49.181 秒[空闲采样](artifacts/r8_live_idle_performance.json)的 CPU 为单逻辑核 2.41%、16 线程整机约 0.15%；这不包含聊天/模型工作，也不是压力测试或下一批功能验收。

R7 后续已补充记忆任务排队后的设置复核、包含等待时间的总期限、仅需 Embedding 的检索，以及同配置单实例启动和跨实例自观察过滤。再次启动会打开原桌宠的聊天窗口；带 `--debug-panel` 则打开原调试面板，详见 [R7 后续修复](docs/R7_FOLLOWUP.md)。

聊天默认总预算为 8192 Token；Mem0 SDK 提炼提示较大，独立使用可配置的 65536 总预算和 1024 输出预留，超额会明确拒绝提炼模型请求；在此之前 SDK 可能已调用 Embedding 检索旧记忆。记忆列表当前最多读取 1000 条，聊天每次最多使用 3 条相关记忆。

调试面板的完整使用、配置安全、验收结果和本次文件清单见 [调试面板说明](docs/DEBUG_PANEL.md)。

前台窗口观察、隐私过滤、变化检测、多模态分析及主动发言限制见 [屏幕感知说明](docs/PERCEPTION.md)。本地观察和远程发送分别启用并保存；默认不会截图或发送屏幕内容。

当前修订为 `2026.11.01-r14`。调试面板标题栏和右上角显示当前修订号。每次应用启动默认新建独立上下文，非空会话自动保存到本地历史。正式屏幕评论也会保存，点击关联气泡可打开原会话并追问。气泡现在按实际可见角色定位，支持上下、左右及斜侧方换位；卡片、内边距、圆角和字号随桌宠同比缩放，100% 时为 12pt，长文自动分页。定位移植自 MIT 开源的 [PyQt Tooltip](third_party/pyqttooltip/README.md)。普通反馈气泡鼠标穿透，关联评论气泡支持点击且不抢键盘焦点。

R3 修复“能分析画面却不评论”：新增默认“陪伴评论”和“只在重要时说话”两种互动风格，以及“评论当前画面”按钮。前轮截图中的 `no_actionable_event` 表示模型分类为 `none`；旧策略还要求重要度至少 0.65，两者使普通视频长期保持安静。R3 会为清晰的媒体内容保留候选评论，并在面板显示分类、候选评论和中文判定原因。

前轮本机验证汇总：[R2 修复报告](artifacts/r2_preview_fix_report.json)（报告位于不提交 Git 的 `artifacts/`）；该报告不是 R3 评论功能的测试结果，R3 以本轮实际验证记录为准。

## Windows 安装与启动

需要 64 位 Python 3.10 或更新的受支持版本。本机已建立 `.venv` 并安装依赖，实测环境为 Python 3.10.11、PySide6 6.12.0。

首次安装（PowerShell，在项目目录执行）：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 可选：需要 Mem0 长期记忆时安装
.\.venv\Scripts\python.exe -m pip install -r requirements-memory.txt

# 可选：在本机运行中文 Embedding，无需 Embedding API Key
.\.venv\Scripts\python.exe -m pip install -r requirements-memory-local.txt
```

如果没有 Python 3.10，可将第一行换为 `python -m venv .venv`，使用已有的 Python 3.10+。项目使用明确的虚拟环境路径，无需执行 `Activate.ps1` 或修改 PowerShell 执行策略。

正常启动：

```powershell
.\.venv\Scripts\python.exe main.py

# 启动时同时打开调试面板
.\.venv\Scripts\python.exe main.py --debug-panel
```

也可以双击 `start_pet.bat`，通过虚拟环境的 `pythonw.exe` 启动，不保留控制台。排查启动问题时使用上面的控制台命令。

在聊天左下角「设置 → 常规 → 启动」调整开机自启动与开机时静默启动。开机自启动仅注册当前 Windows 用户的 AURA 启动项，不需要管理员权限；取消勾选会移除这一项。开机时静默启动只保留系统托盘图标：双击打开聊天，右键可显示桌宠、打开设置或退出。若 Windows 托盘不可用，会自动显示桌宠。开关默认保持关闭，更改在下次自动启动时生效；手动打开程序始终显示桌宠，已有实例时也会恢复显示。

临时只启动到托盘可运行 `start_pet.bat --silent`。`--debug-panel` 明确要求显示调试窗口，优先于静默偏好。Windows 自启动使用项目的绝对路径；移动项目目录后，应在新位置重新关闭、开启自启动。

「设置 → 模型与 API」提供思考强度：默认、关闭、极低、低、中、高、很高、最高。选择后点击「保存配置」，从下一次请求生效；「默认」不发送额外推理参数，保留服务商默认行为。其他档位使用 Chat Completions 的 `reasoning_effort`，支持范围取决于模型；不支持时会显示明确错误，不会自动重试或悄悄降级。较高强度可能增加等待时间与输出预算消耗，可在「常规 → 上下文预算」调整输出预留。参数依据 [OpenAI Docs](https://developers.openai.com/api/docs/guides/reasoning?api-mode=chat)。

「设置 → 模型与 API」的「获取模型列表」会使用当前填写的 Base URL 和密钥（密钥留空则沿用已保存的密钥），在后台查询服务商的模型列表。无需先填模型名称；获取后可下拉选择、输入关键词筛选，也可继续手动填写。获取不会替换当前模型或自动保存，选择后点击「保存配置」生效。地址或密钥变化会清除旧选项并忽略迟到结果；不支持模型列表的服务商仍可手动配置。查询使用 [OpenAI Docs 的模型列表接口](https://developers.openai.com/api/reference/resources/models/methods/list)，不会发送聊天内容；列表中的模型能否聊天或识图仍需单独验证。

只读诊断不会启动桌宠、读取配置/凭据/聊天或联网，即使 PySide6 缺失也能打印依赖问题：

```powershell
.\.venv\Scripts\python.exe -m desktop_pet.diagnostics
.\.venv\Scripts\python.exe -m desktop_pet.diagnostics --json
```

诊断报告包含版本、64 位状态和 PNG 读取结果，默认不含用户路径或服务地址；它不代表 API、模型或实际窗口已经通过验证。详见 [诊断说明](docs/DIAGNOSTICS.md)。

## 使用与调整

- 左键按住角色拖动；超过系统拖动阈值后视为拖动，松开不会触发对话。
- 启动后自动轻微上下浮动并呼吸。短按交替触发开心、惊讶弹跳与气泡，并打开独立聊天窗口；普通点击反馈默认 3 秒后隐藏。
- 正常、惊讶、困倦、生气、思考和说话表情支持眼睛平滑跟随鼠标，开心表情保留闭眼效果。右键角色可切换“眼睛跟随鼠标”，该开关在表情切换后继续生效，只影响本次运行。拖动与恢复时冻结视线，视频动作使用自己的画面。每种表情的独立分层素材位于 `assets/character/eyes/`；单套素材缺失、损坏或与原表情不匹配时，该表情自动使用静态图。
- 按下即冻结当前动画姿态，拖动时由鼠标控制窗口；松开后平滑恢复待机。气泡不会阻挡拖动。
- 右键角色，选择“退出桌宠”。关闭时停止定时器并退出进程。
- 右键可打开“设置”或“打开聊天窗口”；设置和聊天共用同一个窗口，关闭窗口后桌宠和当前会话继续运行。
- 聊天侧栏支持新建、搜索和打开历史会话；每条会话的菜单提供查找、导出、重命名、清空与删除。重启默认空上下文，旧记录继续保留；打开历史后可继续该会话。“长期记忆”和“上下文预算”统一位于设置页，记忆默认关闭；开启「自动记忆」后程序为新用户准备本地中文模型并复用聊天服务，页面只保留开关和记忆管理，已有配置与记忆保留。
- 「设置 → 模型与 API」配置聊天服务；会话菜单的「导出对话」可保存完整 Markdown / JSON，包含正式屏幕评论及文字情境，不包含配置、内部 metadata 或截图。
- 发送时底部显示实际保存、记忆检索、生成和保存回复阶段；切换会话不会将旧请求的阶段或回复归给新会话。草稿和阅读位置分别保留。
- “设置 → 角色”支持 50%～200% 实时大小调整并保存，提供七种逻辑表情；缺少独立 PNG 时会明确提示，并使用原图、动作或气泡反馈。
- “屏幕感知”选项卡可单独启用本地观察和远程分析。默认每 10 秒本地观察；只有画面变化、权限允许且满足间隔和去重条件时才调用模型。点击“3 秒后本地观察”后切到目标应用，采集后返回面板查看；此按钮始终不外发。返回 AURA 时保留并标记上次安全预览，自身窗口不截图。
- “互动风格”默认“陪伴评论”：清晰的 `media` / `interesting` 情境在置信度至少 0.60、重要度至少 0.10 且有候选回复时可自动评论。“只在重要时说话”保留 0.65 重要度门槛；`none` 或无有效画面仍保持安静。修改后点击“保存参数”。
- “评论当前画面”将最近一次安全预览发给已配置 API，不新截图；需要本地观察和远程发送均已启用并保存。可以返回 AURA 面板查看旧预览后点击；此明确请求不受自动重要度、去重、发言冷却和配额限制，也不消耗或重置自动发言计数。隐私/未知窗口、其他窗口切换、禁用、请求占用或拖动仍会阻止或取消评论；错误时显示实际错误，不生成伪评论。
- 自动主动反馈支持安静、只动作和气泡发言，默认发言间隔仍至少 180 秒、每小时最多 6 次；聊天和拖动优先，未配置 API 仍可使用桌宠与本地观察。它不会评论每一帧视频。
- 透明空白和 PNG 的透明孔洞可以点击后面的软件；角色本体是鼠标交互区域。
- 桌宠置顶但不抢走键盘焦点、不显示任务栏按钮；整个窗口被限制在可用工作区中。Windows 自动隐藏任务栏的区域也会预留。

窗口和角色尺寸使用 Qt 逻辑像素；实际显示会跟随 Windows 的缩放比例。角色以窗口中心为基准按比例绘制，布局为气泡、最大弹跳和呼吸预留空间，因此 `--character-size` 是最大边长。超大自定义动作会被限制在可绘区域中，避免角色移出窗口；超过屏幕工作区的窗口也会自动缩小。

```powershell
# 调整窗口、角色和气泡时长
.\.venv\Scripts\python.exe main.py --width 420 --height 540 --character-size 400 --bubble-seconds 4

# 替换素材；带空格的路径需要引号
.\.venv\Scripts\python.exe main.py --image "E:\素材\我的角色.png"

# 查看全部参数，包括动画帧率 --fps（默认 30，范围 1–120）
.\.venv\Scripts\python.exe main.py --help
```

窗口设置位于 `desktop_pet/config.py`，动画参数集中在 `desktop_pet/animation_config.py`。默认图片是 `assets/whale.png`，由原有透明 PNG 原样复制，没有重绘角色。从其他工作目录启动也能找到默认图片；自定义的相对 `--image` 路径则相对当前终端目录。

## 动画状态与扩展接口

| 状态 | 行为与切换 |
| --- | --- |
| `idle` | 上下浮动 ±2.5 px，周期 4.8 秒；叠加 0.996–1.004 的轻微呼吸缩放，周期 3.8 秒。 |
| `happy` | 快速上跳约 12 px，再缓慢回落，总长 0.55 秒，完成后回到 `idle`。 |
| `surprised` | 短暂上跳约 7 px，总长 0.35 秒，完成后回到 `idle`。 |
| `dragging` | 最高优先级；按下时冻结当前位移、缩放和待机相位，鼠标只移动窗口，期间拒绝动作触发。 |
| `returning` | 拖动松开后用 0.4 秒平滑衔接到待机，完成后回到 `idle`。 |

呼吸是姿态的一部分，无需单独抢占状态。短按松开时交替触发 `happy` / `surprised`；用户动作可以打断正在播放的动作或恢复过程，并从当前姿态衔接。重复触发不累加位移，拖出再返回仍算拖动。

原始鲸鱼娘安静待机 4～7 秒后，会从 8 个 `idle_` 动作和姿态中随机播放一轮，之后间隔 8～16 秒再换动作，不会连续重复。其中「傲娇抱臂」复用现有静态 PNG，平滑淡入后停留约 2 秒，再淡回普通待机，保留呼吸效果；没有重新生成视频或新增 API 表情。聊天请求、回复气泡阅读、拖拽、隐藏桌宠以及手动动作预览期间暂停随机播放；对话中的表情沿用 API 语境选择。3 秒内连续点耳朵或尾巴 3 次（可交替点击），会短暂生气约 8 秒，继续点击不会无限延长；到时恢复待机，若正在读新回复则先恢复该回复的表情。自定义图片和另选的固定静态角色不使用鲸鱼娘的随机动作素材。

`AnimationController` 不处理窗口、图片或磁盘 I/O，只输出不可变的 `AnimationPose(offset_y, scale)` 和 `AnimationState`。现有的唯一帧 `QTimer` 用实际经过的秒数调用 `advance(dt)`；绘制与鼠标命中共享变换，旧位置每帧清为真实透明。原图、缩放图和透明命中数据均缓存，不在每帧重读或重建。

后续素材渲染器可通过 `PetWindow.animation.state`、`.pose`、`frame_tick` 接口选择帧或部件，继续复用当前时钟和鼠标交互。本阶段不实现眨眼、嘴型或鱼尾摆动。

## 文件结构

```text
main.py                         程序入口及缺依赖提示
requirements.txt                固定 PySide6、OpenAI SDK、keyring 版本
requirements-memory.txt         可选 Mem0 OSS 与本地 Qdrant 固定版本
requirements-memory-local.txt   可选 FastEmbed / ONNX CPU 检索，包含基础 Mem0 依赖
start_pet.bat                    Windows 无控制台启动器
desktop_pet/
  __init__.py
  app.py                        参数解析、应用启动、图片错误对话框
  application_controller.py     桌宠/面板/共享聊天/感知的生命周期
  tray.py                       托盘入口、显示/隐藏与感知运行时暂停
  diagnostics.py                只读依赖/版本/PNG 诊断，无网络或模型加载
  pet_controller.py             大小、表情、动作的统一角色接口
  expression_manager.py         七状态素材解析、缓存和缺图降级
  settings_manager.py           非敏感本地设置与系统凭据存储
  api_client.py                 兼容 API、响应解析和安全错误处理
  chat_controller.py            异步请求、会话上下文和情绪信号
  conversation_metadata.py      后台标题与增量概况生成
  async_tasks.py                可清理的 Qt 工作任务
  conversations/                SQLite、Token 预算、完整会话安全导出
  memory/                       Mem0、本地 Qdrant、远程/ONNX 检索与离线管理
  memory_controller.py           异步记忆管理接口
  config.py                     尺寸、图片路径、气泡和刷新配置
  animation_config.py           位移/缩放/时长/帧率与安全边界参数
  animation_controller.py       独立动画状态机与平滑姿态输出
  pet_window.py                 变换绘制、逆变换命中、鼠标与帧定时器
  bubble.py                     独立的气泡绘制与隐藏定时器
  placement.py                  屏幕选择和工作区位置约束
  windows_work_area.py          只读查询 Windows 任务栏及 DPI 坐标转换
  ui/                           调试面板、独立聊天视图与样式
    window_state.py             非敏感窗口位置恢复与工作区边界
    perception_panel.py         屏幕感知参数、预览与事件调试
  perception/
    settings.py                 默认关闭的参数、隐私及发言偏好
    screen_observer.py          Windows 前台元信息、过滤与窗口截图
    windows_capture.py          可见窗口区域采集、DPI/遮挡处理与资源释放
    capture_quality.py          未遮挡像素的近全黑检测，失败不调用模型
    change_detector.py          本地差异、基线和画面指纹
    context_analyzer.py         复用 API 传输的结构化视觉情境分析
    event_deduplicator.py       有界、过期的画面/事件去重
    proactive_agent.py          安静/只动作/发言决策与配额
    controller.py               定时、异步分析、取消和界面信号
tests/
  __init__.py
  test_pet.py                   Qt 交互与配置回归测试
  test_work_area.py             隐藏任务栏与缩放的回归测试
  test_animation.py             动画状态/连续性/缓存/边界/透明像素回归
  windows_smoke.py              Windows 原生鼠标、跨进程穿透与退出测试
  animation_smoke.py            真实事件循环动画与 CPU 采样
  missing_image_smoke.py        真实入口的缺图对话框与退出码验证
  test_settings.py              配置恢复、凭据与安全失败回归
  test_api_client.py            真实 SDK 对本地 HTTP 的成功/错误路径
  test_chat_controller.py       异步、上下文、密钥脱敏、取消和退出
  test_debug_ui.py              面板交互和中文输入法回归
  test_pet_controller.py        6 档缩放、表情素材和透明命中
  test_application.py           真实会话+HTTP的完整界面联调
  test_chat_experience.py       草稿、快捷键、阅读位置与小窗口操作
  test_chat_progress.py         阶段顺序、GUI线程和会话/请求归属
  test_conversation_export.py   完整只读快照、原子写入和导出界面
  test_export_edges.py          HTML/图片/坏关联及失败清理边界
  test_memory_offline_management.py 共享实体/namespace和离线管理
  test_diagnostics.py          缺依赖、PNG损坏与无敏感内容的只读检查
  experience_smoke.py          44项合成体验、渲染和缓存模型检查
  local_embedding_smoke.py     真实本地ONNX+Mem0，LLM仅localhost fixture
  panel_smoke.py                可复跑的面板验收、截图与报告
  test_perception_core.py       变化、事件去重与主动决策
  test_screen_observer.py       元信息/截图异常与隐私过滤
  test_windows_capture.py       区域裁剪、遮挡、DPI和GDI资源回归
  test_capture_quality.py       近全黑及遮挡像素排除回归
  test_context_analyzer.py      情境校验及多模态输入安全
  test_perception_ui.py         独立权限、参数及预览界面
  test_perception_integration.py 合成画面+真实本地HTTP完整流程
  perception_smoke.py           可复跑的感知界面验收、截图与报告
  native_perception_smoke.py    原生前台信息及专用合成窗口截图
docs/
  DEBUG_PANEL.md                面板、API、聊天和表情说明
  PERCEPTION.md                 屏幕感知、数据边界和验证说明
  SESSIONS_MEMORY.md             R7/R8 会话、Mem0、本地模型、导出与预算
  DAILY_USE.md                   聊天、托盘、模型准备、导出与诊断
  CONTINUOUS_IMPROVEMENT.md       R8实际文件清单、批次结果与限制
  DIAGNOSTICS.md                 本地只读诊断的字段和退出码
  R7_DELIVERY.md                 R7首轮文件清单、25条验收与验证边界
  R7_FOLLOWUP.md                 后续记忆并发、单实例与原生验证隔离修复
assets/
  whale.png                     现有透明素材的原样副本
  expressions/README.md         可选表情 PNG 文件约定，当前未提供
```

## 验证与复跑

R8 当前完整快照、专项范围和文件清单见 [持续体验改进](docs/CONTINUOUS_IMPROVEMENT.md)。可执行：

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m tests.experience_smoke
.\.venv\Scripts\python.exe -m tests.local_embedding_smoke
```

体验 smoke 全程 offscreen、临时数据和合成内容，不抢前台或读取桌面。本地模型 smoke 的语言模型是 localhost fixture，向量推理是真实 ONNX；需先明确下载公开模型。单独加 `--download` 可执行首次准备，最多等待 120 秒。两套脚本都不修改生产配置或启用记忆。

### 历史阶段记录，不能替代 R8 验收

R7 的测试与边界见 [会话与长期记忆说明](docs/SESSIONS_MEMORY.md#r7-历史验证记录)。下列 R2～R6 数字均为当时结果，旧版“只保留内存聊天”等边界已被 R7 的 SQLite 与可选 Mem0 设计替代。

R6 气泡方向、文字布局、桌宠与角色控制相关 **57 项通过**，其中新增 11 项箭头方向回归。覆盖上下左右、屏幕边缘偏移、负坐标、拖动、缩放、翻页、正文边界及极小工作区重叠时隐藏误导箭头。Windows 原生鼠标穿透、焦点、拖动与菜单检查通过；[四方向预览](artifacts/r6_tail_preview.png)已目视检查。未重复全仓测试或外部 API 检查，以下为前轮记录。

R5 气泡、桌宠与角色控制相关 **46 项测试通过**（其中气泡 18 项），Windows 原生鼠标穿透、焦点、拖动、任务栏边界和菜单退出检查通过。160 字评论在 75% 角色尺寸下以 14 号字体显示为 8 行，气泡 460×275 逻辑像素，[预览已目视检查](artifacts/r5_bubble_preview.png)。本轮未重复全仓测试或外部 API 验证；以下 R4、R3 为既有记录。

R4 完整回归 **310 项全部通过**（28.687 秒），新增 16 项气泡测试覆盖 160 字全文、字号、换行、缩放、屏幕边界、emoji 分页和生命周期。重新运行 Windows 原生交互检查，确认实际点击气泡不透明正文可穿透到另一进程，键盘焦点保留、拖动和任务栏边界正常。已检查仅渲染测试窗口的 [160 字预览](artifacts/r4_bubble_preview.png)。本轮未重跑外部模型准确率验证；R3 的真实 API 结果保留为前轮记录。

R3 完整回归 **294 项全部通过**（26.887 秒），包含 18 项评论链路集成、19 项分析器和 25 项感知界面测试。本轮重新运行感知 smoke 的 32 项及面板 smoke 的 29 项检查，均通过；语法检查和依赖检查通过，61 份源码/文档的真实密钥精确扫描为 0 命中。实际生产应用使用原观察器、用户已有配置与外部 API，在抖音位于前台时记录到新的 `companion_event` 自动评论，也确认了手动评论记录；未修改用户配置、保存屏幕图片或保存评论正文。证据见 [R3 评论验证报告](artifacts/r3_commentary_report.json)。本轮原生自动化的早期检查曾因控件选择器错误和前台切换未完成，原始失败记录保留，不计为通过；最终真实自动评论已单独验证。下方 R2 的 260 项数字及此前原生采集记录均为前轮证据。

```powershell
# 检查 Python 语法
.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests

# 回归测试（不使用真实鼠标，但会创建短暂测试窗口）
.\.venv\Scripts\python.exe -m unittest discover -v

# 交互式 Windows 实测：会短暂打开测试窗口并移动鼠标，请暂时停止操作鼠标
.\.venv\Scripts\python.exe tests\windows_smoke.py
.\.venv\Scripts\python.exe tests\missing_image_smoke.py

# 动画状态与 CPU 采样：1 秒预热、默认 12 秒真实事件循环采样
.\.venv\Scripts\python.exe tests\animation_smoke.py

# 独立面板+本地 HTTP 聊天验收；不需要外部 API Key
.\.venv\Scripts\python.exe tests\panel_smoke.py

# 合成画面+本地 HTTP 感知验收；不截图用户桌面、不访问外网
.\.venv\Scripts\python.exe tests\perception_smoke.py

# 原生采集专用合成窗口；零网络请求
.\.venv\Scripts\python.exe tests\native_perception_smoke.py
```

上述历史 Windows 原生复测曾覆盖置顶、焦点、跨进程透明空白/PNG 孔洞穿透、单击与拖动、工作区限制、面板关闭重开和独立聊天。退出曾通过真实菜单项的 Windows UI Automation InvokePattern 验证；这不是 R8 新托盘和新界面的物理点击验收。运行记录和截图写入 `artifacts/`，该目录不提交到 Git。

动画状态、极端调参边界、像素清理与缓存由 `test_animation.py` 验证；原有窗口和任务栏测试继续保留。CPU 报告同时给出采样墙钟时间、进程 CPU 秒数和单核心占用口径，避免与任务管理器的整机百分比混淆。最新结果见 `artifacts/animation_smoke.json` 与 `artifacts/windows_smoke.json`。

R2 完整回归 **260 项全部通过**（约 22.965 秒），包含 30 项感知集成和 19 项感知界面测试。手动观察倒计时期间自动轮询会直接跳过，不截图、不发 HTTP 请求，避免自动分析占用手动观察；结束或取消后恢复原有轮询，不改用户保存的开关、请求冷却或已有分析状态。语法检查、`pip check`、Git 空白检查均通过。感知 smoke 沿用前轮 32 项通过记录，覆盖返回 AURA 保留预览、不重复截图/请求及黑帧本地拒绝；面板 smoke 沿用前轮 29 项通过记录，R2 未重跑两套 smoke。两套 smoke 均使用真实 SDK 请求 localhost。前轮另用已配置服务完成实际外部文本请求及合成蓝色图片分析，并验证 Windows WinVault 隔离服务的写、跨实例读、删与清理；这些外部 API/凭据检查本轮未重跑。前轮对 57 个源码/文档文件的密钥精确扫描为 0 命中，报告不含真实密钥。相关证据见 `artifacts/external_api_smoke.json` 与 `artifacts/vault_smoke.json`。

感知验证覆盖默认关闭、隐私先过滤、静止画面跳过、去重、请求/发言配额、聊天和拖动优先、取消晚到结果、GUI 响应和仅内存数据。前轮将 HWND 窗口 DC 截图改为仅复制目标窗口的可见屏幕区域，并遮蔽其他窗口的遮挡区域；近全黑画面在本地拒绝，不交给模型。前轮原生 GDI QWidget 预览为 634×467、蓝色占 93.2%；NVIDIA RTX 4060 的 OpenGL 窗口预览为 634×432、蓝/橙分别占 47.7%/52.3%，均为自建合成窗口、零网络请求。证据见 `artifacts/native_perception_smoke.json`。

抖音窗口也已进行仅本地采集对照：旧窗口 DC 的黑色像素占比为 100%，新可见区域采集为 2.6%，预览 640×400；未调用模型、未保存截图，指标见 `artifacts/black_capture_fix_live.json`。抖音内容的模型语义识别和长期观察准确率尚未实测；受保护内容不尝试绕过，独占全屏和多屏混合 DPI 仍未实测。上阶段动画的 12 秒 `QApplication.exec()` 采样约 30.33 次/秒、单核心 CPU 约 2.21%，不是本次聊天或感知请求的性能测量。

R2 另在实际生产应用中通过 UI Automation 点击真实观察按钮，切到抖音后采集、返回 AURA 查看，并等下一次自动自身窗口观察后确认预览仍保留。版本标记、倒计时和预览均通过，没有替换观察器、修改用户配置或保存图片，见 `artifacts/live_r2_ui_report.json`。手动操作本身只做本地观察；本次保留用户原有的远程开关，未将整个生产会话认定为零网络请求。

实机验证范围是当前单屏、150% 缩放和自动隐藏任务栏环境。多屏、混合 DPI、其他任务栏位置和显示器热插拔有相应处理逻辑，但未做对应硬件环境的实机验证。负坐标和其他任务栏边缘的坐标运算由回归测试覆盖。系统安全桌面、独占全屏游戏等特殊窗口层级不在本次验证范围。

## 常见问题

| 问题 | 处理方法 |
| --- | --- |
| 提示缺少 PySide6 | 使用 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`，并用同一个解释器启动。 |
| 提示找不到图片 | 恢复默认 PNG，或用 `--image "完整路径.png"` 指定文件；界面会显示具体路径，控制台同时输出错误，确认后退出码为 2。 |
| 图片存在却读不出 | 确认文件为有效图片，而不是仅修改了扩展名；错误框会包含 Qt 的读取原因。 |
| 角色有白底 | 检查是否误选了原始 JPG，使用已抠图的透明 PNG。 |
| 角色太大或太小 | 调整 `--character-size`；同时增大 `--width`、`--height` 可提供更多空间。 |
| 想减弱动作或降低刷新率 | 调整 `animation_config.py` 的位移/呼吸幅度；也可启动时传 `--fps 24`。 |
| 双击启动器没有显示 | 用控制台启动命令查看报错；先确认 `.venv` 和依赖已安装。 |
| 感知没有预览/发言 | 查看“屏幕感知”状态，确认开关已保存；自身窗口、隐私过滤、空闲、稳定画面、请求间隔和发言配额均会跳过。 |
| 有分析结果却不评论 | 查看分类、候选评论和中文原因；`none` 或空回复不能发言。普通视频选择“陪伴评论”并保存；立即测试可检查安全预览后点“评论当前画面”，该按钮会发起真实 API 请求。 |
| 返回面板只看到旧预览 | AURA 在前台时不观察自身；点击“3 秒后本地观察”并及时切换目标应用。上次安全预览会明确标记。 |
| 无法确认是否运行新版 | 查看面板右上角和标题栏 `2026.11.01-r14`；旧进程不会热加载源码，需正常退出后从本仓库启动。 |
| 启动故障但不想打开窗口或暴露配置 | 运行 `python -m desktop_pet.diagnostics --json`，只查依赖和资源，不读凭据、聊天或屏幕。 |
| 想使用记忆但不懂模型配置 | 安装 `requirements-memory-local.txt` 后，在「设置 → 长期记忆」开启「自动记忆」；新用户的本地检索模型由程序准备，提炼复用聊天服务。 |
| 想保存一段历史 | 打开该会话，再用“工具 → 导出当前对话”；导出不受近期上下文条数限制，取消不会创建文件。 |
| 预览近全黑或有灰块 | 近全黑会在本地拒绝，不请求模型；灰块是其他窗口遮挡的遮蔽区域。将目标窗口保持可见后重试，受保护内容不会绕过采集限制。 |
| 聊天正常但图片分析失败 | 模型还需支持 `chat/completions` 的图片输入；文本连接成功不能证明视觉能力。 |
| 想退出 | 在角色不透明部分右键选择“退出桌宠”。 |

实现所依据的窗口行为可查阅 [Qt 透明 QWidget](https://doc.qt.io/qt-6/qwidget.html#creating-translucent-windows)、[Qt 屏幕工作区](https://doc.qt.io/qt-6/qscreen.html#availableGeometry-prop) 和 [Microsoft 任务栏位置查询](https://learn.microsoft.com/en-us/windows/win32/shell/abm-gettaskbarpos)。
