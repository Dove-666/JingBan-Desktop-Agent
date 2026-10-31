# 动作成品目录：已发布

本目录已发布两段真实 SAM2 视频分割成品。每段均为 62 张 1024×1024 RGBA 帧、12 FPS、5167ms，`complete:true`、`qa_passed:true`；两段实际源视频均为 24 FPS、124 帧且已全部分割。预算 2/2、实际合计3元，不再提交或重试。来源和生成偏差见[生产清单](../production_manifest.json)。

已交付结构：

```text
animations/
  think_scratch_head/
    animation.json
    qa.json
    frames/
      0001.png
      ... 0062.png
  snarl_cute/
    animation.json
    qa.json
    frames/
      0001.png
      ... 0062.png
```

每帧都是完整的 1024×1024 RGBA 画布，共享底部锚点 `(512,960)`；不得逐帧裁切并重新居中。交付帧数、FPS 与时长必须反映真实序列，缺少尾帧或未通过视觉验收时保留草稿，不能只把 `complete` 改成 `true`。

「思考 · 挠头」单次播放后回到静态待机（`completion:idle`）；「趴下 · 奶凶呲牙」单次播放后保持末帧（`hold_last`），用「停止 / 恢复」回静态并释放帧缓存。两段都不循环。

已知视觉限制：奶凶实际偏蹲跪、未完全趴卧；问号淡出 Alpha 根据真实 SAM2 支持区域估算，原生索引44的极小问号没有独立 mask，未补画替代。源视频经 AI 生成，不保证与参考图严格逐像素一致。

清单字段、来源、QA 和调试面板操作见 [角色资产说明](../../../docs/CHARACTER_ASSETS.md)。处理已有视频的工具见 [SAM2 管线](../../../tools/asset_pipeline/README.md)。
