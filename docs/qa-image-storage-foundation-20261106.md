# 图片管理基础层分阶段记录

本页保留基础层与第三阶段的历史范围。实际管理入口后来已接通并通过独立完整窗口验收，见[完成记录](qa-image-storage-manager-20261106.md)。以下早期阶段结果不能单独解释为全部图片删除已受保护；当时的界面原型只使用模拟记录。

第一阶段新增纯 Python 互斥模块，不做文件、网络、Qt 或权限操作。已登记的导入、暂存和任务活动阻止维护；正在运行的工作在真正结束前不会因关闭窗口、放弃等待或启动错误而被提前放行。导入结果到暂存的交接保持连续，旧回调无法释放后继操作。19 项针对性测试及独立的八轮线程交错检查通过，原报告辅助程序错误保留。

第二阶段仅增加三个查询接口，尚未接入删除：图片库总条数与逻辑占用查询、完整队列的未结束/未确认关闭检查，以及当前 AgentService 执行资源快照。查询失败或资源闭合未知不能当成空闲。图片与队列 37 项检查、Service 14 项检查分别通过；一次测试误把历史凭证数量写死的失败保留，修正的是测试判断。

独立组合夹具另做九项检查，使用自有 SQLite、模拟执行资源和模拟删除回调。分页外任务、未确认关闭、Service 占用及查询失败均阻止回调；放行后实际工作持有维护保护直到结束。它没有执行真实图片删除，也未证明任意外部进程已经退出。

第三阶段已完成 Controller 接纳和两种聊天面板的导入/暂存接线。Controller 新增 20 项检查通过；面板新增 27 项、旧会话兼容 7 项、相关回归 27 项分别通过。最初相关回归有一次 SDK 平台信息触发了被禁止的系统版本探测；最终仅在测试程序中固定该请求头信息，仍禁止 Python 创建进程。另独立复现并修复了面板构造后立即销毁时，草稿恢复定时器访问已销毁输入框的问题，原始失败保留。

接线期间还发现了既有的工具路由配置身份不一致：保存视觉模型密钥后，工具任务可能被错误取消。修复将完整的本机配置身份与执行时实际需要的密钥分开，不改变授权校验。新增 3 项及此前 54 项相关检查最终分别通过，原 53/54 的失败结果没有覆盖。

根审查另外打开实际编译聊天界面及原生兼容面板，使用自有 Controller、SQLite 和图片库，验证真实工作线程、图片暂存、隐藏重开、维护期间可编辑草稿及之后恢复聊天的 27 个判断。两次请求仅连接本地 SSE 模拟服务；移除暂存图后，实际 SDK 发送的请求为纯文字，普通聊天没有激活 Agent 工作状态。最终窗口、Qt 工作线程和自有草稿线程已关闭。该检查没有调用真实模型，也没有执行图片删除。

接线采用保守范围：维护期间暂拒新的聊天、Agent、入箱与导入提交，文字仍可编辑；历史同一图片删除前需要明确确认电脑副本、旧聊天及手机缓存的区别。第三阶段验收时，远程请求保护、实际删除、管理入口和缓存失效尚未完成；后续阶段的实现与证据单列在完成记录中。不能仅凭按钮禁用、空队列或界面停止状态放行。

- [纯互斥模块 19 项检查](../artifacts/user-testing-20261105/image-storage-gate-phase1/verification.json)
- [独立线程检查](../artifacts/user-testing-20261105/image-management-readonly-20261106/independent-gate-review.json)
- [图片与队列查询检查](../artifacts/user-testing-20261105/image-storage-queries-phase2a/verification.json)
- [Service 最终检查](../artifacts/user-testing-20261105/service-execution-resources-2b/after-v2.json)
- [独立组合检查](../artifacts/user-testing-20261105/image-management-readonly-20261106/independent-backend-composition.json)
- [界面原型与范围](../artifacts/user-testing-20261105/image-management-prototype/README.md)
- [独立设计审阅](../artifacts/user-testing-20261105/image-management-readonly-20261106/independent-design-review.md)
- [Controller 第三阶段](../artifacts/user-testing-20261105/image-storage-controller-phase3a/verification.json)
- [面板第三阶段与失败保留](../artifacts/user-testing-20261105/panel-image-gate-3b/review.md)
- [工具配置身份修复](../artifacts/user-testing-20261105/tools-gui-policy-identity-fix/verification.json)
- [根审查实际可见窗口](../artifacts/user-testing-20261105/root-image-gate-visible-review/verification.json)

以上各批都使用隔离数据，真实模型调用为 0，没有重启用户桌宠或操作真实手机。测试批次有重叠，不合并为真实任务成功率。当前 Android 包未因这些桌面基础接口重新构建。
