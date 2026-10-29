# 桌宠调试面板

本次继续使用已有 Python 3.10+、PySide6、透明鲸鱼娘 PNG 与动画控制器。提供角色控制、API 设置和聊天测试三个选项卡，以及独立聊天窗口。没有实现屏幕感知、长期记忆或 Live2D，也没有改动原始图片。

## 安装和启动

Windows PowerShell，在仓库目录执行：

```powershell
# 首次使用时创建环境；已经存在 .venv 时跳过这一行
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

本机依赖已安装。也可双击 `start_pet.bat`，不显示控制台。依赖固定为 PySide6 6.12.0、OpenAI SDK 2.54.0、keyring 25.7.0。

角色不透明部分的右键菜单提供：

1. 打开调试面板。
2. 打开聊天窗口。
3. 退出桌宠。

面板是普通独立窗口，关闭时隐藏。再次打开沿用同一实例，不重置角色尺寸、表情或本次会话。两个聊天视图共享同一会话；退出桌宠会关闭所有窗口并取消在途请求。

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

- 普通设置使用 `QSettings` INI，默认在 Qt 当前用户 `AppConfigLocation/settings.ini`；通常为 `%LOCALAPPDATA%\AURA Desktop Pet\settings.ini`，以当前系统实际路径为准。仅保存 Base URL、模型、超时和缩放。
- API Key 明确使用 Windows Credential Manager 的 WinVault 后端，服务名 `AURA Desktop Agent`，账户名 `chat-api-key`，不会使用可能配置成明文的自动后端。
- 重启后请求会读取系统凭据；界面不自动把已存密钥回填成明文。清除按钮会删除系统对应凭据并再次读取核验。
- 凭据存储不可用时明确提示，不降级保存为明文。输入框中的临时密钥仍能用于连接测试；正常聊天使用已保存的设置和系统密钥。
- 禁用 SDK/HTTP 的敏感调试日志，不转发原始响应错误或异常追踪。当前密钥从回复、用户误粘贴的消息及错误文字中脱敏；聊天不写磁盘。
- Base URL 不接受用户名密码、查询参数和片段，避免将认证信息混进普通配置；API Key 应单独填写。
- `.gitignore` 已排除 `.env*`、本地设置、日志、运行时目录和测试输出。仓库不含真实 API Key。

## 聊天与情绪联动

Enter 发送，Shift+Enter 换行；中文输入法预编辑期间 Enter 不会误发。空输入和请求期间重复发送会被禁止。清空需确认，会一起清掉本次消息和上下文。

只在内存保留最近 12 个完整对话轮次、最多 48,000 字上下文及 50 条可见消息；单条输入最多 8,000 字。失败轮次不会被纳入后续上下文。退出后不保存聊天历史。

网络请求在 Qt 线程池工作任务内执行，内部异步 HTTP 有整体超时及退出取消。等待回复期间，角色拖动、尺寸、表情和界面操作继续响应；关闭面板不取消会话。退出程序主动取消在途请求，避免后台线程拖住退出。

请求开始时进入 thinking。模型可以返回 `{"reply":"你好呀！","emotion":"happy"}`；emotion 只允许面板中的七种状态，经统一 `PetController` 切换。普通文本、无效 JSON 或未知表情安全回退为普通文本/neutral，不执行模型输出。

## 实际验证与边界

113 项回归测试、Python 语法、依赖完整性和 Git 空白检查通过。另运行了真实会话面板验收及原生 Windows 桌面交互检查。

| 验收项 | 本机证据 |
| --- | --- |
| 1. 正常启动 | 真实 ApplicationController、透明桌宠及动画已运行 |
| 2. 面板打开/关闭/再开 | 同一面板实例保留角色状态，不退出桌宠 |
| 3. 实时大小/比例 | 六档比例、透明命中、动画边界及配置重读回归通过 |
| 4. 表情与缺素材 | 七状态降级；临时 PNG 的加载、缓存、热替换、损坏和删除均验证 |
| 5. API 设置恢复 | 临时 INI 新实例恢复；真实 WinVault 使用唯一临时 service 完成写、跨实例读、删与清理 |
| 6. 测试连接 | 实际 SDK 请求本地 HTTP，成功及 401/403/404/429/400/500、拒绝连接与超时分类均验证 |
| 7. 聊天 | 已通过本地兼容接口的真实 SDK/HTTP 对话；未配置外部服务商，外网模型尚未实测 |
| 8. 未配置/失败降级 | 界面错误可见，桌宠继续缩放、表情和动画 |
| 9. 异步不阻塞 | 慢回复期间界面操作及 15 ms Qt 定时器正常运行；面板验收中最大 tick 间隔约 31 ms |
| 10. 情绪驱动 | 实际 HTTP 返回 JSON，thinking→happy/surprised 经统一控制器联动 |
| 11. 桌面行为 | 原有拖动、透明穿透、置顶、菜单/退出回归；原生脚本另验面板和独立聊天入口 |
| 12. 密钥安全 | 明文配置不含测试密钥、日志/异常脱敏、系统删除核验与取消退出测试通过 |

凭据测试只使用随机临时 service 和合成标记并完成清理，不读取其他应用或生产命名空间。面板测试使用隔离 INI 和内存凭据，不改用户设置。截图中的 localhost 成功只代表本地兼容接口验证，不代表外部服务商已可用。

实机范围为当前 Windows 单屏、150% DPI 和自动隐藏任务栏；多屏、混合 DPI 和其他机器未实测。

复跑：

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe tests\panel_smoke.py
# 下一项会移动真实鼠标，请暂时停止操作
.\.venv\Scripts\python.exe tests\windows_smoke.py
```

测试报告和图片保存在 `artifacts/panel_smoke_report.json`、`artifacts/panel_smoke_*.png`、`artifacts/windows_smoke.json`，不提交 Git。

## 本次文件变更

新增源文件：`desktop_pet/application_controller.py`、`pet_controller.py`、`expression_manager.py`、`settings_manager.py`、`api_client.py`、`chat_controller.py`，以及 `desktop_pet/ui/__init__.py`、`styles.py`、`debug_panel.py`、`chat_panel.py`。

修改源文件：`desktop_pet/app.py`、`pet_window.py`、`animation_controller.py`、`bubble.py`、`main.py`、`requirements.txt`、`.gitignore`、`README.md`。

新增测试：`tests/mock_api.py`、`test_settings.py`、`test_api_client.py`、`test_chat_controller.py`、`test_debug_ui.py`、`test_pet_controller.py`、`test_application.py`、`panel_smoke.py`。修改 `tests/windows_smoke.py` 以检查新菜单入口及退出。

新增文档：`docs/DEBUG_PANEL.md`、`assets/expressions/README.md`。`assets/whale.png` 及此前的美术原图均未修改。
