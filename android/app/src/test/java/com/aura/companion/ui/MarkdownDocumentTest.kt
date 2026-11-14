package com.aura.companion.ui

import org.commonmark.ext.gfm.tables.*
import org.commonmark.node.*
import org.junit.Assert.*
import org.junit.Test

class MarkdownDocumentTest {
    private fun text(node: Node) = markdownRuns(node).joinToString("") { it.text }

    @Test fun screenshotStyleQuoteKeepsOneQuoteAndThreeParagraphsWithoutRawMarkers() {
        val input = "**故事梗概**（剧透预警）：\n\n> 老李去参加同学会，\n> 偷偷租了豪车。\n>\n> 这时，助理推门进来。\n>\n> 而那家公司老板，是张小满。\n\n笑点落在真诚上。"
        val blocks = parseMessageMarkdown(input).markdownChildren().toList()
        assertEquals(3, blocks.size)
        assertTrue(blocks[1] is BlockQuote)
        val paragraphs = blocks[1].markdownChildren().toList()
        assertEquals(3, paragraphs.size)
        assertTrue(paragraphs.all { it is Paragraph })
        assertEquals("老李去参加同学会， 偷偷租了豪车。", text(paragraphs[0]))
        assertTrue(paragraphs.none { text(it).contains('>') })
        assertEquals("笑点落在真诚上。", text(blocks[2]))
    }

    @Test fun nestedListsKeepTheirStructureAndOrderedStart() {
        val doc = parseMessageMarkdown("3. 第一项\n   - 子项目\n   - [x] 已完成\n4. 第二项\n\n---\n\n## 标题")
        val blocks = doc.markdownChildren().toList()
        val list = blocks[0] as OrderedList
        assertEquals(3, list.markerStartNumber)
        val items = list.markdownChildren().toList()
        assertEquals(2, items.size)
        val nested = items[0].lastChild as BulletList
        assertEquals(2, nested.markdownChildren().count())
        assertEquals("[x] 已完成", text(nested.lastChild.firstChild))
        assertTrue(blocks[1] is ThematicBreak)
        assertEquals(2, (blocks[2] as Heading).level)
    }

    @Test fun tableCellsKeepEscapedPipesInlineCodeAndAlignment() {
        val table = parseMessageMarkdown("| 名称 | 数量 |\n| :--- | ---: |\n| a\\|b | `2` |\n").firstChild as TableBlock
        val rows = table.markdownChildren().flatMap { it.markdownChildren() }.toList()
        val header = rows[0].markdownChildren().filterIsInstance<TableCell>().toList()
        assertTrue(header.all { it.isHeader })
        assertEquals(TableCell.Alignment.RIGHT, header[1].alignment)
        val data = rows[1].markdownChildren().toList()
        assertEquals("a|b", text(data[0]))
        assertTrue(markdownRuns(data[1]).single().code)
    }

    @Test fun inlineStylesAndReferenceLinksAreParsedWithoutRewritingOriginalInput() {
        val input = "**加粗且*斜体***、~~删除~~、`a > b`、[文档][ref]。\n\n[ref]: https://example.com/docs"
        val runs = markdownRuns(parseMessageMarkdown(input).firstChild)
        assertTrue(runs.any { it.text == "斜体" && it.bold && it.italic })
        assertTrue(runs.any { it.text == "删除" && it.strike })
        assertTrue(runs.any { it.text == "a > b" && it.code })
        assertEquals("https://example.com/docs", runs.single { it.text == "文档" }.url)
        assertTrue(input.contains("**")) // Rendering is a projection; copy still receives the source.
    }

    @Test fun linksOnlyOpenWebUrlsAndNeverExecuteHtmlOrFetchImages() {
        for (url in listOf("javascript:alert(1)", "file:///sdcard/test", "intent://x", "https://user:pass@example.com/", "https://example.com/a\nb", "data:text/html,test")) {
            assertNull(safeMarkdownUrl(url))
        }
        assertEquals("https://example.com/a?q=1", safeMarkdownUrl("https://example.com/a?q=1"))
        val document = parseMessageMarkdown("可见 <b>文本</b> [别点](javascript:alert) ![远程图片](https://example.com/track.png)\n\n<script>hidden()</script>")
        val runs = markdownRuns(document)
        assertEquals("可见 文本 别点 ", runs.joinToString("") { it.text })
        assertTrue(runs.all { it.url == null })
        val plain = markdownRuns(parseMessageMarkdown("见 https://example.com/a_(b)。结束").firstChild)
        assertEquals("https://example.com/a_(b)", plain.single { it.url != null }.url)
    }

    @Test fun unfinishedStreamFencesRemainCodeAndLaterCloseWithoutLosingTheReply() {
        val first = parseMessageMarkdown("先看代码：\n\n```python\nprint('hello')\n").lastChild as FencedCodeBlock
        assertEquals("python", first.info)
        assertEquals("print('hello')\n", first.literal)
        val complete = parseMessageMarkdown("先看代码：\n\n```python\nprint('hello')\n```\n\n> 完成。")
        assertTrue(complete.lastChild is BlockQuote)
        assertEquals("完成。", text(complete.lastChild.firstChild))
    }

    @Test(timeout = 10_000) fun longStreamAndDeepFormattingDoNotOverflowTheRendererStack() {
        val input = ("正常段落 **强调** [链接](https://example.com)\n\n").repeat(300)
        for (end in 100 until input.length step 500) {
            val node = parseMessageMarkdown(input.substring(0, end))
            assertTrue(markdownRuns(node).isNotEmpty())
        }
        val nested = parseMessageMarkdown("> ".repeat(500) + "正文")
        assertTrue(markdownRuns(nested).joinToString("") { it.text }.contains("正文"))
    }
}
