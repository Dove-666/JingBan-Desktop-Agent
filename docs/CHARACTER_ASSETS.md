# 首批鲸鱼娘角色资产

后续修复：本页记录首批生产过程。现用正常/傲娇静态图与两段动画已修复发丝孔洞白块和内缘白边，动画增加固定蓝色校正；原始源和修复前包保留。修复后的准确来源、布局数据、77 项定向测试及真实素材播放结果见 [本轮抠图修复说明](ASSET_MATTE_REPAIR.md)。

本批已经发布 1024×1024 的原角色规范化图、抱胸傲娇静态图，以及「思考 · 挠头」「趴下 · 奶凶呲牙」两段真实 SAM2 动作。两段各为 **62 张 RGBA 帧、12 FPS、5167ms**，包含完整 `animation.json`、`qa.json`，均为 `complete:true`、`qa_passed:true`。两条 MiniMax-H3 任务各只提交一次，**预算 2/2 已用完，页面余额变化确认合计 3 元，无额外重试**。来源与审核记录见[生产清单](../assets/character/production_manifest.json)，真实成品已通过[离屏播放检查](../artifacts/character_assets_real_v1/real_playback/result.json)。

实际 `snarl_cute` 动作为蹲跪姿，未完全达到用户要求的趴卧；生成视频也不能保证与参考图严格逐像素一致。这里明确保留生成偏差，不以改写动作名称或额外提交任务掩盖。

## 现在可以使用的内容

如果仍在运行旧桌宠，先通过原桌宠右键菜单「退出桌宠」，再从本仓库启动；单实例保护只会激活已有窗口，不会热更新源码。本轮生产应用已重新启动并确认桌宠窗口可见，具体原生交互与离屏测试的范围分开记录。

```powershell
# 已有环境时直接启动；首次安装只需要桌宠的普通依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --debug-panel
```

日常切换原始/傲娇静态图不需要安装 SAM2、Torch 或素材工具环境；这些图片已经在仓库内。`.venv-assets` 只用于已有视频的离线素材制作。

在「角色控制 → 角色素材与序列帧」中：

1. 静态下拉框选择「原始鲸鱼娘」或「傲娇 · 静态」，立即切换；前者使用 `assets/character/master.png`，该文件缺失时回退到原 `assets/whale.png`。自定义 `--image` 仍按原入口处理。
2. 下方动作框选择「思考 · 挠头」或「趴下 · 奶凶呲牙」，点击「播放动作」。挠头完整播放一次后恢复静态待机；奶凶播放一次后保持末帧，以保留用户要求的末尾低姿态。
3. 「停止 / 恢复」恢复当前静态角色并释放帧缓存；「重新检查素材」刷新文件与清单状态。以后文件缺失、草稿未完成或透明画布无效时，播放器仍会明确报错，不用静态图片伪装动作。
4. 静态素材和七种 API 逻辑表情分别管理。选择傲娇 PNG 不代表已经提供七套面部表情；缺少表情图时继续使用当前角色与轻量反馈。拖动优先，拖动中不切换静态素材；有效序列帧在拖动与平滑恢复期间暂停。

角色缩放沿用原有 50%～200% 控制，绘制保持比例。素材播放器使用原有帧计时器，先在后台读取、验证并缓存帧；播放时不每帧重新读文件，也不靠反复新建窗口实现动作。

## 交付文件与来源

| 文件 | 来源与用途 | 当前状态 |
| --- | --- | --- |
| `assets/whale.png` | 本批开始时已有的原角色透明 PNG。 | 保留，不覆盖。 |
| `assets/character/source/master-original.png` | 原图逐字节副本。 | 与当前原图 SHA256 相同。 |
| `assets/character/master.png` | 现有原图经完整画布等比缩放、平移后的 1024 透明版本。 | 可选用。 |
| `assets/character/source/tsundere-generated.png` | 内置图像生成模型参考原角色生成的抱胸傲娇静态原始输出。 | 保留源 Alpha。 |
| `assets/character/static/tsundere.png` | 上述生成图的 1024 透明规范化结果。 | 已接入静态选择。 |
| `assets/character/canvas.json` | 共同画布、安全范围及底部锚点。 | 已使用。 |
| `assets/character/shared_layout.json` | 依据两段原生动作和特效联合边界建立的共同安全系数与站姿尺度。 | 已计算，并应用到两张静态输出。 |
| `assets/character/static_layout_report.json` | 静态来源、输出哈希、缩放、平移和 Alpha 边界。 | 可核对。 |
| `assets/character/production_manifest.json` | 批次来源、视频预算、两个动作的真实生产状态。 | 提交 2/2；已发布并保留来源与 QA。 |
| `assets/character/prompts/*.txt` | 用户指定动作的提示词归档。 | 文本归档；实际生成、下载与 QA 状态单独记录。 |
| `assets/character/animations/think_scratch_head/` | 挠头成品清单、QA 与 `frames/0001.png`～`0062.png`。 | 已发布，单次播放后回待机。 |
| `assets/character/animations/snarl_cute/` | 奶凶成品清单、QA 与 62 张 RGBA 帧。 | 已发布，单次播放后保持末帧。 |
| `assets/character/animations/README.md` | 目录、结束行为和视觉限制。 | 已与真实成品同步。 |

本批原图与原始副本的实际 SHA256 为 `79450abd5d7201fcb7c8e7a5a668bd5550342658d27f49e3f26d0e4235ff801a`。`master.png` 和 `tsundere.png` 的实际哈希分别与生产清单记录一致；静态变换记录保留在布局报告中。这些资产来自已有原图与内置图像生成，不使用第三方静态图片网站替代。

目录快速入口：[资产清单](../assets/character/README.md)、[生产状态](../assets/character/production_manifest.json)、[共同画布](../assets/character/canvas.json)。

## 两个视频动作：已提交 2/2 的真实进度

| 动作 | 预期内容 | 实际生产状态 |
| --- | --- | --- |
| `think_scratch_head` | 右手挠头、歪头疑惑、蓝色问号，最后尽量回到初始站姿。 | 源视频全部 124 帧已真实分割，62 帧成品已发布；问号淡出估算与缺失极小首现问号的限制见下文。 |
| `snarl_cute` | 向前趴低、双手前伸、露小虎牙，最后保持可爱呲牙姿势。 | 源视频全部 124 帧已真实分割，62 帧成品已发布，保持末帧；实际偏蹲跪，未完全趴卧。 |

本轮已在 AI 派 `https://aipai.cn/image` 实际选择 `MiniMax-H3`，每条请求 5 秒、1 个输出。用户授权其他操作方法后，通过 Windows UI Automation 完成原生页面提交，早先 `Computer Use native pipe is unavailable` 已不再是阻塞。两个不同返回视频对象已核对，页面余额变化确认总计 3 元。每个动作各使用一次机会，总计 2/2 耗尽，之后不再创建、重试或转到其他服务提交新任务。

已取回的真实输入为 [挠头源视频](../artifacts/asset_pipeline/inputs/think_scratch_head_download.mp4)：2560×1440、24 FPS、124 帧，实际时长 **5.166667 秒**，大小 4,779,141 字节，SHA256 `f45dbd3f005218ec8c8c8bb73c9d4bf85767b1fd34281d0bf435af69e57ed771`。浏览器下载曾被 CORS 拒绝，现已取回同一生成结果返回的原始视频对象，没有重新生成；不在源码或文档中公开带签名的下载地址。请求“5 秒”不等于源文件恰好 5.000 秒，交付帧数与时长应按真实视频计算。

[奶凶源视频](../artifacts/asset_pipeline/inputs/snarl_cute_download.mp4)也已取回：2560×1440、24 FPS、124 个视频帧，视频轨时长同为 **5.166667 秒**，4,119,568 字节，SHA256 `abbc57ecaacb17d2013d4d1e2a3540dd5ab7b453503bb0c66677f038f3e97c49`。该 MP4 含音频，容器总长 5.267344 秒；桌宠只使用视频画面的 RGBA 序列帧，不播放或导入这段音频。

两段用户原始动作提示词保存为 [思考挠头](../assets/character/prompts/think_scratch_head.txt)与[趴下奶凶呲牙](../assets/character/prompts/snarl_cute.txt)，仅追加 5 秒、完整居中和纯色背景的制作参数。挠头动作保留蓝色问号；趴下动作保留小虎牙、前伸双手和末尾低姿态，不改成握拳后回站姿。提示词归档不是成品凭证；动作是否正确执行必须检查真实视频与最终序列帧。

## 1024 画布与共同锚点

| 约定 | 值与意义 |
| --- | --- |
| 完整画布 | 所有静态图和产品动画帧均为 1024×1024 RGBA，保留透明区域。 |
| 共同底部锚点 | 像素 `(512,960)`，归一化为 `{"x":0.5,"y":0.9375}`。 |
| 安全范围 | 896，用于依据角色可见联合边界计算统一变换；实际软边范围仍单独检查。 |
| 本次共同安全系数 | `0.7412408317219669`，由两段全部原生帧的角色/特效联合边界共同约束。 |
| 本次站姿高度 | 共同目标约 665.634px；规范化静态图的实际 Alpha 边界为 master 667px、tsundere 666px。 |
| 单个动作的变换 | 对全部已分割原生帧取联合 bbox，只计算一次缩放和平移，整段共享。 |
| 禁止的处理 | 不逐帧裁出角色再分别居中，不逐帧调整比例，不拉伸，不把身体切到画布外。 |

保留共同画布才能让角色在切帧时稳定。若每张图按自身边界重新放大，头发、手臂或尾巴的小变化就会变成整体抖动。本次以角色站姿作为尺度参考，再让两段动作及特效的最大联合边界约束同一个安全系数；`master.png` 和 `tsundere.png` 已从保留源图同步重新规范化，原始 `assets/whale.png` 没有改变。

这会为抬手、姿态变化和独立问号预留更多透明空间，静态图不再单独填满 896 范围；不要逐帧放大来抵消预留。站姿尺度/共同锚点是布局约束，不保证 AI 生成的五官或衣服逐像素相同，也不能替代最终运动边缘、衔接和姿态的视觉检查。参数及每段参考框见 [共同布局记录](../assets/character/shared_layout.json)。

## 真实 SAM2 工具与证据边界

工具位于 `tools/asset_pipeline/`，处理**已经存在的视频**；它不会生成视频或替用户提交服务任务。完整环境、固定的 Meta 官方源码 commit、checkpoint 哈希及命令见 [SAM2 管线](../tools/asset_pipeline/README.md)。素材工具使用独立 `.venv-assets`，不会要求桌宠主环境安装 Torch，也不会在桌宠启动时下载权重。

本机的独立环境报告记录了现有 Conda Torch 的复用、CUDA 可用和 RTX 4060 Laptop GPU。真实 SAM2 此前对四帧重复静态合成图运行 mask 预测和时序传播，技术复测用时 6.736 秒；原图未被该 smoke 修改。分割及参考 Alpha IoU 见 [此前 SAM2 技术结果](../artifacts/asset_pipeline/sam2_smoke_v2/report.json)。这些帧在技术输出目录中，没有发布到产品动画目录。

上述技术结果只是早期环境证据。本轮两段真实视频已分别完成全部 124 原生帧的实际 SAM2 分割，并采样成各 62 帧正式成品；产品清单 `origin.synthetic:false`，没有将重复静态测试当作动作。Windows 下未构建可选 connected-components CUDA 扩展，工具关闭该后处理；换机器仍需重验环境。

问号形状来自独立的真实 SAM2 对象；淡出阶段在其有效支持区域内，依据源 RGB 与黑背景假设估计 Alpha，再记录到 `origin.alpha_postprocess`。这个 Alpha 不是源视频原生透明通道，极暗编码值也无法恢复精确原色。原生索引44的极小问号没有独立 SAM2 mask，未补画虚构替代。两项限制已保留在最终清单，不能将“QA 通过”解释为无任何视觉损失或像素完全相同。

处理已取回及后续生成完成的视频时，流程是：

1. 保留原视频，使用 `ffprobe` 读取时长与时间戳，解码每个原生帧。
2. 用真实首帧坐标框选完整角色，SAM2 分割全部原生帧；不能先只抽取交付帧，也不能用色键冒充 SAM2。
3. 基于所有原生 Alpha 帧的联合边界规范化到共同画布。
4. 分割完成后按真实时间戳选择交付 FPS。本次两段均从 24 FPS、124 原帧采样成 12 FPS、62 帧成品，约 5.166667 秒；不能为了符合请求的“5 秒”而机械裁成 60 帧。不插帧，不把重复静态图当作动作。
5. 自动检查透明度、尺寸、边界和 mask 面积突变，生成 `qa.json` 与 `animation.json` 草稿。真实动作、边缘、浅深背景、尺度和衔接仍由人工检查。
6. 视觉验收后，在新的输出目录重跑并指定 `--reviewed`，再将完整审核目录发布到 `assets/character/animations/<name>/`。已有输出目录不会被工具覆盖。

`--keep-source-frames` 可另保留每个原生尺寸 RGBA 帧；原生 124 帧分割档案不等于交付的 62 帧。本轮正式路径为 [挠头成品](../assets/character/animations/think_scratch_head/animation.json)与[奶凶成品](../assets/character/animations/snarl_cute/animation.json)，各自同目录含 `qa.json` 和 `frames/`。早期 `artifacts/asset_pipeline/outputs/*_native_draft_v1/` 是制作草稿，运行时应使用正式目录。

## 动作清单：数量、时长、来源和 QA

以下字段已用于本轮两个正式动作，也适用于后续同类成品。不能通过手写一个“完成”文件来补足缺失视频或尾帧。

| 字段 | 要求与检查 |
| --- | --- |
| `schema_version`、`name` | 当前版本 1，名称对应所在动作目录。 |
| `canvas` | 固定 1024×1024；每帧完整画布一致。 |
| `fps` | 交付播放帧率 12～24，不是源视频原生 FPS，也不是桌宠 GUI 刷新率。 |
| `frame_count`、`frames` | 2～96 帧，数量与真实 PNG 列表一致，路径不能越出当前动作目录。 |
| `duration_ms` | `frame_count / fps × 1000`；当前播放器允许不超过 2ms 的舍入差。不用旧时长掩盖截断序列。 |
| `loop` | 布尔值；本批动作工具输出 `false`，完整播放一次。循环动作还需独立检查首尾衔接。 |
| `completion` | `idle` 播放结束恢复当前静态角色与待机；`hold_last` 保持末帧，直到停止。挠头预期回初始站姿，趴低奶凶呲牙需保留末尾低姿态，不能自动替换为回站姿。主要用于非循环片段。 |
| `anchor` | 本批共用 `(0.5,0.9375)`，与 `canvas.json` 对齐。 |
| 来源 / provenance | 实际字段名为 `origin`。记录真实源视频 SHA256、SAM2/checkpoint、源 FPS、原帧数、已分割帧数、交付帧率与数量、源帧索引、采样策略。不要另造一个没有实现的 `provenance` 接口。 |
| `origin.synthetic` | 产品交付应为 `false`；技术测试输出不能当真实动作，播放器会拒绝标记为 synthetic 的素材。 |
| `qa_passed`、`qa.json` | 自动完整性与 Alpha/边界检查，不能代表角色身份、动作或边缘已经视觉通过。 |
| `complete` | 本轮两个正式目录均为 `true`；未来草稿仍保留 `false` 并拒绝播放。真实性与视觉限制另由来源和 QA 记录。 |

`complete:false`、`qa_passed:false`、缺失帧、异常画布/透明度等均会让播放器明确报错并保留正常桌宠。缺失素材不会自动触发视频生成。清单中的来源及审核字段是生产记录，不能代替保留真实输入和检查图；单纯把布尔值改为 `true` 不是验收。

## 实际播放性能与内存

本轮[真实成品播放报告](../artifacts/character_assets_real_v1/real_playback/result.json)使用仓库最终 62 帧素材与真实 Qt 播放器，临时 INI、内存凭据、离屏模式，未操作生产窗口或系统鼠标。两段都验证了缓存切帧、六档缩放、拖动冻结、平滑恢复、停止及各自结束策略。

| 成品 | 首次加载缓存 | 稳态 CPU（单逻辑核口径） | 稳态 GUI 心跳最大间隔 |
| --- | --- | --- | --- |
| 挠头 | 0.8781 秒 | 3.50% | 18.076ms |
| 奶凶 | 0.8876 秒 | 1.74% | 18.452ms |

稳态采样分别约 0.892、0.897 秒，属于本机短采样，不是长时压力测试，也不包含图片加载、重缩放、聊天或感知请求的 CPU 峰值。心跳间隔是 GUI 响应采样，不是动作 12 FPS 的帧间隔。

每段 62 张 1024 RGBA 原始帧约 248MiB，另有缩放缓存与 Qt 基线。本次含 200% 缩放检查的进程峰值 RSS 为 **496.52MiB**；停止释放逻辑缓存后约 **130MiB**。奶凶 `hold_last` 保持末帧期间仍保留约 400MiB 的整段缓存，点击「停止 / 恢复」才释放。系统分配器不保证每台机器都立即回落到相同 RSS；不能用 PNG 磁盘大小代表播放内存。

## 实际验证和已知限制

| 检查 | 已有证据与限制 |
| --- | --- |
| 原图与副本、静态输出 | 本次只读核对实际哈希与生产清单/布局报告一致；没有覆盖图片。 |
| 静态选择与缩放 | [离屏应用 smoke](../artifacts/character_assets/application_smoke/result.json)通过，使用临时 INI、内存凭据；验证原始/傲娇选择、50%～200% 比例、透明/置顶属性及缺失动作提示。它不是 Windows 物理操作实测。 |
| 真实 SAM2 API | 四帧重复静态合成图的实际 CUDA 预测及传播已运行，技术证据见上文。 |
| 两段视频提交 | **2/2 已实际使用，每个动作一次，之后不再提交或重试。** |
| 两个真实源视频 | 均已下载并核对为不同对象；各 124 帧、24 FPS、视频轨 5.166667 秒。含音频容器时长不等于视频帧时长。 |
| 真实视频 SAM2 与发布 | 两段全部 124 原生帧分别分割完成，正式目录各有 12 FPS、62 张 RGBA 帧；`complete:true`、`qa_passed:true`。 |
| 共同角色布局 | 同一安全系数已应用于 master/tsundere 与两段动作，透明画布/缩放及真实播放检查通过。 |
| 动作内容偏差 | 奶凶实际偏蹲跪，未完全趴卧；AI 身份与服装不能保证严格逐像素一致。 |
| 问号透明度 | 使用真实 SAM2 支持范围内的估算 Alpha；原生索引44的极小问号没有独立分割。 |
| 真实成品播放 smoke | [报告通过](../artifacts/character_assets_real_v1/real_playback/result.json)，覆盖两段成品、50%～200% 六档、拖动冻结/恢复、停止、回待机与保持末帧；离屏模式，未使用系统鼠标。 |
| Windows 原生挠头 | 在重新启动的实际应用中用 UI Automation 调用「播放动作」，已看到右手挠头与蓝问号；[原生桌宠中段截图](../artifacts/character_assets_real_v1/native_pet_think_middle.png)。没有把 UIA 调用写成物理鼠标点击。 |
| Windows 原生停止 | 实际应用的「停止 / 恢复」已通过 UI Automation 调用确认；这是 UIA 操作证据，不是物理鼠标或拖动验证。 |
| 尚未确认的原生操作 | 奶凶原生播放选择未得到确定验证，物理拖动也未验证；两动作及拖动/缩放/保持末帧的通过结论限于上面的真实素材离屏 smoke。 |
| 最终资产核对 | [交付核对报告](../artifacts/character_assets_real_v1/delivery_check.json)通过：124 张交付帧哈希与 RGBA、原图和两个源视频未修改，以及完整回归与真实播放报告均已核对。 |
| 本轮语法检查 | `real_v1` 阶段 `compileall` 通过。 |
| 本轮完整项目快照 | **640 项通过，237.607 秒，零跳过、零失败**；runner 墙钟 239.579 秒，见 [real_v1 报告](../artifacts/character_assets_real_v1/unittest_report.json)。实际发布素材另由播放、QA 与性能记录验证，不重复累加测试数量。 |
| 此前完整项目快照 | **627 项通过，225.833 秒，零跳过、零失败**；runner 墙钟时间 227.703 秒。见 [此前报告](../artifacts/character_assets/unittest_report.json)及[日志](../artifacts/character_assets/unittest.log)。这发生在真实视频提交前，不单独证明两动作已经交付；本轮动态验收见上面的正式报告。R8 旧 565 项同样属于先前快照，不重复累加。 |

此前应用 smoke 通过（1.709 秒），使用隔离的真实 `ApplicationController`，覆盖透明/置顶属性、六档缩放、静态选择、面板关闭重开、缺失动作降级和原图哈希；本次真实成品播放报告另外验证最终素材。生产应用已重新启动并保留运行，桌宠窗口可见、stderr 为空；原生挠头另有 UIA 与截图证据，奶凶原生播放及物理拖动不记为通过。

文档编辑、下载同一生成结果和 SAM2 本地分割不增加视频提交次数。两次提交预算现已全部使用；不会通过重新提交、自动重试或替代服务创建额外视频任务。

此前文字复核将视频提示词还原为用户指定的蓝色问号、趴低呲牙和末尾低姿态；仅修正动作显示名称后的 36 项回归通过（3.181 秒），属于早期接口证据。两条任务现均已提交，提示词不能替代对实际动作执行情况的复核。

## 本批程序、工具与测试变更

- 新增播放器：`desktop_pet/frame_animation.py`。
- 修改接入：`desktop_pet/pet_window.py`、`desktop_pet/pet_controller.py`、`desktop_pet/expression_manager.py`、`desktop_pet/ui/debug_panel.py`。
- 新增离线工具：`tools/asset_pipeline/static_assets.py`、`sequence_layout.py`、`shared_layout.py`、`sam2_video.py`、`sam2_smoke.py` 及同目录 `README.md`；新增 `requirements-assets.txt`。
- 新增验证：`tests/test_frame_animation.py`、`tests/test_static_asset_layout.py`、`tests/test_asset_sequence_pipeline.py`、`tests/test_asset_objects_and_shared_layout.py`、`tests/character_assets_smoke.py`、`tests/real_animation_smoke.py`。
- 调整已有测试夹具：smoke、application、pet、commentary、perception 和 tray 相关夹具与正式动作加载隔离，避免普通回归意外加载生产序列帧；这些已有文件并非本批全部新建，也不代表其此前改动均属于本批。
- 修改说明与排除规则：`README.md`、`.gitignore`；新增本文、[真实动作验证说明](REAL_ANIMATION_VALIDATION.md)与上表列出的 `assets/character/` 资产、清单、说明和提示词。原有其他未提交改动不计入本批变更。
