# R21 原生控件实测

本轮完成一次真实 Windows UI Automation 生产后端检查。测试只创建自有 UUID 标题窗口及临时文件，使用 `DesktopControls`、`PowerShellUIABackend` 和正式 `windows_uia.ps1`，没有替换控件属性、原生引用或前台信息。

启动程序路径、固定夹具脚本、进程及启动时间均核对，正常激活自有窗口一次，并在每项操作前后确认仍为该窗口。生产工具完成九次读取或操作调用，其中两次输入为填写文本和点击保存；独立控件值核验通过，自有文件的 45 字节 UTF-8 内容及散列也与预期一致。

测试用时 4.578 秒，真实模型调用为 0。测试窗口及九个已记录的辅助进程均关闭；未完整证明每个辅助进程的所有后代已收束，报告保留该边界。没有截图、系统剪贴板、外部窗口激活或真实桌宠重启。

- [实测原始报告](../artifacts/user-testing-20261105/production-uia-owned-once-20261106.json)
- [当次固定范围的实测脚本](../artifacts/user-testing-20261105/production-uia-owned-once-20261106.py)

之前因初始前台条件不足而停止的检查保持原始状态，新结果没有覆盖它。Windows 的普通前台切换有系统约束，见 [Microsoft SetForegroundWindow 文档](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)；本轮没有修改这些约束。文本填写使用真实控件支持的 [ValuePattern.SetValue](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.valuepattern.setvalue)，没有用键盘模拟绕过不支持的控件。

此次只证明自有测试窗口的生产工具链能够读取、填写和保存，不是模型规划、手机远程连接、任意应用或完整 Agent 任务的成功证明。应用内任意 Python/终端执行仍保持关闭。
