# 本地只读诊断

启动遇到问题时，在项目目录运行：

```powershell
.\.venv\Scripts\python.exe -m desktop_pet.diagnostics
.\.venv\Scripts\python.exe -m desktop_pet.diagnostics --json
```

没有 `.venv` 时，也可以用 `python -m desktop_pet.diagnostics`。命令本身只依赖 Python 标准库；即使缺少 PySide6，也能列出缺失组件。

默认输出中文结论；`--json` 输出可分享的结构化报告。报告包含 Python/Qt 和关键包版本、64 位检查、必需与可选组件的存在性，以及角色 PNG 的格式、尺寸、Alpha 通道和解码结果。PySide6 不可用时，仅检查 PNG 文件头，并明确标记未完成解码。

```powershell
python -m desktop_pet.diagnostics --image .\assets\whale.png --json
```

图片检查只读取原文件，不修改、重新抠图或保存图片。正常图片使用 `QImageReader` 解码，不创建 GUI 事件循环，不加载角色窗口。

诊断不会联网，不读取 API 配置、凭据、聊天历史或屏幕内容，不初始化 SQLite、Mem0 或本地运行目录，也不加载 ONNX 模型。默认报告不包含用户目录、用户名、服务地址或窗口标题。它不会验证 API 连接、Embedding 可用性、模型准确性或桌宠是否能实际显示；依赖存在不等于连接成功。

退出码 `0` 表示本地必需项没有发现问题，`1` 表示必需依赖、Python 位数或图片存在问题。可选 Mem0、本地 Embedding 组件缺失只给出提示，不会将普通桌宠判为不可使用。

需要保存报告时，可以自行将标准输出重定向到用户选择的位置；诊断命令不会自动创建报告文件。
