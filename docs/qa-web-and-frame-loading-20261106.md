# 网页编码与动画加载检查

## 显式 HTTP 字符集

正式网页工具原先固定按 UTF-8 解码。自有响应中，UTF-8 对照正确，但显式 GBK/GB18030 的中文标题、正文和链接文字乱码，仍被当成成功结果缓存。现只对原已接受的文本响应处理唯一普通 HTTP charset：UTF8、GB18030、GBK/cp936，以及明确采用 GBK 网页兼容映射的 GB2312。JSON 保持 UTF-8。

未知、重复、扩展、续段和不安全名称回到原 UTF-8 replace，不调用任意 codec 搜索或转换。普通参数仍用标准库解析；窄词法扫描防止 RFC2231 未编码续段被合成普通字符串，引号内的伪参数不会误判。没有增加 HTML/XML BOM 或 meta 嗅探，也没有改变地址验证、原始字节上限、原始哈希、链接提取、分页或缓存。

新增 13 项及既有 10 项通过，原三种响应的冻结夹具重放也通过。独立另外六个实际 reader 检查覆盖 GB18030 四字节汉字/emoji、引号和转义、多份 Content-Type、XML GBK 扩展字、JSON 误标及危险标签保持字面值；分页、链接、raw hash、一次抓取缓存和关闭正确。HTTP/DNS 为受控替身，真实地址校验执行，网络/浏览器/模型均为零。[修复与原失败](../artifacts/user-testing-20261105/public-web-charset-fix/review.md) [独立检查](../artifacts/user-testing-20261105/task-audit/public-web-charset-independent.json)

编码标签参考 [WHATWG](https://encoding.spec.whatwg.org/#names-and-labels)，参数行为参考 [Python Message](https://docs.python.org/3.10/library/email.compat32-message.html#email.message.Message.get_params)，JSON 规则参考 [RFC8259](https://www.rfc-editor.org/rfc/rfc8259.html#section-8.1)。这是有限显式头支持，不宣称完整浏览器编码算法或所有旧编码覆盖。原 R20 官方网页 Agent 任务的实际失败不因这些离线结果变为成功。

随后对 [Python 官方 urllib.request 页面](https://docs.python.org/3/library/urllib.request.html)做一次实际正式 reader 读取：HTTP 200，UTF-8，218816 原始字节，54444 字符分 4 页到尾，首分页重复读取命中相同缓存。页面 GET 恰好一次，现有公开 DNS resolver 的 A/AAAA 查询各一次，三条连接都关闭；未保存完整文章或使用模型。单次耗时约 0.875 秒只描述本次检查，不作为普遍性能指标。[实际读取报告](../artifacts/user-testing-20261105/public-web-official-after-charset/verification-v2.json)

首次旁观测试在 HTTPS 构造前因测试包装替换标准库类名触发递归，尚未发出 HTTPS 请求，已独立保存为失败。纯构造探针确认原因后，只修复测试方法包装才进行上述一次真实抓取；没有切换网络设置、改生产代码或重试真实网络失败。此结果证明当前网页读取链，仍不证明原模型 Agent 任务已经交付。[原失败](../artifacts/user-testing-20261105/public-web-official-after-charset/verification.json)

## 单次动画加载内共享

常规帧有界读取压缩 PNG，使用原 QImageReader 解码与尺寸检查。同一加载内仅完整 payload 相同、已验证的图片才共享底层数据，各逻辑槽仍有独立 QImage wrapper；修改一槽会分离，不影响其余槽。所有槽、尺寸/透明校验、顺序和时序保留，压缩索引内容上限为 4 MiB。预算满、超大文件或特殊路径仍能沿原读取路径加载，不引入全局缓存或有损重采样。

独立属性探针发现 QBuffer 最初丢失文件名带来的 `@2x/@3x` DPR，原失败保留。最终完整路径含 `@` 时保留原文件读取，不仿写 Qt 受文件名、first-dot 和环境变量影响的行为。新加载器的独立六组、16 槽对照中，像素、文本、ICC、DPI、offset、DPR 和修改隔离均与旧实现一致。[属性检查](../artifacts/user-testing-20261105/frame-loader-reader-properties/actual-loader-after.json)

| 素材 | 原 source 图片数据 | 新 source 图片数据 |
| --- | --- | --- |
| puff，74 槽 | 296 MiB | 204 MiB |
| sleepy，30 槽 | 120 MiB | 16 MiB |
| work，68 槽 | 272 MiB | 268 MiB |

这是 source 独立图片 buffer 的数据量。sleepy 的平滑处理原本已经共享多数重复槽，处理后的 32 MiB 变为 28 MiB，不能把 source 的 104 MiB 差值称为播放稳态节省。六个独立自有进程的单样本加载峰值中，puff 约 348.5→261.0 MiB、sleepy 约 172.2→69.6 MiB；work 约 324.5→325.4 MiB，略高约 0.9 MiB。压缩索引及临时对象有开销，没有测出所有场景都改善峰值。[逐阶段测量、限制及原失败](../artifacts/user-testing-20261105/frame-loader-intern/review.md)

新 12 项与相关旧 7 项通过。根另外用实际桌宠、控制器、工作动画与原生绘制做 16 项判断：普通聊天不启动打字、任务启动真实素材、68 槽保留、100%/150% 抽样源像素一致、五轮循环不重新加载，注入到自有窗口的 Qt 鼠标事件能冻结/恢复动画，拖拽中停止会等实际返回，再清空播放数据并保留新的放置位置。截图已查看，最终窗口和 worker 关闭。没有移动系统鼠标、捕获其他窗口或修改用户桌宠。[实际可见检查](../artifacts/user-testing-20261105/root-frame-loader-visible-review/verification.json)

动画与网页检查范围分别保留，不合并为模型任务成功率。所有素材哈希保持，各轮夹具错误、DPR 反例和首版字符集续段失败均保留。通用生成代码执行门仍关闭；这两项不改变 Agent 权限或未知效果处理规则。
