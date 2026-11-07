# R21 中断后的请求归属检查

两项新的交错检查通过，没有据此修改生产代码：停止旧 Agent 后立即发送普通聊天，旧进度与最终回执没有覆盖新请求；在超时完成回调中同步提交下一项 Agent，下一任务的待发消息、停止按钮、工作状态和最终结果保持正确。

检查经过实际 ChatController 与自有 SQLite，后台是可控制的内存适配器；超时由固定 AgentError 注入。这不是实际 worker 超时、模型规划或桌面操作验收。模型和网络调用为 0，工作线程池及可见测试窗口在结束时均为 0，五份生产源码前后不变。

初次夹具误用了非十六进制身份值，没有到达 Agent，原失败记录保留。修正后的夹具只通过替代加载器构建真实的不可变空快照，不读取用户运行配置。

- [有效交错检查报告](../artifacts/user-testing-20261105/task-audit/agent-resume-flow-review-v2.json)
- [初次夹具失败](../artifacts/user-testing-20261105/task-audit/agent-resume-flow-before.json)

安卓数据层另有两个新的断线恢复组合通过：通信确认超时但宿主已完成时，重连拉取唯一回复并结束原待发记录，没有再次提交任务；取消等待中的轮询后，重连也能补齐期间完成的回复，后续轮询没有复制任务或消息。测试经过实际 Repository 协程和数据库，RPC、凭据库及宿主记录均为合成对象，没有真实网络、Iroh、ForegroundSyncLoop、设备生命周期或实际 worker 超时验收。

该次 Gradle 仅离线运行两个测试，使用本机已有依赖。初次本地 SDK 路径解析失败、测试正文未执行的记录保留，修正自有测试路径后才取得有效结果。生产源码和历史 APK 字节不变，没有重新构建或安装应用。[原始恢复报告](../artifacts/user-testing-20261105/task-audit/android-reconnect-review-v2.json)单独记录范围。

同时通过 GitHub 连接重新读取 DeepSeek Harness 两个固定版本的四份源码：核心 Agent 循环、检查点策略、持久句柄与待办工具。先前参考版本 `ce7073d5` 与此次默认分支头 `fcc0ad46` 的这四份 Git blob 及返回正文均相同。只核对这四份文件，不代表整个仓库未变化，也没有因此替换鲸伴的运行时。

核心循环继续体现按阶段处理取消、将中断流与工具回执收束后再结束步骤的设计；检查点策略要求先持久化，再派发后续模型或工具。鲸伴的权限、目标验收和未知输入屏障仍需本地独立证据，不能用框架名称代替。

- [上游核心循环](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts)
- [上游检查点策略](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/session/session-checkpoint-policy/src/index.ts)
- [两版本 Git blob 对照](../artifacts/task-audit/deepseek-reference-20261106/blob-comparison.json)
- [本地参考副本范围](../artifacts/task-audit/deepseek-reference-20261106/verification.json)
