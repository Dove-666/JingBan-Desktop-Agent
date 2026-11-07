# 退出和断连期间的 Agent 完成状态

已保存成功回复的排队任务，可能在宿主退出时被错误地留为“中断”。本轮修复这一状态误报，未发现该复现中的回复或文件丢失，也没有重放已执行任务。

## 原始复现

自有任务通过实际 Controller、TaskRunner、Service 包装层、WorkerLifecycle、Queue、Coordinator 与 SQLite 完成。worker 主体和 Process/Tree 为自有替身，不启动真实进程或调用模型。`complete_request` 的真实事务提交后暂缓返回，此时回复已经保存、原用户记录完成、该次 worker 的关闭回执已验证；随后退出宿主，再允许 worker finally 收尾。

原实现保留一条回复和原输出，重开与远程同步也能找回；但 Queue 为 interrupted，没有 reply_id，重开仍如此。原因是完成证明复用了含宿主 `_closed` 的失效判断，把应用退出当成会话本身失效。[原始复现](../artifacts/user-testing-20261105/agent-closure-user-audit/verification-v3.json)和[归因](../artifacts/user-testing-20261105/agent-closure-user-audit/review.json)保持原记录。

## 本次修改

Coordinator 新增仅用于已保存完成结果的失效判断，缺省仍沿用原判断以兼容既有调用者。Controller 为这条路径保留清空、删除中的会话失效，去掉单纯宿主关闭；完成证明直接重读同一存档的会话代次。

新任务派发仍使用原有关闭、取消、设置、授权和租约判断。保存回复的角色、来源、reply_to、原用户 Agent 启动标记、当前代次、交付引用和该次 worker 关闭证明也保留。AST 对照确认只改变两个构造函数和 `_finish`，其它派发方法相同。它只结算已有成功结果，不继续运行关闭后的任务。

## 验证范围

新九项方法覆盖退出前后保存、Stop 先获胜或已太迟、缺失回复、清空/删除/代次变化、缺失关闭证明及判断异常。四种会话失效为其中一个方法的子场景，不能重复计数。加上既有 HostQueue 21 项与 WorkerLifecycle 8 项，该次定向运行共 38 项通过，模型、网络、真实进程和可见窗口操作均为零。[定向结果](../artifacts/user-testing-20261105/agent-closure-user-audit/after-focused-v2.json)及[源码记录](../artifacts/user-testing-20261105/agent-closure-user-audit/fix-closeout.json)保存原失败和最终范围。

根另重新执行原两段旅程：退出后的保存任务、重开后的任务均为 completed 且绑定原 reply_id；断连重连恢复原操作回执、同请求返回原任务，改载荷、旧连接和不同设备身份被拒绝。两次 worker 与自有文件写入各一次，实际消息与回执一致，Qt 回调到达后空闲。八项根判断通过，并独立核对 AST 差异。[根复测](../artifacts/user-testing-20261105/root-closure-after-review/verification.json)使用归档驱动再次执行，不冒充另一套独立编写的测试。

另由独立编写的三个实际 Coordinator/Store/Queue 组合检查错关联回复、原代次撤销、缺失/不同 worker 关闭证明，以及关闭后后续任务仍排队。全部通过；只有正确关闭证明才能结算原任务并绑定准确回复，新派发保持拒绝。[独立结果](../artifacts/user-testing-20261105/queue-completion-independent/verification-v2.json)及[边界说明](../artifacts/user-testing-20261105/queue-completion-independent/review.md)单列。首轮因误用不含 PySide6 的系统 Python 而零场景导入失败，原报告保留，最终使用项目环境。

初始只读驱动误断言成功 `retry()` 必须返回 true，以及在物理工作完成后过早检查 Qt 回调状态，原记录保留。修复测试首轮还把 recovery barrier 当作全部运行中任务屏障；最终改为实际提交第二任务并证明它不会执行，未放宽生产条件。

## 租约过期的后续修复

另用虚拟时间复现执行租约超过 30 秒后的 unknown / LEASE_EXPIRED 状态。随后为已经保存成功的同次调用补上历史结算。队列取得写事务后，重新检查原任务身份、已提交回执、会话代次、回复内容的关联、交付引用和实际关闭证明，再绑定准确 reply_id。后续派发继续检查当前权限与执行状态。[原过期观察](../artifacts/user-testing-20261105/agent-closure-user-audit/expired-lease-observation.json)和[历史结算设计](../artifacts/user-testing-20261105/agent-closure-user-audit/historical-design.json)

新增 12 项检查和一项旧负例通过，另有 14 项相关检查通过。根重新运行实际 Controller 的虚拟 31 秒场景，八项判断通过，原任务及回复各出现一次。根首轮把秒与毫秒期限直接比较，原失败保留，修正测量单位后的报告单列。[定向结果](../artifacts/user-testing-20261105/agent-closure-user-audit/after-historical-v2.json) [相关结果](../artifacts/user-testing-20261105/agent-closure-user-audit/after-historical-related-v2.json) [根复测](../artifacts/user-testing-20261105/root-expired-completion-review/verification-v2.json)

独立三个交错场景在真实队列事务的核验入口撤下关闭证明、取消原用户记录或撤销配对权限。前两种情况拒绝历史完成，第三种情况允许已保存结果记账，同时拦截撤权设备的回执恢复和后续执行。五个列出的源码散列稳定，使用自有 SQLite 和模拟关闭凭据。[独立审阅](../artifacts/user-testing-20261105/expired-completion-independent/review.md)

本轮固定 worker 与 transport 检查宿主编排、保存、身份和状态处理，不证明模型规划质量、原生进程关闭、实体手机或公网中继可靠性。真实模型额度已用尽，原先失败的模型任务没有改写为通过。

## 本地超时后的排队与收尾

另一条实际控制链复现显示，本地 Agent 超时后准确关闭凭据仍缺失，手机状态却报告空闲。新登记的手机任务随后被队列取出，又被 Service 的忙碌保护拒绝，最终丢失了继续排队的机会。原执行层始终挡住重叠效果。修复前 25 项受控判断保留。[原始问题](../artifacts/user-testing-20261105/task-audit/timeout-cleanup-readonly/verification-v5.json)

现在只在空闲队列准备领取任务前检查 Service 的资源状态，并执行一次零等待的关闭核验。凭据未就绪时保留原排队消息，现有五秒定时器继续检查收尾。手机忙碌状态同时覆盖待收尾资源，运行会话为空时保持空值，配置能力的可用状态继续独立表达。取得准确关闭证明后，原任务才进入实际派发。电脑与 Android 的超时文字同步说明本次等待已结束。[实现与受控验收](../artifacts/user-testing-20261105/task-audit/timeout-cleanup-readonly/closeout.json)

六项新增检查、23 项既有队列检查，以及实际 Controller、Service、Remote RPC、同步和 SQLite 的 38 项旅程判断分别通过。后一组在原任务关闭未知期间四次检查仍保持同一任务排队，关闭证明就绪后该任务通过原派发保护执行一次，保存一条准确关联的回复。恢复任务主体仍为自有固定 worker。

Android 1.14.2 使用原签名构建，223 项单元检查与 Release Lint 通过。四份 APK 的 ABI、16 KiB 对齐、版本和新文字均核对通过。专用模拟器覆盖升级保留数据库、配置和草稿文件，实际正式包的新进程也能读回原草稿。[安装包验收](../artifacts/user-testing-20261105/android-1.14.2/verification.json)

## 本地新发送的忙碌检查

本地 Agent 超时且后台资源仍待收尾时，原来的新发送会先被接受并清空输入，随后才得到忙碌错误。现在显式 Agent 新发送在读取配置和创建用户消息之前检查现有 Service 的资源状态。拒绝后返回未接受结果，文字、共享草稿和同一待发图片都留在输入区，普通聊天仍可继续。[实现与验收](../artifacts/user-testing-20261105/local-agent-pending-admission/closeout.json)

新增 11 项与相关 60 项方法分别通过，实际 Controller、Service、隐藏原生输入框和手机队列的 44 项受控判断也通过。该检查覆盖新发送入口，独立的历史重试入口沿用既有规则。资源查询只是准入前的快照，真正执行仍由 Service 的执行屏障决定。

根审查继续使用真实编译页面、WebChannel 和自有 Store、Service 完成 21 项可见判断。点击拒绝后保留原文、选区、草稿代次及同一图片，待收尾期间普通聊天可带该图完成一次回复，准确关闭证明就绪后才接受一次新的 Agent 请求。工作进程主体与回复来自固定自有夹具，所有窗口和工作者均结束。[实际页面验收](../artifacts/user-testing-20261105/root-local-admission-visible/verification.json)

独立宿主验收另覆盖 44 项判断及三组边界。资源快照为可用、随即变忙的交错仍由最终执行锁拒绝启动，允许新消息被接受后记录失败，因此本次提前检查没有被描述成执行预约。[独立宿主流程](../artifacts/user-testing-20261105/local-admission-independent/host-after.json) [独立边界](../artifacts/user-testing-20261105/local-admission-independent/boundaries.json)

## 安卓实际界面的受理提示

真实 MainActivity 经原生 iroh 连接自有宿主后，复现了手机将已受理排队任务显示为“电脑任务执行中”的问题。宿主原消息为 queued，兼容同步投影为 pending，手机没有保留更细的实时队列阶段。现在该处仅显示“电脑已接收任务…”，实际执行过程继续由过程卡展示。手机本地尚未发出的消息沿用原状态。[修复前宿主事实](../artifacts/user-testing-20261105/android-r22-cleanup-ui/queued-ui-before-fix-host.json) [单字符串修复与原有13项回归](../artifacts/user-testing-20261105/android-r22-cleanup-ui/queue-feedback-change/closeout.json)

修复后的实际 debug 界面确认受理提示和未发送草稿，宿主在准确关闭凭据就绪前保留原任务。释放自有关闭条件后，同一任务只派发一次、保存一条回复并记录准确关闭，24 项宿主断言通过。Android 的103个源文件围栏在本次旅程前后保持。工作进程主体是有限内存替身，原生认证通信、Controller、Service、SQLite 和队列为实际实现。[实际受理与草稿](../artifacts/user-testing-20261105/android-r22-cleanup-ui-after/root-release-witness.json) [宿主末态](../artifacts/user-testing-20261105/android-r22-cleanup-ui-after/host-report.json)

本次 UI 脚本展开长过程后只检查可见节点，工具记录位于键盘上方视口的下方，产生了超时。原失败报告保留，根在实时聊天区域内滚动后确认“读取文件”实际可见，草稿与单次派发保持。完整过程与重连组合留待改进后的脚本重新验收。[原UI失败](../artifacts/user-testing-20261105/android-r22-cleanup-ui-after/ui-runs/ui-v2-20261106T202021-71663470/report.json) [实际滚动观察](../artifacts/user-testing-20261105/android-r22-cleanup-ui-after/root-scroll-observation.json)

两次设备流程均保留原调试签名、实际 UID 和首次安装时间。原42份 QA 文件恢复，正式包7份文件哈希保持，专用模拟器随后关闭。首轮错误调试签名导致的安装拒绝和旧 UID 字段读取缺口已单独记录；后续使用项目原有 ANDROID_USER_HOME，并以设备实际文件 UID 校验。系统 Keystore 整体字节和实体手机未纳入此次围栏。[设备恢复](../artifacts/user-testing-20261105/android-r22-cleanup-ui-after/debug-baseline-restored.json) [原安装失败与纠正](../artifacts/user-testing-20261105/android-r22-cleanup-ui/install-recovery-plan.json)

## 连接中的手动取消

真实 Repository 复现显示，重连持有串行锁等待网络时，手动断开也在等同一把锁，关闭操作无法开始。现在电脑连接使用独立子任务和私有意图归属。手动断开取消准确的旧连接任务，过期排队请求退出，较新连接由自己的归属保护。回归覆盖了手机流式回复、草稿和原消息投递身份。[原始复现](../artifacts/user-testing-20261105/android-manual-disconnect-readonly/verification.json) [修复与233项单元回归](../artifacts/user-testing-20261105/android-manual-disconnect-fix/closeout.json)

连接设置面板根据状态显示动作，连接中提供“取消连接”，已经连接时显示“暂时断开”。40组可用性组合和16项相关单元检查通过，新增界面回归已编译，阻塞真实连接的取消验收单独进行。[入口验收](../artifacts/user-testing-20261105/android-first-pair-cancel-ui/closeout.json)

R23 的完整实际手机旅程已经补跑通过，旧脚本的可见区超时保留。改进脚本在当前聊天区域内有界滚动查找真实工具与回复，最终在断开重连后仍显示同一回复和完整草稿，宿主只登记、派发和保存各一次。设备原42份 QA 文件及正式包7份文件哈希恢复。[完整旅程](../artifacts/user-testing-20261105/android-r23-agent-ui/ui-runs/ui-v3-20261106T212742-10b29e11/report.json) [本轮恢复](../artifacts/user-testing-20261105/android-r23-agent-ui/debug-baseline-restored.json)


## R23 首次配对取消的实际界面检查

正常 MainActivity 在首次配对响应尚未返回时，仍可从连接设置点击“取消连接”。本轮用实际 iroh 配对与正常 App 界面验证，点击前原连接年龄为 8.047 秒，物理连接在建立后 8.203 秒退出，早于排除自然闲置关闭的 13 秒门槛。随后主动放行服务端迟到响应，手机保持未配对界面，累计连接次数保持一次。38 项界面检查与主机收尾检查通过，最后恢复 Debug 的 42 份原有 QA 文件，正式包 7 份原文件哈希保持。

原始测试在点击前因重复读取同一界面超出保守时限，未发生取消点击，失败报告完整保留。a2 复用刚观察到的 CONNECTING 界面，保持即时前台、原连接存活、点击前小于 10 秒和退出前小于 13 秒检查。服务端已创建的配对授权仍存在，本轮证明取消客户端连接及迟到响应释放后的稳定状态。此项使用专用模拟器和 1.14.2-debug/code19 原型，新正式 1.14.3 的 Android 功能源相同，仅调整版本字段。

证据见 [a2 界面报告](../artifacts/user-testing-20261105/android-first-pair-cancel-native-a2/ui-runs/pair-cancel-20261106T215003-91fbf2df/report.json)和[主机收尾报告](../artifacts/user-testing-20261105/android-first-pair-cancel-native-a2/host-report.json)。实体手机与公共中继仍待各自环境验收。


## 只读磁盘盘点的分类恢复

旧入口按顺序共享 30,000 条元数据或 6 秒的扫描预算。Temp 中存在超过 30,000 项时，后面的下载目录会保持未扫描；重复同一次默认调用也会再次先耗在 Temp。目录摘要的固定条数上限也不足以定位较后面的巨大文件。此项由假磁盘元数据复现，原预算本身保留。

现有 inspect_disk_usage 新增可选固定 category，默认 all。Agent 可以选择 downloads 等既有类别单独盘点，返回 used_category 与 available_categories，继续受原路径、固定本地盘、重解析点及扫描预算限制。扫描结果保留逻辑体积下界和未知可回收空间的含义。

4 项新增回归经历原源码失败与修复后通过，50 项相关检查通过，2 项涉及真实适配器或原生链接的用例明确排除。真实 SDK 注册与参数校验已调用新入口和原扫描器。1 项兼容测试中的 4 种默认场景与精确旧模块比较，除新增两项范围字段外全部旧 JSON 字段相同。此轮未盘点用户真实 C 盘，也未调用模型。当前 worker ScopedTool、账本与进度链另做集成复核。见 [分类恢复记录](../artifacts/user-testing-20261105/task-audit/windows-disk-category-recovery/closeout.json)。


分类恢复后，另用当前 worker 的原始 ScopedTool 与原始注册语句，配合实际 SDK、Ledger、Harness、GoalTimeline 和 WindowsSystem 完成完整工具链补证。默认 30,000 项扫描保留 partial；随后指定 downloads，得到 501 项及 12 GiB 加 50,000 字节的完整类别结果。两次账本均 confirmed、domain 为 none，GUI 义务为 false、effect_count 为零。Goal verdict 保持空值，此项只验工具链。74 项代码依赖哈希前后相同，线程、loop、task 和自有输出残留均为零。原首次 QA 进度观察器参数冲突已独立保留，修正仅限观察器。见 [worker 入口补证](../artifacts/user-testing-20261105/task-audit/windows-category-worker-entry/closeout.json)。
