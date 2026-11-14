<p align="center">
  <img src="assets/branding/whale-maid-pixel.png" width="112" alt="鲸伴像素鲸鱼娘图标">
</p>

# 鲸伴 AURA

**一个能陪你聊天、理解屏幕情境，也能接受电脑任务的 Windows 桌宠。**

鲸鱼娘住在桌面上：你可以拖动、互动、换角色，与她聊文字或图片；开启屏幕感知后，她会结合当前画面选择表情或评论。Android 伴侣让你在手机继续聊天、同步角色和记录，也能把任务交给正在运行的电脑。

[下载安装](https://github.com/Dove-666/JingBan-Desktop-Agent/releases) · [开始使用](docs/GETTING_STARTED.md) · [电脑 Agent](docs/ON_DEMAND_AGENT.md) · [Android](android/README.md)

## 能做什么

| 功能 | 使用方式 |
| --- | --- |
| 桌面陪伴 | 拖动角色、点击互动，使用表情和动作回应；可调整大小、暂时隐藏。 |
| 图文聊天 | 文字与图片对话、Markdown 回复、独立会话、历史搜索和草稿保存。 |
| 情境感知 | 检测前台画面变化，结合用户忙闲、冷却和配额决定何时回应，减少重复打扰。 |
| 角色切换 | 导入 PNG / JSON 角色卡，保存到角色库，在电脑和手机同步使用。 |
| 电脑任务 | 基于 DeepSeek Harness 执行文件、命令及 Windows 操作，显示实际执行过程，支持停止、确认和文件下载。 |
| 手机接续 | 手机可独立调用 API 聊天；连接电脑后同步记录、上传任务文件并接收结果。 |

另有象棋、五子棋和可选的长期记忆。棋类使用本地引擎，不配置聊天 API 也能对弈。

## 安装和第一次使用

1. 到 [Releases](https://github.com/Dove-666/JingBan-Desktop-Agent/releases) 选择已发布附件。Windows 运行 `鲸伴安装.exe`，或完整解压 `鲸伴.zip` 后运行 `鲸伴.exe`；Android 一般选择 `鲸伴.apk`。
2. 点击桌宠打开聊天，在 **设置 → 模型与 API** 填入自己的 API 地址、模型名称和密钥。聊天与电脑任务共用这套配置。
3. 直接开始聊天。需要工具时切到 **电脑 Agent**；需要手机接续时，在电脑开启远程连接，再用手机完成配对。

安装包自带运行组件，不需要安装 Python 或 Node。当前源码版本为 **Windows 1.1.1 / Android 1.21.1**；源码和已发布附件可能不同步，下载时以 Release 页面为准。升级前保存草稿，从系统托盘完整退出旧程序。

**模型和权限：** 普通聊天需要可用的兼容 API；图片理解和电脑视觉操作需要模型支持图片输入，Agent 还需要工具调用能力。屏幕感知在新配置中默认开启，配置 API 后符合条件的画面会发送到你选择的服务商，可在设置关闭。电脑任务按当前 Windows 用户权限运行，操作目录不是系统沙箱；执行前可以选择「请求批准」「帮我批准」或「完全访问权限」。

## 项目如何工作

```text
前台画面与用户状态 → 本地变化筛选 → 多模态情境分析 → 主动交互决策
                                                         ↓
                                              角色反馈与对话存储

电脑 / 手机任务 → 鲸伴会话与权限适配 → DeepSeek Harness → 工具与电脑操作
```

桌面宿主使用 **Python + PySide6**，聊天界面使用 **React + TypeScript**，手机端使用 **Kotlin + Jetpack Compose**。会话和感知事件保存在 SQLite；已配对设备通过 iroh 加密连接同步。

鲸伴主要实现桌宠表现、情境筛选与主动反馈、聊天与角色库、跨端同步和任务交互。Agent 的任务循环、工具执行、上下文整理及会话恢复使用官方 **DeepSeek Harness SDK**，Windows 操作使用其 **CUA** 组件；来源和适配记录见 [第三方说明](third_party/deepseek_harness/README.md)。它不是本项目自行训练的大模型。

## 从源码运行

在 Windows x64 准备 Python 3.10 和 Node.js 24.20 或更新的 24.x 版本，在仓库根目录执行：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm --prefix frontend/chat-ui ci
npm --prefix frontend/chat-ui run build
.\.venv\Scripts\python.exe main.py
```

这会准备桌宠与聊天界面。需要电脑 Agent、手机连接或本地棋类时，再按 [开发与可选组件](docs/GETTING_STARTED.md#从源码准备可选组件) 补齐依赖；**不能只安装 Python 依赖就认为电脑任务已经可用**。完整打包流程见 [Windows 构建说明](docs/WINDOWS_RELEASE.md)。

## 继续了解

| 想了解 | 文档 |
| --- | --- |
| 初次配置、手机连接、常见问题 | [开始使用](docs/GETTING_STARTED.md) |
| 任务权限、工具、进度与文件交付 | [电脑 Agent](docs/ON_DEMAND_AGENT.md) |
| Android 安装、构建和同步 | [Android 说明](android/README.md) |
| 多模态观察和主动交互策略 | [屏幕感知设计](docs/PERCEPTION.md) |
| 实际测试范围、已知限制和历史失败 | [验证记录](docs/RELEASE_VALIDATION.md) |
| 旧版设计和更新过程 | [历史首页](docs/README_ARCHIVE.md) |

模型效果、电脑任务成功率和跨网络连接会受模型、系统状态与网络影响。已有自动化检查和受控任务记录不代表所有软件、电脑及实体手机都已验证。

本项目的课程题目为「基于多模态大模型与情境感知的主动交互式桌面智能体系统」。
