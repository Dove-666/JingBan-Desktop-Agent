# 第一批角色资产

当前默认 `master.png`、`static/tsundere.png` 与两段动画已更新为发丝孔洞/白边修复版，原图和修复前版本保留。正常立绘参考的是用户新指定的桌面原图，新增修正源在 `source/*-cutout-corrected-v2.png`，下方首批来源数据保留历史含义。最新文件、颜色校正与本轮验证见 [抠图修复说明](../../docs/ASSET_MATTE_REPAIR.md)。

统一画布原图、**抱胸傲娇静态图**和两段真实动作现已发布。每段动作都已完成 124 原帧真实 SAM2 分割，交付 62 张 1024×1024 RGBA 帧、12 FPS、5167ms；对应清单均为 `complete:true`、`qa_passed:true`。本轮每个动作只提交一次，预算 **2/2 已用完，无额外重试**，页面余额变化确认合计 3 元。实际奶凶是蹲跪姿、未完全趴卧；问号淡出透明度包含估算，原生索引44的极小问号没有独立 SAM2 mask。详见 [角色资产交付与使用](../../docs/CHARACTER_ASSETS.md)与 [SAM2 管线](../../tools/asset_pipeline/README.md)。

## 在桌宠中切换

用 `.\.venv\Scripts\python.exe main.py --debug-panel` 启动，或右键 →「打开调试面板」→「角色控制」→「角色素材与序列帧」。如果仍在运行旧版本，需先右键「退出桌宠」再启动，否则单实例保护只会激活原窗口。静态下拉框可选择「原始鲸鱼娘」或「傲娇 · 静态」。原始模式优先显示 `master.png`，缺失时回退到原 `assets/whale.png`；静态素材独立于七种 API 逻辑表情。日常切图、播放无需安装 SAM2 或 Torch。

动作下拉框选择「思考 · 挠头」或「趴下 · 奶凶呲牙」并点击「播放动作」。挠头播放一次后恢复静态与待机；奶凶播放一次后保持末帧，点击「停止 / 恢复」可回到当前静态角色并释放帧缓存。拖动期间冻结，松开并平滑恢复后继续播放。「重新检查素材」可刷新清单状态；缺失或损坏时仍会明确报错。

## 文件与来源

| 路径 | 内容与状态 |
| --- | --- |
| `source/master-original.png` | `assets/whale.png` 的逐字节副本，保留原始素材。 |
| `master.png` | 将已有原图规范化到 1024×1024 透明画布，保留角色完整比例。 |
| `source/tsundere-generated.png` | 内置图像生成模型以原图为参考生成的静态原始输出，保留源 Alpha。 |
| `static/tsundere.png` | 已规范化的抱胸傲娇静态角色，真实透明背景；不是视频动画。 |
| `canvas.json` | 1024 画布、896 安全范围及共同底部锚点约定。 |
| `shared_layout.json` | 两段原生动作与特效联合边界得到的共同安全系数，静态角色也按此规范化。 |
| `static_layout_report.json` | 静态源与输出哈希、变换参数、Alpha 与实际边界。 |
| `production_manifest.json` | 生产状态、来源和视频预算；已用 2/2，每个动作各提交一次。 |
| `prompts/think_scratch_head.txt`、`prompts/snarl_cute.txt` | 用户动作提示词归档；实际生成、下载与 QA 以生产记录为准。 |
| `animations/think_scratch_head/` | 已发布挠头成品：`animation.json`、`qa.json`、`frames/0001.png`～`0062.png`。 |
| `animations/snarl_cute/` | 已发布奶凶成品：同样 62 帧；完成策略 `hold_last`。 |
| `animations/README.md` | 目录结构、结束行为与已知视觉限制。 |

本批保留原图，没有把新图片覆盖到 `assets/whale.png`。当前原图与 `source/master-original.png` 的 SHA256 均为 `79450abd5d7201fcb7c8e7a5a668bd5550342658d27f49e3f26d0e4235ff801a`，输出哈希见生产清单及布局报告。

静态规范化只对完整源画布等比缩放和平移，不重新绘制角色、不逐张裁切身体；共同中心 x=512、主体底部约 y=960。两动作共同安全系数为 `0.7412408317219669`，目标站姿高度约 665.634px，静态实际 Alpha 边界高度为 master 667px、tsundere 666px。静态输出已从保留源图同步规范化，原图未修改。该预留用于容纳动作和独立问号，不应逐帧放大抵消；不保证 AI 结果与原图严格逐像素一致。

## 视频与 SAM2 的真实状态

此前电脑操作通道不可用的阻塞已由用户授权的 Windows UI Automation 路线解决。本轮在 AI 派实际提交两条 MiniMax-H3，每条选择 5 秒、1 个输出；`attempts` 均为 1，合计 2/2，预算耗尽后不再创建任务。

`think_scratch_head` 源视频在 `artifacts/asset_pipeline/inputs/think_scratch_head_download.mp4`：2560×1440、24 FPS、124 帧、实际 5.166667 秒、4,779,141 字节，SHA256 `f45dbd3f005218ec8c8c8bb73c9d4bf85767b1fd34281d0bf435af69e57ed771`。浏览器下载曾受 CORS 限制，已取回同一原始视频对象，没有重新生成，不公开签名 URL。124 帧已实际分割；问号淡出修正已随最终 62 帧发布，估算方法和缺失的极小首现问号在清单 `origin.alpha_postprocess` 中披露。

`snarl_cute` 源视频也已下载：2560×1440、24 FPS、124 帧，视频轨实际 5.166667 秒，4,119,568 字节，SHA256 `abbc57ecaacb17d2013d4d1e2a3540dd5ab7b453503bb0c66677f038f3e97c49`。含音频容器总长 5.267344 秒，音频不进入桌宠序列帧。124 原帧已实际分割并发布 62 帧；动作实际偏蹲跪而非完整趴卧，作为生成偏差保留说明，不额外提交重做。

本机已经用真实 Meta SAM2 在 CUDA 上处理过**四帧重复静态合成图**，用于验证接口、Alpha 和统一画布工具。它不是这两个动作视频，不是动态成品，不证明运动分割、跨动作比例或动作质量已验收。实际结果及边界见 [SAM2 技术 smoke](../../tools/asset_pipeline/README.md#技术-smoke没有动作视频时也可以验证真实-sam2)。

完整回归 **640 项通过，237.607 秒，零跳过、零失败**，`compileall` 通过；[真实成品播放 smoke](../../artifacts/character_assets_real_v1/real_playback/result.json)另验证两段播放、六档缩放、拖动/恢复、停止与结束策略，为离屏 Qt、无系统鼠标。原生 Windows UIA 已触发挠头并看到[右手动作与蓝问号](../../artifacts/character_assets_real_v1/native_pet_think_middle.png)，奶凶原生播放和物理拖动尚未确认。稳态短采样 CPU 为单核 3.50%/1.74%，进程峰值 RSS 496.52MiB，停止后约130MiB；保持末帧仍保留帧缓存。详见[性能与限制](../../docs/CHARACTER_ASSETS.md#实际播放性能与内存)。
