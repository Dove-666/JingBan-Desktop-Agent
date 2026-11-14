# 鲸伴 Windows

当前源码版本为 **1.1.0**，Agent 已切换到官方 DeepSeek Harness。发布文件仍使用 `鲸伴安装.exe`、`鲸伴.zip` 和 `鲸伴.exe`，界面只在设置中显示版本号。最终安装包验收与交付状态以 [验证记录](RELEASE_VALIDATION.md) 中明确标注 **1.1.0** 的记录为准；下文的 1.0.2 和 Beta 验收不能用于证明新包通过。

全新配置默认浅色、角色大小 100%，电脑 Agent、任务工具与电脑操作开启。API 与模型由使用者填写，打包输入不包含个人设置、密钥、聊天、配对数据或 DSH 会话目录。新 API／新模型选择已识别支持的最高思考档位，手动保存的选择保留。手机安装包单独构建与验收。

Windows 包内置 Python、Node 和自包含的电脑操作组件，使用者无需安装开发环境。升级前保存草稿并从托盘退出旧程序；已有配置继续使用，卸载保留用户数据。Windows 包未做发行者代码签名；本机自动化检查不代表另一台电脑或实体手机跨网络兼容性已经验证。

## 历史封装记录

下文保留过去各版的结果与局限。1.0.2 配套 Android 1.19.1（27）；它的源码核对、安装卸载、ZIP 和隐私检查见 [1.0.2 验证记录](RELEASE_VALIDATION.md)。这些结果发生在 DSH 替换前，不计入 1.1.0 的通过项。历史依赖、引擎及工具链目录为 `artifacts/windows-build`，当前 DSH 构建使用独立的 `artifacts/dsh-windows-build`；验收脚本位于 `tools/qa_release`。

版本：`0.1.0-beta.4`（桌面 R29）。目标：Windows 10 2004 及更新版本的 x64 Windows，推荐 Windows 11。包内五子棋引擎采用上游 SSE4.1 基线版本，不要求 AVX2；未在其它物理电脑上验证全部硬件组合。

安装版运行 `鲸伴-0.1.0-beta.4-Windows-x64-Setup.exe`，无需管理员权限。免安装版须完整解压后运行 `鲸伴.exe`，保留同目录 `_internal`。两者都不要求预装 Python、Node 或开发环境。设置、聊天和记忆写入当前用户的本地配置目录；卸载不主动删除这些数据。安装包目前未做发行者代码签名。

Beta.4 包含模型思考参数适配，配套安卓 1.16.0；共享目录 `shared/reasoning/profiles.json` 同时进入主程序、独立 worker 与 Android Java 资源。构建与验收保存在 `artifacts/user-testing-20261107-model-reasoning`，发布目录为 `deliverables/windows/0.1.0-beta.4`；原包与历史失败记录保留。

上一版 Beta.2 包含 R27 独立角色同步、小屏棋盘，以及先前未进入 Beta.1 的角色库和合并后的「和鲸鱼娘玩耍」入口。与安卓 1.15.0 配合使用。更新前先保存草稿并从托盘退出旧版；重启 Beta.1 不能加载这些源码变更。

Beta.3 基于当前源码重新冻结并应用截图默认值，复核 exe 内全部 151 个项目代码对象及 1350 个分发输入。安装程序与 ZIP 位于 `deliverables/windows/0.1.0-beta.3`，构建和最终验收位于 `artifacts/user-testing-20261107-defaults-release`；旧版交付和失败记录保留。Android 源码没有变化，继续使用 1.15.0。

首次打开后，在设置中填写测试者自己的 API 地址、模型和密钥。本地象棋和五子棋不依赖聊天 API。新配置默认开启屏幕观察和远程发送，已有用户选择保留；摄像头、手机连接和自动记忆仍按需开启。本地 Embedding 模型需要在设置中显式初始化下载。没有随包附带完整 Office/LibreOffice；相关转换能力按实际环境显示。任意生成项目代码运行仍关闭，既有未通过的执行隔离门槛没有因封装而放开。

浏览器扩展源码位于包内 `browser-extension/whale-ask`。有需要的测试者可加载该扩展，再运行包内 `tools/install_browser_ask.ps1 -Apply` 配置当前用户连接；安装程序不会自动修改浏览器配置。已包含独立的 `鲸伴网页桥.exe`，此步骤无需 Python。

## 1.1.0 构建输入与流程

构建脚本为 `tools/build_windows_release.py`，PyInstaller 描述为 `tools/packaging/jingban.spec`，Inno 安装描述为 `tools/packaging/jingban.iss`。版本由 `desktop_pet/__init__.py` 中的 `APP_VERSION` 读取；更新版本时也须同步 `windows_version.txt` 的 PE 版本资源。

构建机需要 Windows x64、Python、npm 和 **.NET 10 SDK**。当前 Windows 运行时固定为官方 **Node.js 24.22.3**，由 `third_party/node/runtime.json` 固定 ZIP、可执行文件和许可证的 SHA256，存入项目自己的 `runtime/node`，不替换系统 Node。此前使用的 24.15.0 有 Windows libuv 退出竞态，不能用于本次交付；宿主和 runner 的兼容检查一致为 `^24.20.0 || >=26.7.0`，25.x 和早期 26.x 也不放行。修复来源见 [Node #61999](https://github.com/nodejs/node/pull/61999)。DSH 和 SDK 固定为 `0.2.2-rc.3`，Windows provider 使用仓库内 `0.2.2-rc.3-jingban.2` 源码。依赖以 `agent_backend/deepseek_harness/package-lock.json` 为准。

以下命令在仓库根目录的 PowerShell 中执行。各步退出码必须为 0 后才能继续。为避免混入旧依赖，使用干净的 `.venv`、`.venv-release` 和独立构建目录；不要把旧 Nanobot／UI-TARS 环境当作新 Agent 的输入。

1. 准备 Python 环境。`.venv` 是独立 Python 工作进程的依赖来源，`.venv-release` 用于冻结桌面程序。

   ```powershell
   py -3.10 -m venv .venv
   .venv/Scripts/python.exe -m pip install -r requirements.txt
   py -3.10 -m venv .venv-release
   .venv-release/Scripts/python.exe -m pip install -r requirements.txt -r requirements-build.txt
   ```

2. 先编译仓库内 Windows provider，再安装 DSH。原生构建脚本生成自包含的 `native/bin/win-x64` 和逐文件哈希 `BUILD.json`；仅运行 `dotnet publish` 而没有清单不能通过打包校验。上游版本、局部兼容修复和来源记录见 [Windows provider 说明](../third_party/deepseek_harness/cua-windows/INTEGRATION.md)。

   ```powershell
   .venv-release/Scripts/python.exe tools/packaging/node_runtime.py
   $env:PATH = (Join-Path (Get-Location) 'runtime/node') + ';' + $env:PATH
   $npmCli = Join-Path (Split-Path (Get-Command npm.cmd).Source) 'node_modules/npm/bin/npm-cli.js'
   runtime/node/node.exe $npmCli --prefix third_party/deepseek_harness/cua-windows install --ignore-scripts
   runtime/node/node.exe $npmCli --prefix third_party/deepseek_harness/cua-windows run build
   & ./third_party/deepseek_harness/cua-windows/build-native.ps1 -DotnetPath dotnet -Architecture x64
   runtime/node/node.exe $npmCli --prefix agent_backend/deepseek_harness ci
   runtime/node/node.exe agent_backend/deepseek_harness/worker.mjs --doctor
   ```

   Node 供给脚本在缓存缺失时下载官方固定 ZIP；已有缓存可传 `--archive <ZIP路径>`，仍须通过同一 SHA256 校验。打包只复制已验证的私有运行时及 `LICENSE`、`provenance.json`，不会复制 PATH 上的 Node。`npm ci` 必须在 provider 编译完成后执行，才能把本地文件依赖的最新产物装入 DSH。保留仓库 `.npmrc` 的 `install-links=true`；最终分发不使用指向开发目录的链接。`--doctor` 只证明 SDK 可加载，不能代替真实任务验收。

3. 构建聊天网页，再准备独立运行时、引擎和许可证。运行时选择器会排除旧 Agent 目录及已退役的宿主执行层；CPU 记忆依赖属于独立功能，不是 Agent 引擎。

   ```powershell
   runtime/node/node.exe $npmCli --prefix frontend/chat-ui ci
   runtime/node/node.exe $npmCli --prefix frontend/chat-ui run build
   .venv-release/Scripts/python.exe tools/packaging/worker_runtime.py artifacts/dsh-windows-build/worker_distribution --install-memory
   .venv-release/Scripts/python.exe tools/packaging/licenses_and_engines.py --staging artifacts/dsh-windows-build/engine_distribution --qt-notices
   ```

4. `tools/packaging/prepare_inno.ps1` 只下载并验证 Inno Setup 6.7.4，**不会安装编译器**。将验证过的编译器准备到 `artifacts/dsh-windows-build/tooling/inno/ISCC.exe`，将官方 `ChineseSimplified.isl` 放到同级 `tooling`，保留来源和 SHA256。引擎与完整 Qt/Chromium 版权材料也必须齐全。

5. 冻结当前源代码，生成候选；不要用 `--skip-freeze` 将旧主程序拼入新包。

   ```powershell
   .venv-release/Scripts/python.exe tools/build_windows_release.py --work-directory artifacts/dsh-windows-build --assemble-only
   $candidate = (Get-Content artifacts/dsh-windows-build/candidate.json -Raw | ConvertFrom-Json).payload
   $auditTag = 'dsh-1-1-0-' + (Get-Date -Format yyyyMMdd-HHmmss)
   .venv-release/Scripts/python.exe tools/qa_release/verify_all_inputs.py --payload $candidate --tag $auditTag --require-match
   ```

   构建会清理 PATH，并拒绝 Windows 系统自带的 unversioned ICU DLL。源码清单同时记录 DSH 的 mjs/yaml、锁文件、provider 源码和原生构建清单。第三方二进制、运行行为和安装器仍需另外验收。

6. 在项目目录外的中文空格路径，用全新 `--profile-dir` 运行候选，验证启动、聊天、草稿、棋局及独立的 DSH 任务。不得读取个人配置完成测试。候选内置运行时的独立检查可用：

   ```powershell
   runtime/node/node.exe tools/qa_release/dsh_candidate.mjs --payload 'C:/验收目录/鲸伴' --fixture artifacts/dsh-native-fixture-selfcontained/dsh-cua-desktop-fixture.exe --output artifacts/dsh-candidate-acceptance.json
   ```

   此路径必须是真正位于项目外的候选副本或安装目录；脚本会用包内 Node 和隔离配置重启验证。原生电脑操作还需处于解锁的交互桌面，使用专用测试窗口。源码测试通过不能代替此项。

   桌面正在使用时改传 `--skip-native-input`，仍检查真实 SDK、会话恢复、文件交付、只读电脑工具及 Office 转换，但报告会保留 `acceptance_complete=false`，不得将其记为原生键鼠验收。先用构建脚本的 `--installer-only` 和 `--portable-only` 生成待验收文件；安装卸载驱动 `tools/qa_release/accept_installer.py` 可使用 `--non-interactive --verify-dsh-runtime --verify-private-literals`，实际装入项目外目录，检查包内运行时和隐私后卸载，不抢占前台。

7. 候选核验、安装卸载检查完成，并明确记录原生操作测试的实际覆盖范围后发布：

   ```powershell
   .venv-release/Scripts/python.exe tools/build_windows_release.py --work-directory artifacts/dsh-windows-build --publish-only
   ```

   发布前逐文件重新核对候选，生成安装程序、免安装 ZIP、完整 CRC 检查和 `SHA256SUMS.txt`；已发布的同版本 ZIP 不自动覆盖。

## DSH 专项测试复现

下列 Python 命令覆盖宿主、队列、停止确认、会话恢复、文件交付、手机同步及实际 Qt/WebChannel 对话，不是全仓库测试总数。第一组为原有 176 项宿主/UI 回归，加上 12 项 Node 选择和校验供给用例，共 188 项；第二组另有 4 项实际官方 SDK 回归，使用本机合成模型服务，不消耗个人 API。各组结果单独记录，不将重复运行累加。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:QT_QPA_PLATFORM = 'offscreen'
$env:QTWEBENGINE_CHROMIUM_FLAGS = '--disable-gpu --no-sandbox'
$dshHostModules = @(
  'tests.test_dsh_host', 'tests.test_dsh_build_inventory',
  'tests.test_execution_attempt', 'tests.test_service_execution_resources',
  'tests.test_worker_process_lifecycle', 'tests.test_staged_agent_input',
  'tests.test_guardian_queue_binding', 'tests.test_agent_job_queue',
  'tests.test_direct_agent', 'tests.test_public_execution_sync',
  'tests.test_remote_agent_projection_upgrade', 'tests.test_agent_shutdown_completion',
  'tests.test_remote_agent_authority', 'tests.test_queued_agent_store',
  'tests.test_artifact_save_receipt', 'tests.test_agent_interaction_ui',
  'tests.test_node_runtime'
)
.venv/Scripts/python.exe -m unittest @dshHostModules -q
.venv/Scripts/python.exe -m unittest tests.test_dsh_host_sdk -q
```

新 Node 24.22.3 下的 188 项通过记录为 `artifacts/dsh-host-node24-final.log`（52.809 秒）；4 项实际 SDK 通过记录为 `artifacts/dsh-host-sdk-node24-final.log`（58.767 秒）。本轮共 192 项独立 Python 用例；均未操控用户前台。`artifacts/dsh-host-stop-regression-final.log`（176 项）和 `artifacts/dsh-host-sdk-stop-final-computer.log`（4 项）是换 Node 前的历史记录，不能代替新候选验收。更早一次 176 项运行有两个未初始化新字段的旧测试夹具报错，修正后的复跑通过；原失败日志保留。

Node 专项测试包含真实 DSH SDK、思考参数、图片、提问、文件、环境隔离和停止回执。要运行其中的原生鼠标停止用例，先构建专用窗口，再显式传入路径；测试只操控该窗口，会短暂移动鼠标，请在空闲且解锁的 Windows 桌面运行。

本轮最终非前台检查为 `artifacts/dsh-node48-final.log`：**48/48 通过、0 跳过**，包含 SDK 初始化失败自动更换 client 后的真实关闭证明。`artifacts/dsh-sdk-final-repeats.json` 记录同代码连续 10 次退出检查通过；这些重复不增加独立用例数。只运行后台部分的命令如下，明确排除原生输入用例：

```powershell
$backgroundTests = Get-ChildItem agent_backend/deepseek_harness/test/*.test.mjs | Where-Object Name -ne 'native-cancel.test.mjs' | ForEach-Object FullName
runtime/node/node.exe --test @backgroundTests
```

```powershell
dotnet publish third_party/deepseek_harness/cua-windows/native/desktop-tests/DesktopFixture.csproj --configuration Release --runtime win-x64 --self-contained true --output artifacts/dsh-native-fixture-selfcontained -p:EnableWindowsTargeting=true
$env:JINGBAN_DSH_NATIVE_FIXTURE = (Resolve-Path artifacts/dsh-native-fixture-selfcontained/dsh-cua-desktop-fixture.exe).Path
$env:JINGBAN_DSH_NATIVE_REPORT = Join-Path (Get-Location) 'artifacts/dsh-sdk-native-cancel-local.json'
runtime/node/node.exe $npmCli --prefix agent_backend/deepseek_harness test
```

此前完整 Node 记录 `artifacts/dsh-node-freeze-final.log` 为 **39/39 通过、0 跳过**。新增一项后的首次 40 项运行 `artifacts/dsh-node-frozen-40.log` 是 **39 通过、1 失败**：原生输入在测试观察到取消前已结束；它不是 40 项通过证据。以修正后明确记录的最终日志为准。未设置专用窗口路径、非 Windows 或缺少原生组件时会跳过相关用例，不能把这种运行写成完整原生验收。

仓库保留的 Nanobot、UI-TARS、旧目标验收器及对应测试属于历史实现，已退出当前执行与打包链路。它们通过或失败都不能证明当前 DSH 的效果；也不能为了“全绿”放宽新执行层的停止确认。普通聊天、记忆、感知、棋局、Android 等其余模块需各自的专项记录，本节没有声称全项目测试通过。

## 更早版本的构建与验收

Beta.1 的构建与验收保留在 `artifacts/windows_release_20261107`。Beta.2 使用独立驱动 `artifacts/user-testing-20261107-sync-background-games/build_windows.py`，在该目录的 `windows/` 复制已验证的依赖及引擎后重新冻结当前源码，交付文件位于 `deliverables/windows/0.1.0-beta.2`。Beta.2 验收还直接读取 exe 中 7 个 Python 模块，与当前源码编译结果比较，并核对网页及外部工作进程源码；原生封装检查使用项目目录外的全新配置，验证聊天、草稿、两种本地人机回合和网页桥。未重新安装到用户的正式应用目录；安装器构建和包体校验分别记录。本轮证据见 `artifacts/user-testing-20261107-sync-background-games/verification.json`。原失败、夹具修正和最终通过报告分别保留，不能将旧失败改写为通过。

## Beta.1 已执行的验证与边界

- 原生 exe 在项目目录外启动，实际 Qt WebEngine 页面、设置保存、localhost SDK 中文聊天、草稿关闭重开、两种棋局实际人机回合及浏览器桥一帧 JSON 协议已验证。
- 外置 Python/Node 在移除系统开发路径后运行；文档生成读回、Mem0/Qdrant/CPU 推理组件、Node SDK 与记忆 worker 协议已验证。
- 完整 Agent 受控用例通过实际注册的 `write_file`、`read_file`、`finish_task` 创建、读回文件并完成独立事实验收。模型响应由本机确定性测试服务提供；这证明封装链路可运行，不是实际模型能力或任意任务成功率。
- 程序在同一台 Windows 机器上以全新配置验证，不能据此声称已在另一台没有开发软件的真机上完成硬件兼容测试。摄像头、外网模型和手机跨网络连接仍需测试者在自己的设备与账户下验证。
- 包内 `PACKAGE-MANIFEST.json` 记录每个交付文件的大小与 SHA256。源码、依赖版本、Qt DLL 污染诊断和历史失败位于构建验收目录，用户聊天与真实凭据未加入交付。
