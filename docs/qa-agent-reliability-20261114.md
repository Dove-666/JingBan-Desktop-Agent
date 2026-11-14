# Agent 续办与失败收敛验证（2026-11-14）

本轮修复 11 月 8 日审计的 CORE-01–04。验证使用临时自有文件、真实 nanobot SDK、进程内模拟响应或本机 HTTP 模型替身；外部模型 API 调用 0 次，真实桌面输入 0 次。以下结果验证执行边界，不代表真实模型任务成功率。

## 历史问题与当前行为

| 问题 | 原审计观察 | 修复后验证 |
| --- | --- | --- |
| CORE-01：续办丢失原目标 | 原请求需要 `report.txt`，文件缺失时正确拒绝；同一历史下只发“继续”，验收检查要求变成 0 条，乐观模拟裁判使其误报完成 | 宿主单独保存完整用户目标及修订，规划与验收共用同一目标；“继续”仍检查缺失文件。明确取消旧文件或提出独立新任务时，不继续绑定旧产物 |
| CORE-02：错误工具请求不计失败 | 未提供工具名、缺少必填参数各重复 16 次仍发起第 17 次规划请求，失败活动为 0 | 工具准备阶段也记录失败、展示安全的失败活动；相同错误在第 4 次后停止 |
| CORE-03：交替失败绕过停止 | 在两个缺失文件间交替读取，16 次失败仍继续 | 保留最近 16 个失败标识；两个失败操作交替时，第 7 次停止。不同候选探索、成功恢复不套用总步数上限 |
| CORE-04：未通过验收却记作成功 | 缺失文件下，16 次 `finish_task` 拒绝都被标记为完成，继续第 17 次规划 | 拒绝验收显示失败，重复 4 次停止；根据真实进展计数，变换总结文案或重复读取不变文件不重置预算 |

原审计仅运行了有限循环并使用模拟裁判，没有实际云模型误判或无限时长运行证据。原始事实来自 `artifacts/user-audit-20261108/core/findings.json`；不把修复后的结果覆盖解释成原审计通过。

## 补充修复

- SDK 会把自动验收反馈写成 `role=user`。宿主目标记录只接受最初的实际请求及已登记的真实补充输入；自动反馈保留在执行回放中，不加入用户目标。检查点重启后仍保持此边界。
- 旧检查点只有在完整、可区分的用户记录存在时才迁移。已压缩的原目标、来源不明的后续裸 `user` 记录或超出目标记录预算的内容，不能被静默截断、猜测为新授权。
- 用稳定进展指纹单独判断失败重试；正式验收仍使用每次更新的完整证据标识。重复读取不会延长失败循环，也不会使旧验收凭据继续有效。实际文件修改、目标修改会改变进展。
- 长 Unicode 补充要求即使在执行回放里被截短，持久化目标仍保存完整宿主输入。
- 修复中发现验收返回语句被临时插入代码移到不可达位置，已恢复并通过真实 SDK 回归；没有将这个中间失败当作通过结果。

## 检查结果

| 检查组 | 结果 |
| --- | --- |
| 失败恢复、目标记录、完整验收契约、检查点 | 87 项通过 |
| 新增 SDK 续办/新任务/循环/真实文件修复 | 9 项通过 |
| 宿主与 SDK 检查点，包括自动反馈和重启 | 2 项通过 |
| 真实运行中补充指令，含长 Unicode | 2 项通过 |
| 进程生命周期 | 11 项通过 |
| 扩大宿主与工具回归 | 102 项通过，77.771 秒 |
| SDK 扩大回归 | 冻结源码及夹具后，同一组 76 项完整通过，167.170 秒 |
| 独立复核 | 18 项通过，覆盖稳定进展与自动反馈目标隔离 |

各行存在复用测试，不应相加作为独立用例总数。源码冻结后，剩余操作为验证与文档记录。

宿主回归首轮有 2 个失败：旧退出文案断言、手机遗漏“检查工具请求”标签，修复后 102 项完整重跑通过。SDK 首轮 76 项产生 14 条失败记录（包括同一用例的清理断言），不能计为通过；其中两项在 `HEAD:b6c57ef` 的独立核心副本中复现相同失败。旧假模型脚本不支持已有的自动验收反馈，另有默认权限、归档续办解析、统一 API 后辨认规划请求等过时假设。夹具仅允许明确的验收反馈请求有限追加原答案，未放宽真实文件、未知效果、重复启动和验收失败断言。相关失败逐项复核通过后，冻结源码及夹具，重新完整运行相同的 76 项，全部通过。日志为 `artifacts/reliability-fixes/core/sdk-frozen-verification.log`。

主要重跑命令（仓库根目录）：

```powershell
.venv/Scripts/python.exe -m unittest tests.test_task_harness_failure_recovery tests.test_agent_goal_history tests.test_goal_contract tests.test_agent_checkpoints -q
agent_backend/nanobot/.venv/Scripts/python.exe -m unittest discover -s agent_backend/nanobot/tests -p test_retry_and_resume_worker.py -q
.venv/Scripts/python.exe -m unittest tests.test_agent_checkpoint_sdk tests.test_agent_runtime_controls tests.test_worker_process_lifecycle -q
```

SDK 扩大组在 `agent_backend/nanobot/tests` 目录运行：

```powershell
../.venv/Scripts/python.exe -m unittest test_goal_revision_worker test_goal_contract_worker test_harness_worker test_gui_goal_worker test_tool_argument_recovery test_sdk_iterations test_agent_mode test_agent_completion_evidence test_file_effect_uncertainty test_agent_redaction_wire -q
```

## 限制

区分“继续原任务”和“独立新任务”仍依赖独立语义解析；此次用预设解析结果验证保留、替换、无效解析停止等边界，没有测真实服务上的理解准确率。未知执行结果不会因新目标而自动解除，也不自动重放旧操作。GUI 已执行后又收到修改目标的请求，仍可能需要先核对状态再继续。

失败窗口只识别重复或短周期失败；不断变化的新尝试不等于已证明有效进展。本轮保留有效任务的不限步数设置，没有声称解决所有无效探索。旧检查点无法还原完整目标时，需要新建对话重新说明要求。安装包、手机实机及真实电脑任务的验收由发布流程另行记录。
