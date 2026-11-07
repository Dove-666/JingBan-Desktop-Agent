# 可编辑的 XLSX 公式交付

`DocumentTools.create_document(path, kind="xlsx", spec=...)` 在现有 `sheets / columns / rows` 结构中增加两种显式单元格。接口不执行 Python、Excel、LibreOffice、宏或任意公式脚本。

```json
{
  "sheets": [
    {
      "name": "明细",
      "columns": ["项目", "数量", "单价", "小计"],
      "rows": [
        ["配件", 2, {"type":"number","value":"19.95","number_format":"0.00"},
         {"type":"formula","expression":"=ROUND(B2*C2,2)","number_format":"0.00"}],
        ["包装", 1, {"type":"number","value":"0.15","number_format":"0.00"},
         {"type":"formula","expression":"=B3*C3","number_format":"0.00"}]
      ]
    },
    {
      "name": "汇总",
      "rows": [["合计", {"type":"formula","expression":"=SUM('明细'!D2:D3)","number_format":"#,##0.00"}]]
    }
  ]
}
```

`columns` 会占第 1 行，所以该例第一条数据是第 2 行。省略 `columns` 时，第一条 `rows` 数据是第 1 行。没有新增 `write_document` 工具或改变旧参数；实际已有工具名是 `create_document`。

## 闭合类型与兼容规则

- 公式：`{"type":"formula","expression":"=SUM(A1:A3)","number_format":"0.00"}`。只允许这些字段，必须提供 type/expression；不能由调用方传 cache、结果或计算成功声明。
- 精确数值：`{"type":"number","value":"19.95","number_format":"0.00"}`。value 必须是明确的十进制字符串，可用科学计数法；不会把普通字符串猜成数值。
- number_format 可省略，默认 General。小白名单为 General、0、#,##0、0%，以及这两种整数格式加 1～6 个小数零、可选百分号，例如 0.00、#,##0.00、0.0%。没有日期、条件、颜色、外部资源或任意格式代码。
- 原有 None / bool / int / float / str 单元格继续支持。普通 `"19.95"` 是文本；普通 `"=SUM(1,2)"`、HYPERLINK/DDE 样式字符串、URL、以 + / - / @ 开头的字符串也一直是 literal inlineStr，不升级为公式或超链接。
- 原有大于 15 位的整数仍拒绝，提示使用字符串保存；typed number 允许不超过 15 个有效数字，忽略尾随零。需要公式运算的财务数字请用 typed number，精度更多的档案标识请用普通文本。

## 公式子集

支持 +、-、*、/，一元正负号、括号、十进制数值、A1 / $A$1 / A$1 / $A1 引用、有限矩形范围和同一本工作簿的工作表引用。跨表可写 Data!A1 或 `'中文 表'!A1`；名称中的单引号用两个单引号表示，如 `'O''Brien'!A1`。一段范围只能属于同一工作表。

六个函数为 SUM、AVERAGE、COUNT、MIN、MAX、ROUND，不区分大小写。公式中的无意义空白在写入前移除；引用中的 `$` 和引号内工作表名称保留。

| 情况 | 本机计算语义 |
| --- | --- |
| SUM / AVERAGE / COUNT / MIN / MAX 的引用或范围含空白、文本、bool | 忽略这些非数值单元格；`"12"` 仍是文本，不隐式转数值 |
| SUM / COUNT 没有数值 | 0 |
| MIN / MAX 没有数值 | 0 |
| AVERAGE 没有数值 | FORMULA_DIVISION_BY_ZERO，拒绝整个交付 |
| 四则运算引用空白 | 按 0 计算 |
| 四则运算引用文本或 bool | FORMULA_NON_NUMERIC_REFERENCE，拒绝整个交付 |
| 范围直接作为标量运算或单元格结果 | FORMULA_RANGE_REQUIRES_AGGREGATE |
| ROUND(value,digits) | 两个数值标量，digits 为 -12～12 的整数；半值向远离零的方向舍入，例如 1.235→1.24，-1.235→-1.24 |

不支持外部工作簿、3D 引用、命名范围、DDE、HYPERLINK、WEBSERVICE、INDIRECT、动态数组、查找函数、日期函数、宏、文本公式、^、百分号运算或任意 eval。包含这些内容的 typed formula 拒绝；把它们放在普通字符串中仍只保存文本。

## 预算与原子性

每本最多 32 个工作表、50,000 个显式单元格、4,096 个公式；每表最多 100 列和 10,000 条数据行。引用限制为列 1～100、行 1～10,001；尚未提供但在界限内的单元格是空白。

工作表名、表头、普通文字、typed 数值/公式和格式字段另有累计 4 MiB UTF-8 输入预算，超过报 SPREADSHEET_INPUT_LIMIT；这一检查在整体 spec 序列化和 Workbook 构造前完成，不依赖 worker 帧或最终 ZIP 大小限制。工作表名首尾单引号拒绝，内部 O'Brien 仍支持。

每公式最多 4,096 字符、1,024 个 token/AST 节点，解析和依赖深度最多 64，每函数最多 64 个参数，每范围最多 10,000 个单元格。整个工作簿最多 100,000 个 AST/evaluation 节点和 100,000 次引用读取。数字有效指数/adjusted exponent 的绝对值不超过 300；计算使用 Decimal 34 位精度。原文档 spec 大小与输出包大小限制仍保留。

所有工作表先完成类型、函数、引用、环、零除、数值和预算验证，再生成内存中的 XLSX。计算、格式或独立包读回失败时，目标新文件不会出现，已有目标也不会被覆盖。最后仍通过 WorkspaceFiles 的授权、已观察 hash 和原子写路径发布，外部编辑的保护保持有效。

常见固定错误包括 INVALID_TYPED_CELL、INVALID_TYPED_NUMBER、INVALID_CELL_NUMBER_FORMAT、FORMULA_SYNTAX、FORMULA_FUNCTION_NOT_ALLOWED、FORMULA_UNSUPPORTED_REFERENCE、FORMULA_UNKNOWN_SHEET、FORMULA_CYCLE、FORMULA_DIVISION_BY_ZERO、FORMULA_NUMBER_LIMIT、FORMULA_RANGE_LIMIT、FORMULA_REFERENCE_LIMIT、FORMULA_COMPLEXITY_LIMIT、FORMULA_EVALUATION_LIMIT、SPREADSHEET_READBACK_FAILED。错误不包含原始单元格内容或路径。

## 计算缓存证明的范围

输出有真实 OOXML `<f>`，公式保持可编辑；对应 `<v>` 是宿主独立 Decimal DSL 算出的数值缓存，typed number 也按明确的十进制字面值写入。宿主重开 ZIP/XML，逐格核对公式、缓存、数据类型和 numFmt；测试还用 openpyxl 的 data_only=false / true 分别读回可编辑公式与缓存。

返回的 `verification.spreadsheet_calculation` 明确标记 cache_source=host_decimal_evaluator_v1、decimal_precision=34、excel_recalculated=false、visual_layout_verified=false。这证明本机子集求值和包内数据一致，**不是 Excel 或 LibreOffice 已真实重算，也不是渲染/排版验证**。Excel 的二进制浮点、极值和更大公式集合可能有不同语义；不会把这个子集描述为完整 Excel 引擎。旧 float 单元格按 openpyxl 实际保存的 16 位十进制字面值参与计算；需要明确十进制的财务数据应使用 typed number。

workbook calcPr 请求应用在打开时自动重算（fullCalcOnLoad / forceFullCalc）；这只是请求，不作为真实重算成功证据。重新用其他库保存工作簿可能丢失缓存，需重新核验最终文件。

离线回归：`agent_backend/nanobot/.venv/Scripts/python.exe -m unittest agent_backend.nanobot.tests.test_spreadsheet_formulas agent_backend.nanobot.tests.test_document_tools -v`。测试不调用 provider，不启动桌面软件，不执行表格公式之外的任意代码。

## 独立能力基准

`benchmarks/agent_tasks/xlsx_formula_oracle.py` 提供一个闭合的 `xlsx_editable_formulas / three_item_budget_round_v1` 场景：从三行 source.csv 做商品预算，保留逐行 ROUND、总计 SUM、数量/单价/金额格式，以及普通文本 `=SUM(D2:D4)`。这是这一小类可编辑表格交付的验证，不扩大为完整 Excel 编辑能力。

oracle 不导入本文生产 DSL 或 DocumentTools。它用标准库直接读取 CSV，以独立 Decimal 乘法和半值远离零舍入计算预期行金额，再逐格检查真实 ZIP/XML 公式、数值缓存、numFmt、标签和源文件字节。仅合计正确、把公式改成数字或文本、错误格式、把普通示例升级为公式、外部关系均不能通过。ZIP 解压总量/成员数/XML 大小有硬上限，重复 ZIP 成员、DTD/entity、外部关系和工作区别名拒绝。

`catalog_entry()` 只返回新条目，`validate()` 仅接受固定 kind/case/path/source_path，`evaluate(workspace, case)` 返回固定安全错误码，`write_reference_outputs()` 仅供离线自检构造最小标准 OOXML，`oracle_selfchecks()` 可重复执行正例和五类负向。参考输出和 oracle 源不进入模型可读 workspace；离线自检不算真实模型成功。旧 v2 前 29 项的语义 SHA 保持 `18fe8fe554b2b32f5305e85fa71a158eb7cef2f5d0b38fe915726662b07385a6`，新条目由 runner 显式追加。
