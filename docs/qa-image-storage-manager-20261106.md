# 桌面图片存储管理验收

设置 → 常规新增“图片存储”用量与管理入口。管理窗口每页读取 20 条图片信息，不解码整库缩略图，只支持单张明确确认后的删除。确认显示已知消息与会话数量；执行前再次核对图片信息和具体引用集合，数量相同但引用被换到另一条消息也会要求重新确认。

图片暂存、导入、聊天请求、Agent 执行与远程写入使用同一生命周期保护。正在退出但尚未确认闭合的执行资源不能当成空闲。请求超时、隐藏窗口或点击停止不会提前释放真正运行中的工作；没有超时自动放行或自动重试删除。显示图片的读取工作也保持其真实占用，迟到结果不能恢复旧像素。

删除只清除此电脑图片库的副本，保留聊天文字与引用记录。旧聊天可能无法继续查看或识别该图，手机待发内容可能需要重新附图，手机已有缓存可能保留。64 MiB 逻辑容量没有提高，也没有自动删除历史图片。引用摘要不读取消息正文；它只用于本机确认，不扩展手机通信协议。

## 实际整体验收

根审查运行实际 ApplicationController、设置工厂、图片库、管理服务、原生大图和编译聊天界面。所有配置、凭据、启动偏好和图片均为自有隔离数据；模型入口被禁止。实际走过 21 张图片的分页、跨两个会话的三条引用、Enter 取消确认、明确确认后真实删除一次、关闭旧预览、聊天显示图片缺失，以及通过真实文件选择入口重新上传相同内容后恢复旧消息图片。20 项判断全部通过，13 个生产源哈希前后不变，测试窗口、Qt 工作线程和草稿线程已结束。

- [最终完整流程](../artifacts/user-testing-20261105/root-image-manager-full-visible-v2/verification.json)
- [设置入口截图](../artifacts/user-testing-20261105/root-image-manager-full-visible-v2/01-general-entry.png)
- [删除后缺失状态](../artifacts/user-testing-20261105/root-image-manager-full-visible-v2/03-deleted-image-fallback.png)
- [同图重传恢复](../artifacts/user-testing-20261105/root-image-manager-full-visible-v2/04-reuploaded-image-restored.png)

首轮完整流程虽功能判断全部通过，仍因三个被拦截的进程探测判为失败。独立隐藏诊断确认它们来自未隔离的长期记忆辅助凭据构造，经 keyring 依赖导入触发平台探测；实际进程启动为零。最终只在测试程序中为所有缺省 CredentialStore 注入内存后端，原进程禁令继续生效，生产代码未为测试放宽。原失败、调用栈归因和原脚本全部保留。另补齐 DOM 最终状态及绘制等待，避免截图抓到上一帧。[初轮失败及诊断](../artifacts/user-testing-20261105/root-image-manager-full-visible/verification.json) [诊断](../artifacts/user-testing-20261105/root-image-manager-full-visible/process-boundary-diagnostic.json)

## 分阶段证据

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 纯生命周期、后台资源与 Controller/面板接线 | 分别按阶段通过 | [基础层记录](qa-image-storage-foundation-20261106.md) |
| 远程入口及超时后真实工作保护 | 27 项通过，使用模拟图片删除 | [RPC 报告](../artifacts/user-testing-20261105/image-storage-rpc-phase4/verification.json) |
| 全库存档引用统计及同快照身份摘要 | 52 项通过 | [引用快照](../artifacts/user-testing-20261105/image-reference-snapshot/verification.json) |
| 实际管理服务与自有图片库 | 12 项通过，实际删除 4 次 | [管理服务](../artifacts/user-testing-20261105/image-storage-manager-phase5/after-reference-revision.json) |
| 独立确认、关闭、提交未知及读取失败 | 4 项通过，实际删除 2 次 | [独立审查](../artifacts/user-testing-20261105/image-storage-manager-phase5/independent-review-reference-revision-final.json) |
| 原生管理窗口 | 12 项交互检查及后续 5 项外观检查分别通过 | [交互](../artifacts/user-testing-20261105/image-storage-native-ui/final-tests.json) [外观](../artifacts/user-testing-20261105/image-storage-native-ui/disabled-style-review.json) |
| 预览/缩略图缓存、异步读取与前端 revision | 新 Python 10 项、Node 7 项、实际界面 13 项及旧回归 66 项分别通过 | [缓存审查](../artifacts/user-testing-20261105/image-cache-epoch/review.md) |
| 根审查实际配对 RPC 与自有图片库组合 | 19 项判断通过，实际删除 2 次 | [组合审查](../artifacts/user-testing-20261105/root-rpc-image-storage-review-v2/verification.json) |

历史失败未覆盖：SQLite JSON1 的 NUL 截断边界、仅绑定引用数量的删除确认缺口、取消确认后的残留进度、禁用危险按钮外观，以及测试程序的清理假设/错误字段/旧故障注入入口均有原记录。表中检查批次存在重叠，不合并为真实任务成功率。

本轮真实模型调用为 0，没有触碰用户图片、操作真实手机或重启原桌宠。Android 源码与 APK 未因本功能重新构建；蜂窝网络、公开中继与实体手机缓存生命周期不属于这些结果。Chromium 仍输出本机网络变化监听警告 10108，最终断言、Qt 回调检查和资源告警检查均通过。任意外部文件变更和整个桌面 Agent 的可靠性不由此证明。

后续增加占用上下文提示，并修正队列指导、优化记忆状态读取。更新后的编译界面再次通过 20 项完整可见检查，19 个列出的生产源稳定，先前 v2 报告保留为原里程碑，不替换成后续源码的结果。详见[状态反馈补充](qa-state-feedback-20261106.md)。

## 末页删除后的分页恢复

继续使用时发现，第二页仅剩一张图片，删除后仍停在空的第二页，会显示“暂无图片”，即使第一页还有 20 张。外部删除使当前页变空、以及整个库被删空后也会留下旧页码。实际图片库复现的四个场景中原实现有三项失败，正常非空页刷新通过；首轮另有测试发送非法空图片 ID 的错误，原日志和测试版本保留，修正夹具后再取得上述三项产品失败。

管理窗口现在只在成功读取的空页、且总数表明当前页已不存在时重新读取更早的有效页；全空回到第一页。每次恢复的偏移严格减小，不向前猜页，也不触发删除重试。原 generation/epoch 检查、删除确认、资源收尾和引用身份核对保持原路径。

[四项真实存储场景](../artifacts/user-testing-20261105/image-pagination/after.json)与[25 项包含相关窗口检查的回归](../artifacts/user-testing-20261105/image-pagination/related.json)分别通过，不合并重复数量。另在自有原生窗口实际点击确认、删除末页唯一图片一次，列表回到第一页面的 20 张，未自动选中下一张，键盘焦点留在列表。九项窗口判断通过，相关源前后稳定，窗口和工作线程归零；[原生结果](../artifacts/user-testing-20261105/image-pagination/visible/verification.json)和[返回后的窗口](../artifacts/user-testing-20261105/image-pagination/visible/returned-first-page.png)保留。使用合成 QTest 事件与自有图片，不是实体手机或物理鼠标验收。

独立检查另用真实隐藏窗口和受控回调验证连续缩减、epoch 变化与旧回调重放，两项通过；读取偏移只在空页恢复时向后移动，重复旧回调不会结束新请求或调用删除。[独立评审](../artifacts/user-testing-20261105/image-pagination-independent-review/review.md)保留该批五个源码散列，不将受控回调算成真实删除次数。随后只修正生命周期模块过时的“尚未接线”说明，所有可执行 AST 相同，[文档差异记录](../artifacts/user-testing-20261105/image-pagination/gate-doc-only.json)单列。

大小显示随后统一使用 B、KiB 或 MiB：小图不会显示成“0.00 MiB”，总用量与每行采用同一格式。浅色窄窗与深色窗口已查看，十个边界样值正确；配额、进度公式、列表与删除路径保持原 AST。[显示记录](../artifacts/user-testing-20261105/image-size-display/review.md)是自有元数据回调和当前主题的显示检查，没有新增真实存储或删除次数。
