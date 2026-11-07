# 鲸伴 · 问问鲸鱼娘：网页右键入口

支持 Windows 下的 Microsoft Edge 和 Google Chrome。选中网页文字，右键选择带鲸鱼娘图标的“问问鲸鱼娘”，即可在鲸伴的提问框中继续提问。选中文本和有限的页面正文会作为本次问题的参考资料；打开提问框不代表已经调用模型。

## 安装

在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/install_browser_ask.ps1 -Apply
.venv\Scripts\python.exe main.py
```

安装脚本只注册当前 Windows 用户的本地连接桥，不修改浏览器配置、不安装扩展、不需要管理员权限。

在 `edge://extensions` 或 `chrome://extensions` 开启“开发人员模式”，选择“加载解压缩的扩展”，选中本项目的 `browser-extension/whale-ask` 文件夹。然后返回普通网页，选中文字并打开右键菜单。扩展固定 ID 为 `gbeikpndpckhjeakbckijelidpdpinei`。

不带 `-Apply` 的安装或卸载命令只输出计划，不改文件或注册表。`-PrepareOnly -InstallDirectory <临时目录>` 仅生成连接文件，供检查和测试，不注册宿主。

## 使用范围

- 仅当用户点击右键菜单时，读取这次选中文字、页面标题、来源地址和最多 16000 字符正文。选中文字最多 8000 字符，请求总量最多 96 KiB。
- 不后台读取其他页面、不截图、不读取密码框或表单草稿；来源地址移除查询参数和片段，避免带入常见登录令牌。
- 页面正文排除脚本、隐藏区域、表单和可编辑区域。页面跳转或不允许读取正文时，保留选中文字并说明限制。
- 首版支持文字，不支持直接理解图片、音频、视频或扫描版 PDF。浏览器内置页面、扩展商店、部分跨域框架及其他受限页面不支持正文提取。
- 模型请求沿用桌宠已配置的 API。扩展不保存或读取 API Key，也不直接连接模型服务。
- 连接失败会打开本地说明页显示错误，不会悄悄丢弃操作。该说明页的地址不包含选中文字或页面正文。

## 卸载与回滚

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/uninstall_browser_ask.ps1 -Apply
```

脚本恢复安装前对应的当前用户宿主注册值，只删除自己生成的四个连接文件，保留目录内其他文件。若注册已被其他安装接管，停止并提示，不覆盖其他程序。随后可在浏览器扩展页手动移除“问问鲸鱼娘”。桌宠配置、聊天与记忆不受卸载影响。

项目目录移动或 `.venv` 重建后，重新执行安装命令，更新本地连接桥路径。

## 开发验证

```powershell
.venv\Scripts\python.exe -m unittest tests.test_browser_selection_extension -v
node --test --test-reporter=tap tests/browser_selection_extension.test.mjs
```

自动化测试使用模拟浏览器 API、模拟 DOM 和临时目录，不修改真实浏览器或注册表。浏览器实际加载扩展和右键菜单需要另外验证。
