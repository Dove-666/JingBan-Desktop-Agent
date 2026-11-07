# R7 后续修复：记忆排队控制、单实例与原生验证隔离

本轮延续 `2026.10.30-r7`，在已有独立会话、屏幕事件关联与 Mem0 集成上修复并发和启动边界。此前 R7 首轮的 **429 项通过**保留在 [交付记录](R7_DELIVERY.md)，属于先前批次；本轮结果单独记录，不将旧数字冒充重新验证。

## 修复的问题与实际行为

### 排队中的记忆任务遵守最新设置

同一本地 Qdrant 的操作串行执行。此前任务在进入队列时读取设置，等待期间用户关闭记忆或暂停自动提炼后，旧任务可能仍依据旧设置继续。现在任务取得存储锁后重新读取设置：关闭记忆会跳过等待中的检索与提炼，暂停自动提炼会跳过等待中的自动提炼。暂停自动提炼仍允许检索已保存记忆。

单次记忆操作的期限从受理时开始，包含等待本地存储、准备配置、启动隐藏子进程和子进程工作时间。等待不会另获一整段模型超时；设置缩短超时时按较早期限执行，设置延长超时也不会追溯延长已受理任务。超时停止后台进程并释放本地存储，普通聊天继续使用当前会话。已发送的远程请求能否在服务商端停止由服务商决定。

语义检索和“测试记忆检索”只要求有效的 Embedding 配置与凭据，不再因缺少提炼模型密钥而被拒绝。真正提炼仍要求模型与 Embedding 均配置有效。本地查看、删除、清空继续不依赖远程密钥；这些功能没有被改成虚假记忆或仅隐藏界面内容。

### 凭据状态与连接结果分开显示

此前 Windows 凭据库可用时，面板仍可能缺少实际密钥。现在会检查当前选择的提炼密钥（复用聊天或独立配置）及 Embedding 密钥，分别显示“已保存”“未保存”或“无法读取”，不回显密钥，也不把凭据库可用当作已经配置。

凭据摘要独立显示，不会被本地记忆刷新结果覆盖；删除密钥后从同一后台任务重新读取摘要。打开记忆开关仅表示功能已开启，不代表连接成功。“测试记忆检索”只验证真实 Embedding 检索及本地向量库，不验证模型提炼。本地查看记忆也明确标注未验证远程连接。

对已保存聊天服务的模型列表进行了一次只读检查，第一页两个模型中未发现 Embedding 候选，未向其他地址转发聊天密钥，也未修改 API 配置。没有发现候选不等于证明该服务的所有接口都不支持 Embedding。此机器当前没有完整的本地 Embedding 推理依赖和模型权重，因此真实记忆联调仍需独立服务配置或选择安装本地模型。

### 相同配置只运行一个桌宠

启动入口按规范化后的 `settings.ini` 路径建立 `QLockFile` 生命周期锁。锁取得成功后才创建桌宠、会话控制器、感知任务和记忆服务，避免重复进程同时管理同一数据目录。

- 再次运行普通启动命令会通知原实例打开聊天窗口，然后退出新启动进程。
- 再次运行带 `--debug-panel` 的命令会通知原实例打开调试面板。
- 原实例不能及时回应时显示明确错误，不继续启动第二个桌宠。原实例的参数、当前会话和已保存配置保持原有状态。
- 激活通信仅接受固定的聊天/调试命令，限制报文长度、连接数和等待时间。锁按进程生命周期释放；不会仅因锁文件较旧就删除仍在运行的实例锁。

不同配置文件拥有不同锁，供隔离测试使用；不要通过手工复制配置目录或直接绕过启动入口来让多个进程共享同一 Qdrant 路径。

### 跳过其他鲸伴实例的界面

屏幕观察器除按本进程 ID 排除自身外，还通过 Python/鲸伴进程宿主与鲸伴专属窗口标题的组合识别其他鲸伴实例的桌宠、调试、聊天和记忆窗口，在截图前跳过。仅因进程名为 Python 不会排除普通 Python 应用；普通程序仅出现类似文本也不会直接命中该组合规则。

这项补充可处理升级期间旧进程仍存活、不同配置的隔离实例等情况，减少聊天与记忆界面被桌宠再次观察。已有隐私过滤、未知窗口降级和截取目标前台区域的规则继续生效。这不是对任意第三方窗口的全面敏感信息识别。

### 原生 smoke 只操作受控测试窗口

Windows 原生验证为被测入口提供临时配置和内存凭据后端，验证过程不读取生产 API Key 或生产聊天库。根据启动进程及子进程关系定位测试窗口，鼠标输入和前台校验仅允许受控进程，避免按同名窗口误选用户实际运行的桌宠。

如果自动化环境拒绝移动鼠标或无法将合成测试窗口置于前台，报告写为 `incomplete`，保留阶段与原因，不将未完成步骤计为通过。此类环境调用失败本身不能证明桌宠存在物理点击或采集缺陷。原生屏幕测试只截取受控合成窗口，不以用户桌面代替测试素材。

## 本轮文件清单

| 文件 | 修改内容 |
| --- | --- |
| `desktop_pet/memory/service.py` | 排队后复核设置、统一操作期限、检索凭据依赖 |
| `desktop_pet/perception/screen_observer.py` | 跨实例鲸伴窗口过滤 |
| `desktop_pet/single_instance.py` | 新增按配置路径加锁与有限本地激活通信 |
| `desktop_pet/app.py` | 在创建应用控制器前取得单实例锁并处理再次启动 |
| `tests/test_memory_service.py` | 记忆排队、设置切换、期限与仅 Embedding 检索回归 |
| `tests/test_screen_observer.py` | 跨实例界面排除与普通 Python 窗口不过滤 |
| `tests/test_single_instance.py` | 单实例锁、激活、退出释放及非法通信测试 |
| `tests/test_perception_ui.py` | 等待真实定时事件完成，消除固定 70 毫秒等待造成的测试波动 |
| `desktop_pet/ui/memory_panel.py` | 明确连接测试和提炼预算的实际网络边界 |
| `desktop_pet/memory_controller.py` | 独立、持久的凭据状态信号；删除密钥后重新读取摘要 |
| `tests/test_memory_controller.py`、`tests/test_session_ui.py` | 凭据状态不被本地刷新覆盖、删除同步和界面范围提示回归 |
| `tests/windows_smoke.py` | 原生输入限定受控进程，环境不足时结构化记录 |
| `tests/native_perception_smoke.py` | 合成窗口前台/截图验证边界及报告 |
| `tests/isolated_entrypoint.py` | 新增原生 smoke 的临时配置与内存凭据启动入口 |
| `README.md`、`docs/SESSIONS_MEMORY.md`、`docs/R7_DELIVERY.md` | 链接本记录并区分先前验证批次 |
| `docs/R7_FOLLOWUP.md` | 本轮修复、复跑命令及测试边界 |

## 安装、启动和复跑

无需增加新的必装依赖。普通聊天仍不要求安装或启用 Mem0。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 需要长期记忆时安装
.\.venv\Scripts\python.exe -m pip install -r requirements-memory.txt

.\.venv\Scripts\python.exe main.py --debug-panel
# 桌宠已经运行时，再次执行这一行应打开原实例的聊天窗口
.\.venv\Scripts\python.exe main.py

.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest tests.test_memory_service tests.test_screen_observer tests.test_single_instance -v
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe tests\windows_smoke.py
.\.venv\Scripts\python.exe tests\native_perception_smoke.py
```

原生验证需要可交互的 Windows 桌面；先保存手头工作，避免在验证过程中手动切换到其他窗口。脚本遇到受控窗口之外的输入目标时停止相关操作，不会自动向用户应用发送测试点击。

会话、数据库迁移和跨重启记忆的完整操作见 [会话与长期记忆说明](SESSIONS_MEMORY.md)。本轮不改聊天 SQLite schema，不重建旧会话、不重新处理角色图片。

## 本轮验证与未验证范围

凭据状态修复后的最终完整回归 **463 项通过，135.691 秒，零失败、零跳过**。先前 454 项记录保留在 `artifacts/r7_followup_unittest_report.json`；更早 429 项、真实聊天服务调用和系统凭据验证属于 [R7 首轮证据](R7_DELIVERY.md#当前执行记录)，不与本轮累加。

| 检查 | 实际结果与报告 |
| --- | --- |
| Python 语法、依赖和空白检查 | `compileall`、`pip check`、`git diff --check` 通过 |
| 最新完整测试 | `artifacts/r7_credentials_unittest_report.json`；完整日志 `artifacts/r7_credentials_tests.txt` |
| 凭据控制器与界面专项 | 28 项通过；另有 4 项服务层测试验证密钥未保存、已保存、模式选择和不可读分支 |
| 单实例专项 | 13 项通过，包含真实进程间激活、死进程锁恢复、活锁保留及启动失败释放 |
| 记忆服务 | 最新全套包含 25 项服务测试；凭据修复前的独立 21 项已通过，覆盖排队后禁用/暂停、整体期限、退出取消及缺少提炼密钥时检索 |
| 观察器专项 | 25 项通过，包含跨实例排除、普通 Python 窗口不过滤、受控 HWND/PID 不匹配时不读取标题 |
| 面板与感知实际 Qt smoke | 分别 29、32 项检查通过；`artifacts/r7_followup_panel_smoke_report.json`、`artifacts/r7_followup_perception_smoke_report.json` |
| 真实启动及重开窗口 | 通过；桌宠、聊天和记忆管理可打开，关闭记忆页不退出桌宠，现有设置哈希不变；`artifacts/r7_followup_live_startup_report.json` |
| 实际重复启动 | 第二进程返回 0，打开原进程的聊天窗口，原进程继续持有同一配置锁；`artifacts/r7_followup_duplicate_start_report.json` |
| Windows 原生输入与截图 | 已尝试，均为 `incomplete`：`SetCursorPos` 成功，但 `SetForegroundWindow` 返回 0，受控测试窗口未获前台；未进入物理点击或截图验证，未读取生产凭据或捕获用户桌面 |
| 密钥和资源检查 | 99 份源文件/资源扫描，已知密钥精确匹配 0；当前 PNG 已在本轮代码修改之外更新，使用并保留现有文件，不回滚为 Git HEAD 图片 |

原生检查的 API 返回值和阶段记录在 `artifacts/windows_smoke.json`、`artifacts/native_perception_smoke.json`，不能将未完成记为通过。本轮没有重新编写屏幕识别算法，没有修改聊天数据库版本。完整汇总为 `artifacts/r7_followup_validation_summary.json`。

锁与本地通信分别使用 Qt 的 [QLockFile](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QLockFile.html) 和 [QLocalServer](https://doc.qt.io/qtforpython-6/PySide6/QtNetwork/QLocalServer.html)，新增测试使用安装版本的真实 API。

当前用户配置中的长期记忆保持关闭，外部 Embedding 尚未配置密钥。本轮未擅自启用记忆，也未发起真实外部记忆模型或 Embedding 请求；仅向已保存聊天服务查询模型目录，报告为 `artifacts/r7_embedding_capability_report.json`，未发现 Embedding 候选。凭据标签的首次真实 UI Automation 检查确认“聊天密钥已保存、Embedding 密钥未保存”，本地刷新后仍保留；随后复检的 UI Automation 交互不可用，因此不把该复检记为完整界面通过。确定性 localhost 模型与真实 Mem0 SDK 的测试可以证明调用、持久化、隔离和失败处理，不能证明外部服务的语义提炼质量。完整历史、Mem0 管理列表上限和 Token 估算等既有边界见 [当前限制](SESSIONS_MEMORY.md#主要模块与当前限制)。
