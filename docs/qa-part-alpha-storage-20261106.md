# R21 交互透明遮罩存储

耳朵、尾巴交互的纯透明度遮罩改为 Alpha8 保存，保留原始 8 位透明度；角色彩色图层、眼睛、部件及绘制目标继续使用原格式。修改只发生在加载器验证遮罩后，以及命中遮罩完成原有裁剪后，没有修改素材、绘制、点击或运动算法。

实际七套交互表情中，去重后的 QImage 数据从 93,283,372 降至 68,732,792 字节，减少 24,550,580 字节，约 23.41 MiB。这个数字是该组图片存储的变化，不是整个进程的内存下降或帧率提升。Qt [QImage 格式说明](https://doc.qt.io/qt-6/qimage.html#Format-enum)将 Alpha8 定义为只存透明度的 8 位格式；本轮只把它作为遮罩源，保留原有 ARGB 绘制目标。

专用素材验收从完整旧源码构建原类，与修改后的实际类对照：8,187,191 个透明度像素、49 组带眼睛底图的部件修补结果，以及 168 组眼睛与部件混合绘制均一致。开心表情没有匹配的眼睛层，仍按原图回退；其它六套使用实际匹配眼睛。原始资源合约的八项失败及修复后的三项检查分别保留，另有八项相关既有检查通过。

独立可见检查只创建一个自有 PetWindow，使用旧加载器作为基线。正常、困倦、惊讶、开心四种实际表情，在两个大小及静止、耳朵尾巴联动状态下共 16 组原生绘制逐字节相同；每组的 121 点命中采样和可见边界也相同。只抓取窗口自身，未截取用户桌面。窗口、后台任务均已关闭，源码与声明素材前后不变，模型调用为 0。

- [纯遮罩消费边界的只读实验](../artifacts/task-audit/part-alpha8-readonly-20261106/verification.json)
- [完整旧类与实际眼睛组合对照](../artifacts/task-audit/part-alpha8-implementation-20261106/actual-original-eye-comparison.json)
- [当次 11 项针对性检查](../artifacts/task-audit/part-alpha8-implementation-20261106/targeted-tests.json)
- [独立原生可见验收](../artifacts/user-testing-20261105/root-part-alpha8-visible-review/verification.json)

专用实际素材预算不会作为未来所有角色的固定上限。仓库中的持续回归改用自有临时素材验证透明度、内存布局及彩色层保留，最终三项独立通过；任意元数据文件名、软透明度、非四字节对齐宽度以及空命中遮罩均纳入这组夹具。[最终可移植检查](../artifacts/task-audit/part-alpha8-implementation-20261106/portable-resource-tests-final.json)与原实际素材检查分别记录，旧测试草案完整保留。
