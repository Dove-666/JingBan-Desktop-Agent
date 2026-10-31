# 持续体验改进

用户授权：持续从使用者角度改进，直到手动暂停；本阶段不修改角色建模和动画。

## 设计基线

沿用 PySide6 原生窗口和浅蓝配色。采用本机微软雅黑 UI / Segoe UI 字体；聊天正文优先可读性。
常用操作直接可达，预算等高级操作渐进展示。提供明确的等待、失败、空列表与恢复入口。
保留全部已有会话、图片与凭据，验证使用临时配置、合成屏幕和本地模拟服务。

## 已交付的 R8 批次

- 聊天：16 px 正文、可折叠历史、快捷键、按会话保留草稿及阅读位置；请求分阶段显示保存消息、检索记忆、生成回复、保存回复，切换会话不会冒充当前会话正在发送。
- 入口：聊天“工具”统一提供聊天服务设置、长期记忆、导出、预算和清空；两个聊天视图及托盘复用同一个记忆窗口。
- 导出：显式选择当前会话，异步导出完整 Markdown / JSON；SQLite 只读事务快照与同目录临时文件原子替换，取消不产生文件，失败保留已有目标。导出不读取配置、凭据、截图或 JSON metadata。
- 本地记忆检索：真实 Mem0 2.2.1 + FastEmbed / CPU ONNX；下载需明确点击，模型准备不会启用记忆或自动提炼。原远程 Embedding 模式保留。
- 记忆管理：查看、筛选、删除和清空走纯本地路径；删除共享实体链接不重算向量，不调用 LLM / Embedding，也不隐式下载 NLP 或 BM25 模型。
- 桌面：托盘聊天、控制台、记忆、显示/隐藏、暂停感知和退出；独立窗口恢复位置并限制在现有工作区，退出清理有异常兜底。
- 隐私：感知运行时暂停不改写授权；撤销本地观察或远程发送先立即生效，再尝试保存。恢复不能重新授予已撤销权限。
- 诊断：`python -m desktop_pet.diagnostics` 提供无网络、无模型加载的只读中文 / JSON 检查，缺少 PySide6 时也能报告必需依赖问题。

本轮不调整角色建模、动画参数或素材；角色 PNG 保持本轮开始时的文件内容。操作见 [日常使用](DAILY_USE.md)，诊断见 [诊断说明](DIAGNOSTICS.md)。

## 实际验证快照

| 批次或专项 | 实际结果与范围 |
| --- | --- |
| R8 日常功能完整回归 | **565 项通过，227.156 秒，零失败、零跳过**；[报告](../artifacts/r8_daily_unittest_report.json)。这是该次执行时的完整快照，不预先包含之后正在开发的功能。 |
| R8 第一批完整回归，历史快照 | 514 项通过，172.719 秒，零失败、零跳过；[第一批报告](../artifacts/r8_experience_unittest_report.json)。它不替代上面的后续完整结果。 |
| 后续定向复核 | 诊断 11 项通过；窗口 / 记忆相关 43 项通过。定向复核不与完整回归相加，也不冒充再次运行全部测试。 |
| 真实 Qt 体验 smoke | **44 项检查通过**；临时 INI / SQLite、内存凭据、合成会话、offscreen 实际渲染；[报告](../artifacts/r8_experience_smoke.json)。验证统一记忆入口、气泡定位、托盘动作逻辑、关闭重开、权限不升级和窗口可达性。 |
| 本地模型 | `BAAI/bge-small-zh-v1.5`，512 维，实际下载与 ONNX 验证 28.609 秒；缓存 9 文件，共 95,223,596 字节，约 90.8 MiB。离线界面检查缓存模型 3.047 秒。 |
| 真实 Mem0 + Qdrant + ONNX | 提炼写入、服务重建后语义检索、删除和清空通过；提炼 LLM 为 localhost fixture，Embedding 为真实本地 CPU 推理，未调用远程 Embedding；[首次报告](../artifacts/r8_local_embedding_smoke.json)、[无下载复核](../artifacts/r8_local_embedding_offline_smoke.json)。 |
| 记忆纯本地管理 | 9 项专项覆盖共享实体、其他 namespace、无关集合、损坏身份文件及离线 HTTP 阻断；合成记录，不读取生产记忆。 |
| 导出安全边界 | 9 项独立边界检查通过：反斜杠奇偶、标题/摘要/情境图片语法、时间/ID HTML、多行图片数据、缺事件坏记录、源数据库保护及写入失败清理。 |
| 诊断真实执行 | 普通中文及 JSON 命令退出码 0；Python `-S` 隐藏 site-packages 后正常报告缺依赖并退出 1，不因缺 Qt 崩溃；[无敏感内容报告](../artifacts/r8_diagnostics_report.json)。 |
| 本机 R8 启动 | 已实际启动，仅枚举自身进程窗口确认桌宠与 R8 控制台可见；不发送物理输入、不激活前台、不截图；[启动报告](../artifacts/r8_live_startup_report.json)。后续源码改动不会热加载到该实例。 |
| 本机空闲采样 | 桌宠与控制台可见时只读采样 49.181 秒，CPU 使用 1.1875 秒，单逻辑核口径 2.41%，16 线程整机口径约 0.15%；工作集 193.8 → 181.1 MB，10 线程；[报告](../artifacts/r8_live_idle_performance.json)。未发起聊天或模型操作，不是提炼性能或压力测试。 |

专项已包含在相应回归或单独复核中，不能把这些行相加作为唯一测试数。R7 的 429、454、463 项，以及 R2～R6 的结果仅为各自历史阶段证据。

截图只来自合成窗口，已逐张检查中文与布局：[聊天](../artifacts/r8_experience_chat.png)、[记忆设置](../artifacts/r8_experience_memory.png)、[API 设置](../artifacts/r8_experience_api.png)、[托盘菜单](../artifacts/r8_experience_tray_menu.png)。没有截取用户桌面。

## 本轮实际创建和修改的文件

以下为当前已交付 R8 批次的文件，后续进行中的批次另行补充。

新增功能与依赖文件：

```text
desktop_pet/tray.py
desktop_pet/ui/window_state.py
desktop_pet/conversations/export.py
desktop_pet/diagnostics.py
desktop_pet/memory/local_embedder.py
desktop_pet/memory/semantic_qdrant.py
requirements-memory-local.txt
```

修改功能文件：

```text
.gitignore
desktop_pet/__init__.py
desktop_pet/application_controller.py
desktop_pet/chat_controller.py
desktop_pet/memory_controller.py
desktop_pet/memory/settings.py
desktop_pet/memory/service.py
desktop_pet/memory/worker.py
desktop_pet/perception/controller.py
desktop_pet/ui/chat_panel.py
desktop_pet/ui/debug_panel.py
desktop_pet/ui/memory_panel.py
desktop_pet/ui/perception_panel.py
desktop_pet/ui/styles.py
```

新增测试与可复跑验证：

```text
tests/test_chat_experience.py
tests/test_chat_progress.py
tests/test_conversation_export.py
tests/test_export_edges.py
tests/test_desktop_controls.py
tests/test_window_state.py
tests/test_perception_pause_ui.py
tests/test_perception_permissions.py
tests/test_local_embeddings.py
tests/test_memory_setup_ui.py
tests/test_memory_offline_management.py
tests/test_diagnostics.py
tests/experience_smoke.py
tests/local_embedding_smoke.py
```

修改既有测试：`tests/test_chat_controller.py`、`tests/test_debug_ui.py`、`tests/test_memory_controller.py`、`tests/test_perception_ui.py`。新增说明为 `docs/DAILY_USE.md`、`docs/CONTINUOUS_IMPROVEMENT.md`、`docs/EXPERIENCE_REVIEW.md`、`docs/DIAGNOSTICS.md`；同步更新 `README.md` 和 `docs/SESSIONS_MEMORY.md`。运行报告和合成截图位于 Git 忽略的 `artifacts/`。

## 安装、启动和复核

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 可选：真实 Mem0 和 CPU 本地向量模型；包含基础 Mem0 依赖
.\.venv\Scripts\python.exe -m pip install -r requirements-memory-local.txt
.\.venv\Scripts\python.exe main.py --debug-panel

# 只读诊断；不会检查连接或启用记忆
.\.venv\Scripts\python.exe -m desktop_pet.diagnostics --json

.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m tests.experience_smoke
# 已下载模型时的真实本地 ONNX + Mem0 验证；LLM 仅为 localhost fixture
.\.venv\Scripts\python.exe -m tests.local_embedding_smoke
# 只有明确需要首次下载公开模型时才增加 --download
```

## 当前限制和下一批

- 本轮没有实测原生通知区注册/点击、物理鼠标、多显示器热插拔或混合 DPI。QAction、几何单测和 offscreen 渲染不能代替这些硬件验证。
- 本轮没有重新调用生产外部 LLM / 视觉 / Embedding 服务；localhost 固定回复不能证明外部模型质量、权限和计费行为。
- 普通历史与记忆数据未加密。导出是用户明确选择的本地副本，应自行保管；强制终止进程或断电不在正常临时文件清理保证内。
- 通常每个记忆目录只归一个稳定用户；如旧库混有其他 namespace，清空仅删除当前用户可归属的记录，其他向量、候选消息和无关集合保留。SDK 历史表没有 `user_id`，无法归属的旧孤立历史不会被猜测删除。剩余数据继续受 Embedding 身份保护，不能据“当前列表为空”强行切换模型。
- 记忆管理列表最多读取 1000 条；本地模型只适合短事实，中文模型输入窗口为 512 Token。提炼仍使用所选语言模型的独立预算。
- **下一批进行中：运行时临时暂停自动记忆的统一入口与状态同步。尚未列为已完成或纳入上述完整验收。** 现有持久化的“暂停自动提炼”仍可使用，二者应明确区分。

每批更新实际执行结果，未验证部分保持明确标注。
