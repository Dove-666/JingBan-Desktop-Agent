# 长对话与电脑输入边界的只读检查

本次没有因为“可能慢”或控件名称而修改生产行为。检查使用独立配置、数据和替身工具，未调用模型。

## 长对话草稿

实际 Controller、自有 SQLite/INI 与当前编译聊天界面分别加载 200、2000 条纯文字记录，每条 200 字。各输入 20 次，后十次保持手动上翻状态。两组输入期间完整状态构建、历史复制、完整状态信号与历史传输字节均为零；最终草稿一致，消息节点身份保留，消息区无 DOM 变更，上翻位置保持不变。因此未新增虚拟列表或性能补丁。

这只覆盖稳态的纯文字草稿编辑。消息节点不变不能证明 React 内部完全没有遍历；合成输入、两帧等待和短时 Qt 心跳也不是物理键盘延迟或整机流畅度基准。原报告 `dom_ready_ms` 字段错误地包含后续输入阶段，独立小结已指出，原报告未覆盖。

[原始结果](../artifacts/user-testing-20261105/long-history-draft-ui/verification.json)与[范围说明](../artifacts/user-testing-20261105/long-history-draft-ui/review.json)包含完整计数和耗时限制。

## 电脑输入权限

检查确认普通工具、Windows UIA 和像素电脑操作的注册条件不同。仅启用普通工具不自动获得 UIA；UIA 仍要求电脑操作设置，像素路径另需 GUI 配置。生成代码执行门属于另一条入口，不等于对所有 GUI 目标应用的用途分类。

七个自有控件 descriptor 进入实际 `DesktopControls.observe/act` 检查函数，输入值为固定普通标记。普通 Edit、名称类似终端、Run 或地址框的 Edit 到达 fake act 哨兵；密码、禁用和身份已变化的控件分别提前拒绝。fake act 没有改值，真实 UIA backend、输入、窗口、进程、模型与网络操作均为零。

这些结果描述宿主实际检查的范围，不能证明真实终端或地址框提供相同 pattern、命令可以执行或存在隔离逃逸。因此没有增加按名称判断的黑名单，也没有改变用户的电脑操作授权。R19 生成代码隔离缺口仍按原记录保留，未重新执行探针或开启相关功能。

首个测试驱动被 Windows Proactor 内部 socketpair 的禁止网络守卫阻止，原驱动和错误记录保留。修正驱动只同步推进保证立即返回的 fake coroutine；不据此宣称事件循环或并发已验证。

[七项结果](../artifacts/user-testing-20261105/gui-sensitive-target-readonly/verification-v2.json)与[源码边界审阅](../artifacts/user-testing-20261105/gui-sensitive-target-readonly/review.md)分别保存。生产代码未因这两项审阅修改。
