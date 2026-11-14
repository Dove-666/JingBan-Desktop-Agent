# Agent 活动流改版记录

日期：2026-11-15。仅本地源码及内置聊天页面更新，未上传 GitHub，未重新制作 Windows 安装包或 APK。

## 显示方式

- 参考 DSH 官方的短工具行：图标、动作、操作对象。删除逐行时间、步数和竖直时间线，悬停仍可查看时间。
- 文件显示文件名，网页显示域名，电脑操作区分查看画面、选择应用、切换窗口、点击、输入和滚动等；有可信应用信息时显示应用名。
- 同一次调用的运行与结束状态更新同一条记录。连续相同且已完成的动作可以折叠；失败不会并入成功组。较早记录可展开，仍在执行的动作保持可见。
- 子任务按真实开始、返回、停止和失败事件显示。“已返回”不表示用户目标已经完成。
- 公开进度说明与操作行穿插显示；运行时显示动态图标，结束后自动收起，用户可以再次展开。
- 保留独立最终回复，清理只有空白头像和复制按钮的运行占位。适配浅色、深色、窄窗口及减少动画设置。

旧记录如果只有“操作电脑”，仍只能显示该信息，不能恢复没有保存的动作对象。

## 数据边界

工具活动来自实际调用与结果，使用固定字段重建展示对象，不转发任意参数或回调详情。`getApp` 对应“选择应用”，避免把选择授权误写成已启动应用。

进度说明只取官方已落定的 assistant `TextBlock`，且必须与真实工具调用匹配。`ReasoningBlock`、内部推理、原始流片段、工具返回正文和独立最终答案不作为进度说明。没有公开说明时只显示真实动作，不编造解释。

展示前清理私有路径、网址查询参数、常见凭据、代码块、图片数据及格式控制字符。带空格的完整或未闭合引号凭据已加入回归；不能将这种过滤表述为任意敏感文本的完整检测保证。

手机同步仍采用固定动作标签，不发送桌面公开说明及对象详情。远程同步 schema 更新为 13，旧公开投影按现有原子升级流程刷新，归档不删除。Android 仅更新标签兼容，本批未重做手机活动流或 APK。

## 官方依据

- [DSH 工具呈现接口](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/tools/src/presentation.ts)
- [DSH 紧凑工具行](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-tool/README.md)
- 本地固定版本 SDK 的消息类型与事件：公开文本、推理块、工具调用、子任务生命周期分别处理。

## 验证结果与范围

| 检查 | 本批结果 | 记录 |
| --- | --- | --- |
| Node 活动、配置和权限单元测试 | 20 项通过 | `artifacts/activity-node-final.log` |
| 官方 SDK 活动链路 | 1 项通过 | `artifacts/activity-sdk-accepted.log` |
| Python 展示与远程边界 | 12 项通过 | `artifacts/activity-boundary-final.log` |
| Python → 官方 SDK 完整宿主链路 | 1 项通过，核对实际文件字节与独立最终回复 | `artifacts/activity-host-sdk-accepted.log` |
| 远程投影与升级 | 17 项通过 | `artifacts/activity-remote-projection-final.log` |
| 界面、既有聊天和权限菜单回归 | 25 项通过 | `artifacts/activity-ui-regression.log` |
| 界面最后复核 | 7 项通过；子任务前缀调整后相关 1 项再通过 | `artifacts/activity-ui-accepted.log`、`artifacts/activity-ui-detail-final.log` |
| Android 数据库存储兼容 | 定向 1 项通过 | `artifacts/activity-android-fixture-corrected.log` |
| TypeScript / Vite | 构建通过，更新内置页面 | `artifacts/activity-frontend-build-final.log` |

以上测试有重叠，不合计成独立测试总数。全部使用本地合成接口与受控数据，没有使用用户模型 API，也没有进行真实桌面鼠标键盘操作或物理手机验收。

实际 Qt WebEngine 截图已检查非空并人工查看浅深色、1000 / 375 像素宽度；软件渲染日志仍有 GPU 警告，截图才是界面显示证据。示例使用测试数据，不代表真实 Agent 已完成示例任务。

- [浅色示例](../artifacts/activity-feed-ui/activity-light-1000.png)
- [深色示例](../artifacts/activity-feed-ui/activity-dark-1000.png)
- [窄窗口示例](../artifacts/activity-feed-ui/activity-light-375.png)

### 保留的早期失败

首轮界面回归仍断言旧逐行时间显示，更新为新设计后通过；日志保留在 `activity-ui-existing-first.log`。Python 首次归档用例缺少来源或使用非 UUID 会话 ID，宽回归曾为 48 项通过、1 项夹具错误；修正夹具后相关测试通过，旧 `activity-host-first.log`、`activity-host-final.log` 保留。远程时间夹具的 `Z` 写法不符合本机现有 Python 3.10 解析路径，改为带偏移时间后通过，保留 `activity-remote-projection-tests.log`。

Android 最后增补用例曾使用无效 `step-select` ID，被正式解析器正确拒绝；只修正测试为数字 ID，没有放宽验证。失败记录为 `activity-android-accepted.log`，最终通过记录为 `activity-android-fixture-corrected.log`。

本批定向通过不覆盖或推翻此前权限任务中旧队列夹具失败，也不代表一般电脑任务成功率已经提高。
