# 鲸伴 Windows

当前版本 1.0.1。运行 `deliverables/windows/1.0.1/鲸伴安装.exe` 安装，或完整解压 `鲸伴.zip` 后启动 `鲸伴.exe`。程序已内置运行环境，不需要另装 Python 或 Node。产品界面仅在设置常规页底部显示版本，安装器不再显示额外信息页。

全新配置默认浅色、角色大小 100%，电脑 Agent、任务工具与电脑操作开启。API 与模型由使用者填写，没有携带开发者的设置、密钥、聊天或配对数据。新 API／新模型选择已识别支持的最高思考档位；手动保存的选择保留，未知模型不猜测参数。手机 1.19.0 采用相同默认策略，并保留角色卡、API 和对话同步。

本轮从当前源码重新冻结主程序、网页和工作进程，验收范围及此前失败保存在 [验证记录](RELEASE_VALIDATION.md)。升级前保存草稿，从托盘完整退出旧程序。已保存配置继续使用；卸载保留用户数据。Windows 包没有发行者代码签名，实体手机跨网与另一台电脑的硬件兼容性未在本轮验证。

## 历史封装记录

版本：`0.1.0-beta.4`（桌面 R29）。目标：Windows 10 2004 及更新版本的 x64 Windows，推荐 Windows 11。包内五子棋引擎采用上游 SSE4.1 基线版本，不要求 AVX2；未在其它物理电脑上验证全部硬件组合。

安装版运行 `鲸伴-0.1.0-beta.4-Windows-x64-Setup.exe`，无需管理员权限。免安装版须完整解压后运行 `鲸伴.exe`，保留同目录 `_internal`。两者都不要求预装 Python、Node 或开发环境。设置、聊天和记忆写入当前用户的本地配置目录；卸载不主动删除这些数据。安装包目前未做发行者代码签名。

Beta.4 包含模型思考参数适配，配套安卓 1.16.0；共享目录 `shared/reasoning/profiles.json` 同时进入主程序、独立 worker 与 Android Java 资源。构建与验收保存在 `artifacts/user-testing-20261107-model-reasoning`，发布目录为 `deliverables/windows/0.1.0-beta.4`；原包与历史失败记录保留。

上一版 Beta.2 包含 R27 独立角色同步、小屏棋盘，以及先前未进入 Beta.1 的角色库和合并后的「和鲸鱼娘玩耍」入口。与安卓 1.15.0 配合使用。更新前先保存草稿并从托盘退出旧版；重启 Beta.1 不能加载这些源码变更。

Beta.3 基于当前源码重新冻结并应用截图默认值，复核 exe 内全部 151 个项目代码对象及 1350 个分发输入。安装程序与 ZIP 位于 `deliverables/windows/0.1.0-beta.3`，构建和最终验收位于 `artifacts/user-testing-20261107-defaults-release`；旧版交付和失败记录保留。Android 源码没有变化，继续使用 1.15.0。

首次打开后，在设置中填写测试者自己的 API 地址、模型和密钥。本地象棋和五子棋不依赖聊天 API。新配置默认开启屏幕观察和远程发送，已有用户选择保留；摄像头、手机连接和自动记忆仍按需开启。本地 Embedding 模型需要在设置中显式初始化下载。没有随包附带完整 Office/LibreOffice；相关转换能力按实际环境显示。任意生成项目代码运行仍关闭，既有未通过的执行隔离门槛没有因封装而放开。

浏览器扩展源码位于包内 `browser-extension/whale-ask`。有需要的测试者可加载该扩展，再运行包内 `tools/install_browser_ask.ps1 -Apply` 配置当前用户连接；安装程序不会自动修改浏览器配置。已包含独立的 `鲸伴网页桥.exe`，此步骤无需 Python。

## 构建输入与流程

构建脚本为 `tools/build_windows_release.py`，PyInstaller 描述为 `tools/packaging/jingban.spec`，Inno 安装描述为 `tools/packaging/jingban.iss`。版本由 `desktop_pet/__init__.py` 中的 `APP_VERSION` 读取；更新版本时也须同步 `windows_version.txt` 的 PE 版本资源。

1. 使用独立 `.venv-release` 安装 `requirements.txt` 与 `requirements-build.txt`。
2. 按项目后端说明准备 nanobot 独立环境。`tools/packaging/worker_runtime.py <staging> --install-memory` 将固定的 CPython 标准库、后端包、Node 和 UI-TARS 模块复制到独立分发目录，再补全 CPU 记忆依赖。它不复制开发虚拟环境配置或用户数据。
3. 用 `tools/packaging/licenses_and_engines.py --staging <engine-staging> --qt-notices` 准备引擎、对应精确源码、许可证和完整 Qt/Chromium 版权材料。开发机的 AVX2 引擎不会被作为最低硬件基线直接分发。
4. `tools/packaging/prepare_inno.ps1` 下载并验证官方 Inno Setup 6.7.4 签名；将编译器安装到构建工作区 `tooling/inno`。中文翻译使用官方源仓库的 ChineseSimplified.isl，构建记录保留其 SHA256。
5. `tools/build_windows_release.py --assemble-only` 冻结并生成待验收候选。构建时清理 PATH，避免其它软件的 DLL 混入；明确不打包 Windows 自带的 unversioned ICU DLL。
6. 在项目目录外、中文空格路径、全新 `--profile-dir`、仅保留 Windows PATH 的条件下运行候选。`--profile-dir` 同时隔离配置、凭据范围和单实例，隐藏的 `--qa-exit-after-ms` 通过正常退出流程结束测试。
7. 验收完成后运行 `--publish-only`。发布前逐文件重新核验候选，生成安装程序、免安装 ZIP、完整 ZIP CRC 检查和 `SHA256SUMS.txt`。已发布的同版本 ZIP 不自动覆盖。

Beta.1 的构建与验收保留在 `artifacts/windows_release_20261107`。Beta.2 使用独立驱动 `artifacts/user-testing-20261107-sync-background-games/build_windows.py`，在该目录的 `windows/` 复制已验证的依赖及引擎后重新冻结当前源码，交付文件位于 `deliverables/windows/0.1.0-beta.2`。Beta.2 验收还直接读取 exe 中 7 个 Python 模块，与当前源码编译结果比较，并核对网页及外部工作进程源码；原生封装检查使用项目目录外的全新配置，验证聊天、草稿、两种本地人机回合和网页桥。未重新安装到用户的正式应用目录；安装器构建和包体校验分别记录。本轮证据见 `artifacts/user-testing-20261107-sync-background-games/verification.json`。原失败、夹具修正和最终通过报告分别保留，不能将旧失败改写为通过。

## Beta.1 已执行的验证与边界

- 原生 exe 在项目目录外启动，实际 Qt WebEngine 页面、设置保存、localhost SDK 中文聊天、草稿关闭重开、两种棋局实际人机回合及浏览器桥一帧 JSON 协议已验证。
- 外置 Python/Node 在移除系统开发路径后运行；文档生成读回、Mem0/Qdrant/CPU 推理组件、Node SDK 与记忆 worker 协议已验证。
- 完整 Agent 受控用例通过实际注册的 `write_file`、`read_file`、`finish_task` 创建、读回文件并完成独立事实验收。模型响应由本机确定性测试服务提供；这证明封装链路可运行，不是实际模型能力或任意任务成功率。
- 程序在同一台 Windows 机器上以全新配置验证，不能据此声称已在另一台没有开发软件的真机上完成硬件兼容测试。摄像头、外网模型和手机跨网络连接仍需测试者在自己的设备与账户下验证。
- 包内 `PACKAGE-MANIFEST.json` 记录每个交付文件的大小与 SHA256。源码、依赖版本、Qt DLL 污染诊断和历史失败位于构建验收目录，用户聊天与真实凭据未加入交付。
