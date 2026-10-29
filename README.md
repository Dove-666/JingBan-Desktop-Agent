# AURA Desktop Agent · 鲸鱼娘桌宠原型

本阶段为 Python + PySide6 的 2D 动画桌宠，包含独立调试面板、角色缩放、表情管理、可配置的 OpenAI 兼容 API 与异步聊天。沿用已有透明 PNG、浮动/呼吸/弹跳和拖动交互，不实现屏幕感知、长期记忆或 Live2D。

调试面板的完整使用、配置安全、验收结果和本次文件清单见 [调试面板说明](docs/DEBUG_PANEL.md)。

## Windows 安装与启动

需要 64 位 Python 3.10 或更新的受支持版本。本机已建立 `.venv` 并安装依赖，实测环境为 Python 3.10.11、PySide6 6.12.0。

首次安装（PowerShell，在项目目录执行）：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果没有 Python 3.10，可将第一行换为 `python -m venv .venv`，使用已有的 Python 3.10+。项目使用明确的虚拟环境路径，无需执行 `Activate.ps1` 或修改 PowerShell 执行策略。

正常启动：

```powershell
.\.venv\Scripts\python.exe main.py
```

也可以双击 `start_pet.bat`，通过虚拟环境的 `pythonw.exe` 启动，不保留控制台。排查启动问题时使用上面的控制台命令。

## 使用与调整

- 左键按住角色拖动；超过系统拖动阈值后视为拖动，松开不会触发对话。
- 启动后自动轻微上下浮动并呼吸。短按交替触发开心、惊讶弹跳，同时显示“你好呀，我来啦！”，默认 3 秒后隐藏，重复点击重新计时。
- 按下即冻结当前动画姿态，拖动时由鼠标控制窗口；松开后平滑恢复待机。气泡不会阻挡拖动。
- 右键角色，选择“退出桌宠”。关闭时停止定时器并退出进程。
- 右键可“打开调试面板”或“打开聊天窗口”；面板关闭后桌宠和当前会话继续运行，可以再次打开。
- 调试面板支持 50%～200% 实时大小调整并保存，提供七种逻辑表情；缺少独立 PNG 时会明确提示，并使用原图、动作或气泡反馈。
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

`AnimationController` 不处理窗口、图片或磁盘 I/O，只输出不可变的 `AnimationPose(offset_y, scale)` 和 `AnimationState`。现有的唯一帧 `QTimer` 用实际经过的秒数调用 `advance(dt)`；绘制与鼠标命中共享变换，旧位置每帧清为真实透明。原图、缩放图和透明命中数据均缓存，不在每帧重读或重建。

后续素材渲染器可通过 `PetWindow.animation.state`、`.pose`、`frame_tick` 接口选择帧或部件，继续复用当前时钟和鼠标交互。本阶段不实现眨眼、嘴型或鱼尾摆动。

## 文件结构

```text
main.py                         程序入口及缺依赖提示
requirements.txt                固定本次验证的 PySide6 版本
start_pet.bat                    Windows 无控制台启动器
desktop_pet/
  __init__.py
  app.py                        参数解析、应用启动、图片错误对话框
  application_controller.py     桌宠/面板/共享聊天的生命周期
  pet_controller.py             大小、表情、动作的统一角色接口
  expression_manager.py         七状态素材解析、缓存和缺图降级
  settings_manager.py           非敏感本地设置与系统凭据存储
  api_client.py                 兼容 API、响应解析和安全错误处理
  chat_controller.py            异步请求、会话上下文和情绪信号
  config.py                     尺寸、图片路径、气泡和刷新配置
  animation_config.py           位移/缩放/时长/帧率与安全边界参数
  animation_controller.py       独立动画状态机与平滑姿态输出
  pet_window.py                 变换绘制、逆变换命中、鼠标与帧定时器
  bubble.py                     独立的气泡绘制与隐藏定时器
  placement.py                  屏幕选择和工作区位置约束
  windows_work_area.py          只读查询 Windows 任务栏及 DPI 坐标转换
  ui/                           调试面板、独立聊天视图与样式
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
  panel_smoke.py                可复跑的面板验收、截图与报告
assets/
  whale.png                     现有透明素材的原样副本
  expressions/README.md         可选表情 PNG 文件约定，当前未提供
```

## 验证与复跑

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
```

本机的 Windows 原生验证覆盖真实置顶标志、不抢前台焦点、跨进程透明空白/PNG 孔洞点击穿透、后台窗口继续键盘输入、单击交替动作与气泡、拖动冻结和恢复、拖后不误点击、工作区限制，以及真实入口的右键菜单退出。孔洞测试短暂停住计时器，以避免原生鼠标输入延迟造成像素位置漂移；其余交互在动画运行中验证。运行记录和截图写入 `artifacts/`，该目录不提交到 Git。

动画状态、极端调参边界、像素清理与缓存由 `test_animation.py` 验证；原有窗口和任务栏测试继续保留。CPU 报告同时给出采样墙钟时间、进程 CPU 秒数和单核心占用口径，避免与任务管理器的整机百分比混淆。最新结果见 `artifacts/animation_smoke.json` 与 `artifacts/windows_smoke.json`。

本次 113 项回归测试通过，包含既有动画与桌面交互测试。面板验收通过 29 项检查，使用真实 SDK 请求本地 HTTP 测试服务；外部服务商尚未配置，未声称外网模型已测试通过。上阶段动画的 12 秒 `QApplication.exec()` 采样为约 30.33 次/秒、单核心 CPU 约 2.21%，不是本次聊天请求的性能测量。

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
| 想退出 | 在角色不透明部分右键选择“退出桌宠”。 |

实现所依据的窗口行为可查阅 [Qt 透明 QWidget](https://doc.qt.io/qt-6/qwidget.html#creating-translucent-windows)、[Qt 屏幕工作区](https://doc.qt.io/qt-6/qscreen.html#availableGeometry-prop) 和 [Microsoft 任务栏位置查询](https://learn.microsoft.com/en-us/windows/win32/shell/abm-gettaskbarpos)。
