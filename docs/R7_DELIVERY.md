# R7 交付记录：会话、屏幕事件与 Mem0

版本：`2026.10.30-r7`。R7 首轮 **429 项测试通过、零跳过、零失败**；完整结果见 [首轮 R7 验证汇总](../artifacts/r7_validation_summary.json)。使用、数据库、记忆配置和手工验收步骤详见 [会话与长期记忆说明](SESSIONS_MEMORY.md)。本记录列出该批次功能涉及的文件、与用户 25 条验收的对应关系及实际验证边界。

后续的记忆排队设置复核、整体期限、检索凭据依赖、同配置单实例和原生验证隔离单独记录于 [R7 后续修复](R7_FOLLOWUP.md)。以下 429 项是先前批次，不作为后续代码已复跑的证明。

## 新增文件

| 文件 | 用途 |
| --- | --- |
| `desktop_pet/async_tasks.py` | Qt 后台任务与安全完成通知 |
| `desktop_pet/conversation_metadata.py` | 异步标题、短概况和增量长对话概况 |
| `desktop_pet/conversations/__init__.py` | 会话和上下文模块接口 |
| `desktop_pet/conversations/store.py` | SQLite 三表、事务、查询、约束和版本迁移 |
| `desktop_pet/conversations/context.py` | 有限上下文组装、隔离和 Token 估算 |
| `desktop_pet/memory/__init__.py` | 可替换的记忆模块接口 |
| `desktop_pet/memory/settings.py` | 非密钥记忆配置与范围校验 |
| `desktop_pet/memory/service.py` | 候选筛选、凭据、期限与 Mem0 服务封装 |
| `desktop_pet/memory/worker.py` | 真正的 Mem0 SDK、本地 Qdrant、删除/清空与提炼预算校验 |
| `desktop_pet/memory_controller.py` | 异步记忆管理和 UI 信号 |
| `desktop_pet/ui/memory_panel.py` | 长期记忆内容、搜索、删除、暂停与独立配置 |
| `requirements-memory.txt` | 固定可选 `mem0ai==2.2.1`、`qdrant-client==1.20.0` |
| `tests/test_conversation_store.py` | SQLite、迁移、事务、搜索、清空和删除 |
| `tests/test_context_budget.py` | 预算、摘要、事件/记忆限量和当前问题保护 |
| `tests/test_session_controller.py` | 会话生命周期、历史恢复、元数据及迟到响应归属 |
| `tests/test_persistent_screen_flow.py` | 模拟屏幕→正式消息→气泡→相关追问的完整 Qt/HTTP 链路 |
| `tests/test_memory_service.py` | 真实 Mem0 OSS、本地 Qdrant 与回环模型/Embedding |
| `tests/test_mem0_chat_integration.py` | 自动提炼、跨重启/跨会话检索、删除和失败降级 |
| `tests/test_memory_controller.py` | 后台管理、期限和聊天不受记忆故障影响 |
| `tests/test_session_ui.py` | 历史侧栏、事件详情、输入草稿、预算和记忆界面 |
| `docs/SESSIONS_MEMORY.md` | 配置、存储、数据关系、操作和验收说明 |
| `docs/R7_DELIVERY.md` | 本交付记录 |

## 修改现有文件

以下按本次 R7 的功能关联点列出。工作区还保留了此前阶段的未提交更改；已有感知算法、角色 PNG、R4～R6 气泡排版文件不会因为处于未提交状态而被误记为本版重新创建。

| 文件 | 本次关联变化 |
| --- | --- |
| `.gitignore` | 排除 SQLite、Mem0 数据及会话导出等本地数据 |
| `desktop_pet/__init__.py` | R7 修订标识 |
| `desktop_pet/app.py` | 应用修订号和启动生命周期入口 |
| `desktop_pet/api_client.py` | 接收统一组装的上下文与输出上限，保持兼容服务及安全错误处理 |
| `desktop_pet/settings_manager.py` | 上下文预算持久化、可区分账户的凭据接口 |
| `desktop_pet/chat_controller.py` | 持久会话、历史管理、屏幕消息落库、后台元数据、记忆调用和请求归属 |
| `desktop_pet/application_controller.py` | 共享会话/记忆生命周期、关联气泡打开指定会话 |
| `desktop_pet/perception/controller.py` | 绑定分析发起时的明确会话、事务保存后气泡、清空/删除取消旧分析 |
| `desktop_pet/pet_controller.py` | 统一回复接口传递会话/消息链接 |
| `desktop_pet/pet_window.py` | 与独立气泡位置/生命周期及角色点击聊天入口保持兼容 |
| `desktop_pet/bubble.py` | 仅关联评论接受点击并发出会话/消息定位信号 |
| `desktop_pet/ui/chat_panel.py` | 历史侧栏、搜索/重命名/删除、来源标记、情境详情、重试及记忆/预算入口 |
| `desktop_pet/ui/debug_panel.py` | 接入共享记忆控制器，R7 版本标识和持久会话提示 |
| `tests/mock_api.py` | 区分普通聊天与标题/概况辅助请求 |
| `tests/test_api_client.py` | 输出上限、兼容端点与请求约束回归 |
| `tests/test_chat_controller.py` | 完整归档与有限请求上下文的回归适配 |
| `tests/test_settings.py` | 上下文预算持久化与设置恢复 |
| `tests/test_pet_controller.py` | 回复气泡链接参数与原角色控制兼容 |
| `tests/test_commentary_flow.py` | 正式评论归档及截图/密钥不持久化回归 |
| `tests/test_perception_integration.py` | 屏幕事件归档与原感知流程回归 |
| `tests/panel_smoke.py` | 当前持久会话与共享界面的实际窗口 smoke |
| `tests/perception_smoke.py` | 屏幕评论归档语义及原观察/权限流程 smoke |
| `tests/windows_smoke.py` | 尝试 Windows 原生关联气泡点击，保留未完成证据 |
| `README.md` | 当前版本、依赖、会话/记忆入口和旧测试记录分界 |
| `docs/DEBUG_PANEL.md` | 当前聊天/记忆/预算操作及数据持久化规则 |
| `docs/PERCEPTION.md` | 归档屏幕评论、关联追问与截图不持久化边界 |

## 25 条验收与对应测试

这些测试在隔离目录和测试凭据下运行，不使用生产聊天记录或密钥。屏幕素材为合成图片，模型服务以 localhost 固定响应模拟。Mem0 标为真实 SDK 的测试实际创建并重开本地 Qdrant，不用内存列表冒充长期记忆。

| 验收项 | 对应自动测试与检查重点 |
| --- | --- |
| 1. 新建会话获得独立 ID | `test_session_controller.test_distinct_uuid_drafts_leave_no_empty_history`：UUID 独立，空草稿不堆积历史 |
| 2. 不同会话消息互不混淆 | `test_conversation_store.test_different_conversations_never_share_history`：A/B 原文隔离 |
| 3. 退出后聊天保留 | `test_conversation_store.test_sessions_and_messages_survive_new_store_instance`；`test_session_controller.test_shutdown_keeps_accepted_user_write_and_drops_cancelled_reply` |
| 4. 重启默认新会话 | `test_session_controller.test_restart_creates_new_draft_and_explicit_old_resume_keeps_history` |
| 5. 主动打开旧会话 | 同上；`test_session_ui.test_click_history_opens_correct_id_and_updates_title`，历史加载失败另有恢复与明确错误测试 |
| 6. 删除会话清理消息与事件 | `test_conversation_store.test_delete_cascades_messages_and_events_and_keeps_other_session`；`test_persistent_screen_flow.test_deleting_session_cancels_pending_analysis_without_resurrection` |
| 7. 自动标题与概况持久化 | `test_session_controller.test_title_generated_once_and_incremental_summary_cursor_is_persistent`；`test_conversation_store.test_summary_cursor_must_belong_to_same_conversation_and_persists` |
| 8. 模拟屏幕事件产生评论 | `test_persistent_screen_flow.test_proactive_comment_is_atomic_assistant_event_without_open_chat` |
| 9. 评论成为正式助手消息 | 同上；`test_conversation_store.test_screen_comment_atomically_creates_event_and_assistant`，核对 role/source/event/message 关系 |
| 10. 点击气泡打开对应会话 | `test_persistent_screen_flow.test_bubble_qt_click_opens_exact_conversation_and_followup_has_event_context`：Qt 点击信号、会话 ID 和消息定位；本轮原生物理鼠标验证未完成 |
| 11. 围绕事件继续追问 | 同上，发送追问并验证保存在原会话，关闭再开继续可见 |
| 12. 请求带相关屏幕摘要 | 同上，检查真实 SDK 发往回环 HTTP 的请求中含事件 ID 与情境摘要 |
| 13. 旧事件不自动复制到新会话 | `test_persistent_screen_flow.test_new_session_never_copies_old_events_and_restart_restores_only_on_request` |
| 14. 重启后能查看旧评论 | 同上，重开数据目录，明确打开旧会话才恢复评论与事件 |
| 15. 提炼并保存有价值记忆 | `test_memory_service.test_real_mem0_extraction_restart_relevance_delete_clear_and_local_management`；`test_mem0_chat_integration.test_real_auto_extraction_restart_cross_session_relevance_and_delete`：真正的 Mem0 `infer=True` |
| 16. 重启检索相关记忆 | 同上，关闭并重开服务/控制器后从本地 Qdrant 语义检索 |
| 17. 不注入大量无关记忆 | `test_session_controller.test_irrelevant_memory_is_omitted_and_retrieval_stays_small`；`test_context_budget.test_only_few_high_score_memories_are_included`；真实 Mem0 集成包含无关问题检索 |
| 18. 删除后不再检索 | 真实 Mem0 提炼/重启/删除测试检查 `delete` 与 `reset` 后结果及本地向量数 |
| 19. Mem0 不可用仍可聊天 | `test_mem0_chat_integration.test_actual_mem0_provider_failure_keeps_gui_live_and_commits_chat`；`test_session_controller.test_unavailable_memory_does_not_lose_or_block_chat` |
| 20. 不把完整聊天无差别写记忆 | `test_mem0_chat_integration.test_screen_comment_never_enters_memory_and_paused_user_fact_is_not_extracted`；`test_memory_service.test_candidate_filter_excludes_screen_assistant_chitchat_questions_secrets` |
| 21. 长聊天不每次携带完整历史 | `test_context_budget.test_long_history_does_not_carry_all_original_messages`；`test_session_controller.test_real_http_context_is_bounded_with_summary_and_output_reserve` |
| 22. 概况与近期消息共存 | `test_context_budget.test_summary_and_recent_messages_coexist_with_screen_event`，包含相关事件 |
| 23. 新会话不继承旧原文/概况 | `test_context_budget.test_new_session_has_no_raw_history_or_summary_from_old_session`；`test_session_controller.test_restart_creates_new_draft_and_explicit_old_resume_keeps_history` |
| 24. 超预算压缩或裁剪 | `test_context_budget.test_hard_budget_trims_optional_material_instead_of_current_question`、`test_oversized_current_question_raises_instead_of_truncating`；Mem0 另测 SDK 展开提示超额拒绝提炼请求 |
| 25. 切换后迟到回复不串会话 | `test_session_controller.test_late_reply_persists_original_conversation_without_overwriting_new_view`；`test_persistent_screen_flow.test_delayed_screen_analysis_keeps_explicit_original_conversation` |

表中的 `test_*.test_*` 采用“文件名.方法名”方便在文件中定位；逐模块运行使用以下 unittest 命令。

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py desktop_pet tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest tests.test_conversation_store tests.test_context_budget tests.test_session_controller tests.test_persistent_screen_flow tests.test_memory_service tests.test_mem0_chat_integration tests.test_memory_controller tests.test_session_ui -v
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe tests\panel_smoke.py
.\.venv\Scripts\python.exe tests\perception_smoke.py
```

## 当前执行记录

R7 首轮全套回归已完成，包含删除/清空取消旧屏幕分析、防止会话复活、历史切换失败恢复及 UI 并发防护。专项是该批全套的子集或独立 smoke，不累加重复测试数量。后续修复的执行记录见本文件开头的链接。

| 批次 | 已得到的实际结果 |
| --- | --- |
| Python 语法与依赖 | `compileall`、`pip check` 通过；汇总为 `artifacts/r7_validation_summary.json` |
| 最终全套自动化回归 | **429 项全部通过，158.250 秒，零跳过、零失败，退出码 0**；`artifacts/r7_unittest_report.json`，完整日志 `artifacts/r7_tests_final.txt` |
| 真实 Mem0 SDK 专项 | 18 项属于最终 429 项的子集；独立专项运行 127.102 秒、零跳过，提炼与 Embedding 为回环 HTTP |
| 最新屏幕评论与 Qt 气泡会话联动 | 8 项通过，5.300 秒；包含删除/清空取消旧分析、防止会话复活 |
| 调试面板实际 Qt 窗口 smoke | 29 项检查通过，`artifacts/panel_smoke_report.json` |
| 屏幕感知实际 Qt 窗口 smoke | 32 项检查通过，`artifacts/perception_smoke_report.json` |
| Windows 物理鼠标 smoke | 实际尝试，`SetCursorPos` 返回 `False`，未完成，不计通过 |
| Windows 原生采集 smoke | 实际尝试，无法把专用合成 probe 置于前台，未完成；未捕获用户桌面、无网络请求，不计通过 |
| 已配置真实聊天服务 | 本轮实际请求成功并获得非空文本，见 `artifacts/r7_external_chat_report.json`；未修改配置、聊天记录或记忆开关，不记录服务地址与密钥 |
| 实际 R7 生产应用启动 | 通过，UI Automation 选中聊天选项卡，确认新建/历史搜索入口，打开并关闭长期记忆窗口后桌宠仍显示；既有 `settings.ini` 哈希未变，未发起 API 请求或截图，应用保留运行；`artifacts/r7_live_startup_report.json` |
| Windows 系统凭据 | 临时独立凭据的保存、重新实例读取、删除核验及清理均通过，未更改生产账户；`artifacts/r7_vault_report.json` |
| 密钥/运行数据/原图检查 | 95 份文件的已知真实密钥精确扫描 0 命中，运行数据忽略规则通过，原鲸鱼 PNG 与 HEAD 相同；`artifacts/r7_security_report.json` |

R2～R6 的原生交互和外部 API 验证属于历史证据，不代替本轮 Windows 物理操作或外部 Embedding 验证。Qt 自动事件能证明气泡到正确会话的数据流，本轮未声称已通过 Windows 物理点击。真实外部聊天检查只验证已配置聊天服务；外部 Embedding 能力和 Mem0 提炼的语义质量仍未验证。

## 配置与运行边界

- 三类数据分离：聊天和会话概况使用 SQLite；长期记忆使用 Mem0；默认不持久化截图。具体 schema、外键、事务和 `PRAGMA user_version=2` 迁移见 [数据库说明](SESSIONS_MEMORY.md#本地路径数据库和迁移)。
- 每次启动是新上下文；已保存的旧会话仅在用户主动打开后恢复。Mem0 是跨会话传递经过筛选事实的途径，不自动混入其他会话的原文或完整概况。
- 被决策选为发言的屏幕评论及其事件先事务归档，成功后再显示关联气泡。如果新用户操作优先而抑制气泡，已归档消息仍在历史。未被选为发言的候选不会批量归档。
- Mem0 默认关闭，自动提炼也默认关闭。需要安装 `requirements-memory.txt`，配置真正可用的 Embedding 地址、模型、实际维度和独立系统凭据；缺失时普通聊天和历史仍可使用。模型服务质量、跨服务商差异和语义提炼准确率未由确定性回环测试证明。
- 聊天默认 8192 总 Token / 1024 输出预留、最多 16 条近期消息、3 条事件与 3 条相关记忆。没有 provider tokenizer 时按 UTF-8 字节和安全余量估算，当前完整问题不会静默截断；服务商实际计费可能不同。
- Mem0 2.2.1 原生提炼提示较大，提炼独立默认 65536 / 1024，可配置。较长候选或已有记忆仍可能超额并明确报错；拒绝保证未发提炼模型请求，但 SDK 在此之前可能已调用 Embedding 检索。记忆出错不回滚已经保存的聊天。
- 管理列表和本地文本搜索最多 1000 条；尚未实现超大记忆库分页。更换 Embedding 模型/地址/维度前需清空原记忆，避免向量空间混用。后续启动保护已按配置路径阻止重复实例；绕过入口直接调用服务时仍需避免多进程共享同一本地 Qdrant 路径。
- SQLite 和本地向量库不加密，无跨设备同步、多用户切换、截图档案或语音。API Key 只进 Windows 系统凭据，源代码和普通配置不含真实密钥；敏感内容仍应由用户自行判断是否发送到所配置服务。

## 快速使用入口

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-memory.txt
.\.venv\Scripts\python.exe main.py --debug-panel
```

“聊天测试”或角色右键“打开聊天窗口”进入当前会话；“历史”侧栏可查看旧会话。“长期记忆”打开配置与内容管理，“上下文预算”调整聊天预算。屏幕感知权限仍单独控制；保存后的关联评论气泡可直接打开相应会话。

跨重启记忆验证、完整历史与有限上下文对照的步骤见 [可执行验收步骤](SESSIONS_MEMORY.md#可执行验收步骤)。测试报告保存在被 Git 忽略的 `artifacts/`；个人会话库及 Mem0 数据位于当前用户 Qt 配置目录，不应加入 Git。
