# 鲸鱼娘白块、白边与视频色彩修复

本轮修复针对垂发内部残留的白色背景、角色外轮廓白边，以及视频角色颜色偏暗。保留原角色、原视频和修改前版本，优先修复现有素材，没有重新提交视频生成任务。

## 素材在哪里

完整美术包已发布到 [美术资产/鲸鱼娘_抠图修复_v2](../美术资产/鲸鱼娘_抠图修复_v2/README.md)，运行素材也已同步更新。目录如下：

```text
美术资产/鲸鱼娘_抠图修复_v2/
  原图_未修改.png
  鲸鱼娘_透明背景_修复_v2.png
  傲娇_透明背景_修复_v2.png
  桌宠画布/
    master.png
    tsundere.png
  动画/
    think_scratch_head/
      animation.json
      qa.json
      frames/
    snarl_cute/
      animation.json
      qa.json
      frames/
  源视频/
  制作记录/
    修复前版本/
```

完整分辨率静态修复源与桌宠运行画布分开保存。桌宠读取 `assets/character/master.png`、`assets/character/static/tsundere.png` 和 `assets/character/animations/` 下的两组动作；它们对应美术包里的运行画布和动画副本。

## 正常立绘如何修复

本次以用户指定的桌面文件 `蓝发鲸鱼少女女仆立绘.png` 为准。原文件为 1254×1254 RGB，SHA256 为 `0160d77f0cdce6f5f170ae7ee209430277e272ab3fb1062f8e1549b10abb2738`。

先使用内置图像模型生成透明蒙版，再将蒙版与指定原图组合。模型生成结果的颜色没有直接用作成品颜色，角色内部采用原图 RGB；仅对透明边缘做白底反合成，以减少白边。

蒙版的低透明度散点被清除，接近不透明的内部归一为完全不透明。原图中与外部背景相连的近白区域经过连通分析清理；白头饰、围裙和袜子由原画轮廓保护，不能通过全局删除白色的方式处理。原始细黑描边在蒙版附近局部恢复，并对紧邻外部背景的灰色抗锯齿边缘调整透明度。

独立审查通过的完整分辨率候选为 `制作记录/原色还原_去白边_候选3.png`。其 772,982 个完全不透明像素的 RGB 与指定原图逐像素一致，RGB 平均绝对误差为 0。该保证针对修复源；1024×1024 运行画布经过正常缩放采样，像素值会随插值改变。

## 傲娇立绘如何修复

傲娇立绘保留原有生成源 `assets/character/source/tsundere-generated.png`，SHA256 为 `d16880f55daedd2ab23920767b465ededd79d6910a6907a80562816a522a26c9`。

本轮曾生成新的傲娇蒙版，但它没有正确清除两处左侧发孔；后续试验也出现了头饰误透明。因此这些候选被拒绝并留在制作记录中，没有作为最终素材使用。

通过审查的版本使用原始傲娇 RGBA 的透明通道，仅清除低透明度噪点，并在左侧两处已确认的发孔局部处理残留白背景。连通处理限定在原图坐标 `x=100～260、y=600～910`，种子为 `(230, 660)` 和 `(140, 855)`，没有对头饰进行全局白色清理。

完整分辨率通过审查的候选为 `制作记录/傲娇_保留原蒙版_孔洞修复_候选2.png`。其 745,772 个完全不透明像素的 RGB 与原傲娇源逐像素一致，RGB 平均绝对误差为 0。

## 视频如何处理

沿用已有的 `think_scratch_head` 与 `snarl_cute` 两个原始视频，不重新生成视频，也没有新增视频任务或改写生成提示词。源 MP4 应保存在美术包的 `源视频/` 中，原文件内容不变。

两段视频使用真实 SAM2 跟踪发丝间的背景空隙，处理全部 248 个原生帧，再修正对应的透明蒙版，最后各交付 62 帧、12 FPS 的运行序列。颜色调整采用每段固定的蓝色调色板变换，以原图色彩为参考、75% 保守混合，排除皮肤、白衣和黑色描边，避免逐帧自动曝光带来的明暗闪动。它是对已有视频的色彩校正，不表示视频 RGB 能逐像素恢复成静态原图。

在两段视频已跟踪发孔区域中，跨帧累计的不透明白背景像素从 369,888 降至 0；受保护的蓝发核心透明度不变，皮肤强红区域没有透明度改动，原生问号层保持此前审查版本。该统计针对已跟踪区域，不能替代整个角色所有边缘的目检。完整方法、校色参数和逐段检查结果见 `artifacts/asset_pipeline/hair_repair_v2/final_repair_report.json`；它保留了候选发布前的阶段状态，最终运行包验证见下方的实际播放报告。

两组动作保持 1024×1024 透明画布、共同的角色缩放标准和底部锚点。每组使用同一套整体定位变换，不能逐帧裁切后重新放大。

## 保留哪些文件

- 指定桌面 RGB 原图保留，另存 `原图_未修改.png` 副本。
- `assets/whale.png` 与 `assets/character/source/master-original.png` 保留；它们是此前的透明源，与本次指定 RGB 原图不是同一文件。
- `assets/character/source/tsundere-generated.png`、两份原始 MP4 保留。
- 修改前的运行静态图片与动画保留在制作记录的修复前版本中。
- 试验候选、被拒绝的蒙版与复核预览保留，便于追溯，不会把失败候选伪装为最终成品。
- 用户此前已删除的旧美术文件不恢复。

## 如何检查

先在深色、浅色和粉色背景下查看角色，再放大发孔和外轮廓。正常立绘重点检查原图坐标 `(220,650)`、`(140,855)`、`(950,610)` 的发孔与 `(570,1100)` 的腿间空隙；这些位置应透明。白头饰 `(550,65)`、`(600,70)`，围裙 `(640,900)` 及白袜 `(520,1090)`、`(610,1090)` 应保留。

傲娇重点检查两处左发孔 `(230,660)`、`(140,855)`，以及左上头饰 `(350,180)`、`(400,140)`、`(450,120)` 是否完整。

发布后重启桌宠，再在“调试面板 → 角色控制 → 角色素材与序列帧”中切换正常、傲娇与两段动作。“重新检查素材”可以更新素材列表，但当前运行实例缓存了默认静态角色，不能替代重启来完整加载新 `master.png`。

```powershell
.\.venv\Scripts\python.exe main.py --debug-panel
```

验证动画时，同时检查浅底、深底、角色缩放、拖动时暂停以及停止后恢复静态角色。视频的原始动作仍受已有生成结果限制：奶凶动作实际是蹲低，并非完全趴卧。

## 本轮验证记录

已完成正常候选3和傲娇候选2的独立像素检查，以及深色、浅色、粉色背景整图和局部放大审查。检查确认发孔透明、白色衣饰保留，以及上述不透明区域 RGB 与各自原图一致。

审查报告位于：

- `artifacts/asset_pipeline/hair_repair_v2/static_independent_qa_candidate3/report.json`
- `artifacts/asset_pipeline/hair_repair_v2/tsundere_independent_qa_candidate2/report.json`

最终正常、傲娇静态画布与两组修复动画已发布到运行路径，并复制到美术包。修改前素材与清单位于 `制作记录/修复前版本/`，对应 `backup_inventory.json` 保留校验信息。

本轮对以下六个模块运行了隔离回归：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest -v `
  tests.test_static_asset_layout tests.test_asset_sequence_pipeline `
  tests.test_asset_objects_and_shared_layout tests.test_frame_animation `
  tests.test_pet_controller tests.test_debug_ui
Remove-Item Env:\QT_QPA_PLATFORM
```

实际结果为 **77 项通过，3.902 秒，0 失败、0 跳过**。命令、工作目录与结果保存在 `artifacts/asset_pipeline/hair_repair_v2/targeted_execution_report.json`，完整日志为同目录的 `targeted.log`。

随后使用正式 `assets/character/animations/` 中的两组修复素材运行真实播放检查：

```powershell
.\.venv\Scripts\python.exe -m tests.real_animation_smoke `
  --directory assets/character/animations `
  --names think_scratch_head snarl_cute --expect-frames 62 `
  --output artifacts/asset_pipeline/hair_repair_v2/real_playback
```

实际退出码 0，**两组均通过，墙钟用时 21.890 秒**。检查涵盖真实 PNG 加载、缓存切帧、Qt 拖动时冻结、平滑恢复、50%～200% 六档缩放、停止后恢复静态角色，以及挠头动作回到待机、奶凶动作保持真实末帧。检查期间原始 `whale.png`、正式动画帧与清单的哈希保持不变。

两段动作首次加载和安装缓存分别为 0.9213 秒、0.8780 秒；本轮所有检查阶段的最大 GUI 心跳间隔为 31.333ms，峰值进程 RSS 为 495.465MiB。两段短时稳定播放采样的进程 CPU 分别为单核口径 5.19%、6.92%，不能解释为整机长期占用。每段原始解码数据为 248MiB，缩放缓存和程序本身还会占用内存。

播放报告与截图位于 `artifacts/asset_pipeline/hair_repair_v2/real_playback/`，完整命令和墙钟记录见 `real_playback_execution_report.json`。这些检查使用临时配置、内存凭据及本进程的 offscreen Qt 窗口，没有使用物理鼠标或操作生产桌宠；它们不能单独证明原生 Windows 合成、多显示器和物理拖动已经通过。此前的 640 项测试属于上一批交付，本轮没有重复运行全套测试，也没有将旧结果冒充为本次修复验证。

主流程另外优雅重启了实际桌宠，确认修复版正常立绘已经加载，左侧发丝孔洞能透出桌面；通过 UI Automation 调用播放后也观察到了修复版奶凶末姿，并确认停止后恢复正常立绘。记录为 [正常立绘](../artifacts/asset_pipeline/hair_repair_v2/native_corrected_pet_verified.png)与[奶凶末姿](../artifacts/asset_pipeline/hair_repair_v2/native_corrected_snarl_observed.png)，启动 stderr 为空。物理拖动与原生多显示器没有重新验收。

最终发布、备份哈希及运行时/美术包一致性以 [本轮交付核对](../artifacts/asset_pipeline/hair_repair_v2/repair_delivery_check.json)和美术包内 `制作记录/修复发布检查.json` 为准。`final_repair_report.json` 是视频子流程的候选审查记录，其中“不新增图片”仅指视频修复子流程；本轮静态图使用了两次内置图像蒙版请求，其中傲娇新蒙版未采用，没有新增视频任务。
