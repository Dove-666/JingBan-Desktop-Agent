# 启动图标

当前选用用户确认的 **P01 经典笑脸**，来自 `concepts/2026-11-15-pixel/P01-classic.png`。`whale-maid-pixel.png` 是该透明 PNG 的原样副本；不是桌宠动画或聊天角色立绘。

- Windows：`windows/whale-maid.ico` 包含 16、20、24、32、40、48、64、96、128、256 像素尺寸，用于程序、安装器、桌面快捷方式和托盘。
- Android：前景为相同 P01 的尺寸适配版本；自适应图标使用简洁浅色背景，并留出系统裁切空间。
- 历史选稿保留在 `concepts/`，不会加入 Windows 运行包。旧的 `android/whale-maid-icon-v1.png` 是历史来源，不再代表当前启动图标。

Windows 格式导出：在仓库根目录运行 `.venv/Scripts/python.exe tools/export_launcher_icon.py`。原始图案与透明底保持不变，仅转换尺寸和 ICO 格式。

生成方式、参考图和提示词见 `concepts/2026-11-15-pixel/prompts.json`。原图由内置 image_gen 生成，P01 于 2026-11-15 选定。
