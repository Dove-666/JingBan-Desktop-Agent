# 状态反馈与记忆配置读取

排队任务在派发前取消或被配置变化阻止时，普通聊天重试路径本来就拒绝重复执行，但界面仍可能显示“重试回复”。现只对齐可用性判断，不修改执行、授权、排队或回执规则。原生与网页界面同时采用宿主返回的完整固定指导，避免将尚未派发的任务替换为“检查已完成操作”。错误栏仅保留独有且适用于重试的补充说明。

真实队列的两条 before 反例保留。可用性新增 10 项检查通过，首轮误多发现的 18 项旧测试单独记录；显示指导的新 7 项与旧 timeout 1 项分别通过。根审查又用真实 Controller、存档恢复、Queue/Coordinator 终态记录和编译界面做 16 项可见判断，确认取消/阻止任务无无效重试按钮、实际 retry 仍拒绝、宿主指导不被覆盖、错误栏无重复补充，且没有启动任务或调用模型。[可用性](../artifacts/user-testing-20261105/queue-retry-affordance/final-targeted.json) [显示指导](../artifacts/user-testing-20261105/queue-retry-guidance-ui/handoff.json) [根可见审查](../artifacts/user-testing-20261105/root-queue-retry-visible-review-v3/verification.json)

图片管理的忙碌提示也会区分可观察占用：显示仍有多少处待发图片，在操作处理中只说明仍需等待，占用解除后提醒重新确认。活动 token 数没有显示成任务数量，资源闭合未知仍保留原提示。它只读取已有 Gate 计数并更新失败说明，不清除附件、不重试删除、不放宽维护准入。新 9 项与既有 12 项检查分别通过。[占用上下文](../artifacts/user-testing-20261105/manager-busy-context/review.md)

记忆状态加载改为一次操作内复用一份记忆配置和每种所需密钥的存在/不可读状态。原逻辑每次读记忆配置 3 次，聊天密钥状态 2 次，远程 Embedding 模式还读其密钥 2 次；现在分别为 1 次，并去掉了仅为读取普通 INI 而构造的多余设置/凭据对象。主设置与 worker-local 配置读取复用同一个解析函数。不会跨操作缓存明文密钥，也不会省略关闭状态下用户需要查看的配置状态。

专项 14 项和相关旧回归 57 项通过。根独立另外检查在读取凭据时变更配置、移除密钥：同次返回的文字与事实保持一致，下次操作立即反映新状态，getter 在自有 worker 运行，返回结果不含凭据值，9 项判断通过。这是操作内采样一致性，不是不同配置源之间的原子事务，也不等于已经测出 Windows 凭据读取延迟或整个启动速度的提升。[实现与旧失败](../artifacts/user-testing-20261105/memory-readiness-snapshot/closeout.json) [独立变化检查](../artifacts/user-testing-20261105/root-memory-readiness-review/verification.json)

随后以最新状态显示、记忆读取、图片管理与编译界面组合再做一次实际应用完整窗口流程。20 项判断、19 个列出的生产源围栏均通过；辅助凭据只需注入原有两个服务对象，先前多余第三构造已不再出现。实际删除仅限自建测试库的一张图片，文字/引用保留、旧预览失效、同图重传恢复，最终窗口与 Qt/草稿工作线程结束。[最新完整应用检查](../artifacts/user-testing-20261105/root-image-manager-full-visible-v3/verification.json)

所有本页检查均为 0 真实模型调用，没有重启用户桌宠、操作用户图片或更改 Android 包。各组有重叠，不能相加为真实 Agent 任务成功率。原 Qt/夹具错误、旧显示反例、构建后 GBK 终端打印异常及各轮 before 均保留；单次前端构建自身正常结束。两个同一 Goal 内 GUI 无效果失败后恢复到文件任务的旧缺口仍未修复，未通过清除失败标记来绕过验收。[恢复边界](../artifacts/user-testing-20261105/task-audit/gui-no-effect-short-review.md)

## 保存确认与进度更新的后续验收

回复保存返回异常时，Controller 在原提交锁内读取一个存档快照，核对本次消息、回复内容、情绪、终态过程和会话代次。已保存的准确结果会立即显示并进入原队列结算。读取仍失败或结果不匹配时，界面保留待确认状态，用户成功重新读取后解除对应消息的重试保护。注入的故障发生在真实保存成功之后，18 项定向检查和 25 项相关检查分别通过。[保存恢复交付](../artifacts/user-testing-20261105/ack-recovery/closeout.json)

独立原生窗口分别走过普通聊天、已认证排队 Agent 和需要用户接手的结果。三段流程都显示准确回复，保留新的待发草稿，维持各自执行状态。根另用两个本地 SSE 请求检查文字、进度、停止、部分回复、草稿及下一条消息，16 项旅程判断通过。普通消息和进度各自到达 DOM，停止后连接关闭。[三段保存恢复](../artifacts/user-testing-20261105/root-ack-visible-review/readout.json) [文字与进度整合](../artifacts/user-testing-20261105/root-stream-execution-current/verification.json)

执行过程增加独立的增量通道。2000 条历史、40 段各 200 字上下文、20 次公开进度更新的同一组受控样本，信号内容按 UTF-8 计量，从 20 个完整状态的 29,464,263 字节降为一次完整状态加 19 个进度包，共 1,483,311 字节。整历史复制从 80 次降为 4 次，40 段上下文展开后逐字一致。200 条和 2000 条样本的草稿、正文和手动上翻位置均正确。测量逐次等待 DOM，耗时包含测试轮询和 Qt/WebEngine 调度。[实现与实测](../artifacts/user-testing-20261105/execution-incremental/closeout.json)

独立检查覆盖双通道交错、启动前乱序和 Controller 重入。发现的单通道兼容排序问题已修复并复测。正式构建完成一次，旧图片输入 20 项和图片代次 7 项检查通过。[独立协议验收](../artifacts/user-testing-20261105/execution-protocol-independent/delivery-final-review.json) [图片协议](../artifacts/user-testing-20261105/execution-incremental/root-image-protocol-related.json)

先前 42 项界面功能检查通过，但未记录实际显示平台，原生日志的工具输出也被截断。独立诊断使用同一组双窗口、重载和关闭流程，Windows 平台四次抓图均正常，offscreen 平台四次抓图均为纯白并复现图形上下文告警。随后只在自有测试进程中显式选择 Windows 平台，重新运行原 42 项检查，全部通过，完整原生日志没有出现这组图形告警。两次回归及原始诊断分别保留。[首次功能回归](../artifacts/user-testing-20261105/root-ui-compatibility-current/verification.json) [显示平台诊断](../artifacts/user-testing-20261105/renderer-lifecycle-diagnostic/summary.json) [Windows 原生回归](../artifacts/user-testing-20261105/root-ui-compatibility-native-current/verification.json)

持续无法自动读回的 Agent 保存也已补齐历史结算。保存异常路径保留本次准确回复和终态过程的摘要，新的显式历史读取被接受后，在后台核对原请求、任务、会话代次、存档内容、交付引用和准确关闭凭据，再结算原任务。27 项定向检查、43 项相关回归及联合冻结版本的三组独立事务交错分别通过。需要用户接手的结果保留原状态。候选仅存于当前宿主内存，关闭凭据被淘汰时保持保守结果。[实现验收](../artifacts/user-testing-20261105/ack-history-settlement/closeout.json) [独立事务验收](../artifacts/user-testing-20261105/ack-history-independent/verification.json)

手机导入旧时间消息与任务结算现在共用既有宿主锁。四组实际 RPC 交错覆盖等锁后正常导入，以及等待期间撤权、连接代次变化和电脑端关闭。28 项相关方法通过。[导入与结算顺序](../artifacts/user-testing-20261105/ack-history-readonly/import-lock-closeout.json)

## 已完成请求的内存释放

连续完成 20 轮普通聊天后切到空会话，原实现仍保留 20 个请求及其执行记录和密钥刷新闭包。现在在实际工作函数返回后的界面回调中释放已确认提交的同一请求，保存结果待确认的候选继续单独保留。相同受控流程的旧请求和原执行记录对象均从 20 个降为 0。这里量测请求对象及其公开上下文记录，进程整体内存仍包含当前界面和存档缓存。[原始量测](../artifacts/user-testing-20261105/request-retention-readonly/verification.json) [修复后量测](../artifacts/user-testing-20261105/completed-request-retention/measure-after/verification.json)

六项专项检查覆盖连续完成、保存 ACK 恢复、待确认保存、提交前失败、保存后历史读取失败，以及已保存但后台关闭仍待确认的情况。含这六项的相关组合共 44 项通过。早期四项的两个失败和测量脚本首次路径错误均保留。[专项](../artifacts/user-testing-20261105/completed-request-retention/after-initial.json) [组合回归](../artifacts/user-testing-20261105/completed-request-retention/related-current.json)

原模型任务失败、GUI 无效果恢复缺口及实体手机验收继续保留各自状态。

## 网页视图的销毁顺序

实际 Windows 窗口验收暴露了页面销毁前先释放 profile 的警告。新的构造顺序在 page 创建后才把 profile 交给同一 view 持有，使正常销毁按 page、profile 的顺序进行。Qt 要求自建 profile 覆盖页面的完整生命周期。[Qt 页面文档](https://doc.qt.io/qt-6.11/qwebenginepage.html#QWebEnginePage-1)

三组实际窗口回归覆盖自然销毁、回退重建、页面构造失败和后续初始化失败。正常 panel 和 view 的延迟销毁、回退销毁均观察到 page 先于 profile，相关警告从修复前四条降为零，草稿和窗口收起重开通过检查。其它 GPU 和 Chromium 诊断保留完整日志。[修复与验收](../artifacts/user-testing-20261105/web-profile-lifecycle/closeout.json)

## 完整视图的凭据采样

逐字流式增量已经保持零凭据读取。另一个热点发生在完整视图构造，一次包含 20 条执行记录的受控刷新，会在 Qt 线程读取聊天与视觉密钥各 20 次。现在网页与原生视图在本次有效记录首次脱敏时采样一批密钥，供这次刷新复用，合计从 40 次读取降为 2 次。空视图和无效记录维持零读取，下一次完整刷新重新采样。[原始计数](../artifacts/user-testing-20261105/task-audit/stream-gui-readonly/verification.json) [实现与验收](../artifacts/user-testing-20261105/view-redaction/closeout.json)

新增九项、既有兼容 34 项及最终组合的三项生命周期回归分别通过。独立三组检查还覆盖读取部分值后抛错、下次操作恢复、脱敏函数释放、无效记录及旧调用方式。失败采样使该次过程投影整体关闭，下一次刷新可恢复；操作结束后短命函数已释放，Controller 和 Panel 的属性集合没有增加。受控延迟样本包含自有注入延迟与 Windows 调度，实际系统凭据耗时仍需独立量测。[独立检查](../artifacts/user-testing-20261105/view-redaction-independent/verification-v2.json)

## 同时间请求的独立过程

真实 Store 的两次保存可以拥有相同的过程开始时间，同时保留不同的用户消息、回复关联、工具记录和上下文。旧网页和原生视图以开始时间作为全局分组键，会只显示后一条过程。现在先绑定本轮用户消息，再区分该请求的尝试时间。两条有效请求均可独立展开，用户与回复中的同次镜像仍合并显示。[有效数据复现](../artifacts/user-testing-20261105/same-time-trace-readonly/verification.json)

明确回复关联优先，旧记录真正缺少关联时保留相邻回复回退。明确无效关联投影为空值，避免重新进入旧版回退；当前视图先筛选本会话消息。原生与网页局部计算归属，原始存档保留。Android 已按用户消息关联展示过程，沿用现有实现。[实现与范围](../artifacts/user-testing-20261105/request-execution-identity/closeout.json)

新增 12 项 Native/DTO 方法和 10 组前端检查通过。原九组转换场景、2000 条消息连续 20 次更新的引用复用、执行及流式协议回归分别通过。本轮前端构建一次。真实 Store 的两次同时间保存，在 Windows 两种视图中均显示各自的工具和上下文，草稿保留。独立三组交错还覆盖乱序回复、无效关联、proactive、不同尝试及旧版缺过程记录，原始数据库和输入元数据逐字段保持。[独立宿主验收](../artifacts/user-testing-20261105/request-execution-identity-independent/host-verification.json) [实际 App 转换](../artifacts/user-testing-20261105/request-execution-identity-independent/app-verification.json)

根以最新编译界面重新走过两个本地 SSE 请求，文字与进度各自到达页面，停止、部分回复、草稿及下一条消息的 16 项旅程判断通过，窗口和工作线程结束。该流程使用本地固定响应。[最新流式整合](../artifacts/user-testing-20261105/root-stream-execution-identity-final/verification.json)

## 失败和停止后的请求释放

普通聊天失败六轮或停止三轮后，旧实现仍会保留已经退出的私有请求、过程对象和密钥刷新闭包。现在只在本会话的主任务与取消持久化工作均已返回、且没有待确认保存或历史结算候选时释放对应对象。公开消息、存档和可用的重试入口继续保留。17 项专项与 57 项相关回归分别通过，原对象的受控存活计数均降为零。[修复与量测](../artifacts/user-testing-20261105/inactive-request-retirement/closeout.json)

独立检查使用真实提交与 Gate 回调，先阻塞取消写入，再完成同会话的新回复及另一会话的回复。旧取消工作尚未返回时保留必要归属，真实回调结束后释放旧对象，公开重试仍只关联原用户消息。36 项判断通过，最终草稿工作者、任务、取消计数和 Gate 活动均已结束。[独立交错检查](../artifacts/user-testing-20261105/inactive-request-retirement-independent/closeout.json)

## 错误说明的重复显示

网页与原生界面仅在同一会话、同一消息的行内错误文字与当前错误栏完全一致时省去行内副本。错误栏消失后，历史消息的说明会恢复。不同原因、不同消息和重试权限各自保留。两种实际视图覆盖十种状态及阅读中更新，输入与选区保留，读到旧消息时仍可看到错误栏。[显示验收](../artifacts/user-testing-20261105/error-guidance-dedup/closeout.json)

首次从网页切换到兼容视图也能在第一次渲染时完成消重。新增首次切换场景连同原七项检查全部通过，构造过程的最大渲染深度保持为一。首轮新增测试误把正常顺序渲染次数写成二，实际为三，该失败记录与修正后的验收分开保存。[首次切换验收](../artifacts/user-testing-20261105/error-guidance-native-first-fallback/closeout.json)

随后在合并全部修改的同一冻结源码上，42 项 Windows 原生界面回归及两个本地 SSE 请求的停止流程分别通过。布局截图写入本轮目录，旧示例目录的现存文件哈希保持。实际查看了窄屏与桌面宽度的过程展开图。三组最新窗口日志各保留一条 Windows 网络枚举 10108 诊断，profile 生命周期、D3D 与 context lost 警告计数均为零。[当前完整回归](../artifacts/user-testing-20261105/root-ui-cleanup-final/verification.json) [当前流式流程](../artifacts/user-testing-20261105/root-stream-cleanup-final/verification.json)

## 回归截图的历史边界

旧布局测试把输出位置写在 1.9.0 示例目录中，后续回归重生成了其中五张 PNG 和一份布局指标。此次继续执行前的原始字节没有备份，这六个文件不能再作为原版本的独立截图证明。已记录现存文件及受影响路径，后续测试改用本轮专属输出位置，独立运行确认旧目录的现存文件哈希保持。[问题记录](../artifacts/user-testing-20261105/snapshot-output-audit/observation.json) [输出隔离验收](../artifacts/user-testing-20261105/snapshot-output-audit/verification.json)

## 历史安装包校验的计数范围

1.14.2 独立审查中的68份旧包与4份新包，覆盖当时全局校验清单的72个条目。后续实际目录枚举发现共76份安装包，其中既有1.11.0的四份包未列入该清单。新一轮已对实际76份全部取哈希围栏，旧报告按原清单范围保留。后续交付将单独补列遗漏版本的哈希，并保持原清单的字节前缀。[实际文件围栏](../artifacts/user-testing-20261105/android-r22-cleanup-ui-after/before.json)

## R23 的分派来源与同步升级

显式本地或手机 Agent 请求会跳过聊天模型分类器，旧过程标题却统一写成“模型公开回复”。现在依据现有直接请求标志显示“任务分派说明（直接请求）”，模型路由显示模型来源标题。Python 和 Android 的固定公开标签表同步接受该标题。[修复与全部失败沿革](../artifacts/user-testing-20261105/delegation-source-label/closeout.json)

同步派生结构从10升级到11，聊天存档的 user_version 为3。旧触发器在同一事务内刷新，已追平客户端会收到新的公开过程投影。真实旧v10夹具验证了完成记录补投影、旧快照失效、后续写入和重复初始化，并核验原存档、回执和墓碑。retention 可以按既有边界裁剪旧同步记录，测试同时核对存活原记录的字节。原文来源以原始存档为准，本次补发覆盖已存在的直接请求标签。[冻结迁移证据](../artifacts/user-testing-20261105/delegation-source-label/frozen-integrity.json)

R23 的107项宿主与界面组合、42项原生网页界面回归，以及两个本地 SSE 请求的流式停止流程分别通过。Android 实际 MainActivity 另完成认证原生通信、收尾期间排队、同一任务单次派发与回复、过程展开、暂时断开、重连和草稿保留。手机流程使用同代码的19号 debug 原型与有限工作进程替身，模型调用为零。[107项组合](../artifacts/user-testing-20261105/root-host-ui-r23/verification.json) [42项界面](../artifacts/user-testing-20261105/root-ui-r23/verification.json) [流式停止](../artifacts/user-testing-20261105/root-stream-r23/verification.json) [实际手机流程](../artifacts/user-testing-20261105/android-r23-agent-ui/ui-runs/ui-v3-20261106T212742-10b29e11/report.json)


## R23 手动断开与迟到配对回调

实际 ViewModel 回归复现了更外层的连接意图竞态。配对的 IO 工作已成功、界面回调尚在队列里时，用户手动断开会先关闭自动连接；旧配对回调随后仍可能把自动连接重新打开，前台同步循环就会再次连接。Repository 内的连接世代保护不足以覆盖这次界面偏好写入。

ViewModel 现在为每次显式配对、重连和断开保存最新意图，配对成功回调只在原意图仍有效时启用自动连接。公开 Application 构造入口保留，测试用内部构造入口注入真实 Repository 及内存假依赖。6 项实际 ViewModel 检查覆盖旧配对与断开竞态、正常配对、显式重连、新配对，以及失败配对下原自动连接选择的保留。最终全量 XML 为 240 项、34 套件，失败、错误和跳过均为零。

原始回归的目标失败、首次全量运行中的测试收尾超时、修正后的全量成功分别归档。收尾超时来自测试未继续处理 Main 队列，产品源码在两轮全量运行之间保持相同。首轮未发布 1.14.3 安装包单独保留，新包仍需重建与设备验收。见 [修复与回归记录](../artifacts/user-testing-20261105/android-viewmodel-connection-intent-fix/closeout.json)。


## 发布校验的当前状态与历史报告

APK 校验现在按版本持有操作系统文件锁，竞争调用在进入实际验证前明确退出。旧报告先按本轮 ID 原字节移交，再写入 running、passed 或 failed 状态，并用同目录原子替换发布完整 JSON。历史子目录故障仍保留原字节并生成失败状态；临时文件只有本次创建成功后才进入清理范围。若校验目录本身禁止首次移交，调用明确拒绝启动，旧报告保留且不输出本轮成功。

24 项回归及独立的 12 种临时文件状态探针通过，另保留旧源码的报告残留与临时文件误删复现。随后用新工具重新检查实际四份 1.14.3 APK，CLI 退出为零，状态 passed，240 项 Android 单元结果通过。四份 APK 字节、5 份已冻结发布记录不变，旧当前报告按原字节归档，本轮临时报告文件残留为零。见 [工具回归](../artifacts/user-testing-20261105/android-verifier-attempt-lifecycle/verification.json)和[当前四包复验](../artifacts/user-testing-20261105/android-verifier-attempt-lifecycle/post-delivery-real/verification.json)。
