package com.aura.companion.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.*
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import org.commonmark.ext.gfm.tables.*
import org.commonmark.node.*
import org.commonmark.node.Paragraph as MarkdownParagraph

/** Native, selectable Markdown. Model text never becomes a WebView or HTML. */
@Composable
internal fun MessageText(text: String) {
    val document = remember(text) { parseMessageMarkdown(text) }
    MarkdownBlocks(document, Modifier.fillMaxWidth())
}

@Composable
private fun MarkdownBlocks(parent: Node, modifier: Modifier = Modifier, compact: Boolean = false) {
    Column(modifier, verticalArrangement = Arrangement.spacedBy(if (compact) 6.dp else 12.dp)) {
        parent.markdownChildren().forEach { block -> MarkdownBlock(block) }
    }
}

@Composable
private fun MarkdownBlock(block: Node) {
    when (block) {
        is MarkdownParagraph -> InlineMarkdown(block)
        is Heading -> InlineMarkdown(block,
            style = when (block.level) {
                1 -> MaterialTheme.typography.headlineSmall
                2 -> MaterialTheme.typography.titleLarge
                else -> MaterialTheme.typography.titleMedium
            }.copy(fontWeight = FontWeight.SemiBold),
            modifier = Modifier.padding(top = 4.dp).semantics { heading() })
        is BlockQuote -> {
            val lineColor = MaterialTheme.colorScheme.outlineVariant
            CompositionLocalProvider(LocalContentColor provides MaterialTheme.colorScheme.onSurfaceVariant) {
                MarkdownBlocks(block, Modifier.fillMaxWidth().testTag("markdown-quote")
                    .drawBehind { drawLine(lineColor, Offset(1.5.dp.toPx(), 0f),
                        Offset(1.5.dp.toPx(), size.height), 3.dp.toPx()) }
                    .padding(start = 14.dp, top = 2.dp, bottom = 2.dp))
            }
        }
        is FencedCodeBlock -> CodeBlock(block.literal.removeSuffix("\n"), block.info.orEmpty().substringBefore(' ').take(32))
        is IndentedCodeBlock -> CodeBlock(block.literal.removeSuffix("\n"), "")
        is BulletList -> MarkdownList(block, null)
        is OrderedList -> MarkdownList(block, block.markerStartNumber)
        is ThematicBreak -> HorizontalDivider(Modifier.padding(vertical = 4.dp), color = MaterialTheme.colorScheme.outlineVariant)
        is TableBlock -> MarkdownTable(block)
        is HtmlBlock, is LinkReferenceDefinition -> Unit
        else -> if (block.firstChild != null) MarkdownBlocks(block, compact = true)
    }
}

@Composable
private fun InlineMarkdown(node: Node, modifier: Modifier = Modifier,
                           style: TextStyle = MaterialTheme.typography.bodyLarge,
                           dropPrefix: Int = 0, textAlign: TextAlign? = null) {
    val colors = MaterialTheme.colorScheme
    val uriHandler = LocalUriHandler.current
    val runs = remember(node) { markdownRuns(node) }
    val content = remember(runs, colors, uriHandler, dropPrefix) {
        buildAnnotatedString {
            var remainingDrop = dropPrefix
            runs.forEach { run ->
                val value = run.text.drop(remainingDrop)
                remainingDrop = (remainingDrop - run.text.length).coerceAtLeast(0)
                if (value.isNotEmpty()) {
                    val span = SpanStyle(
                        fontWeight = if (run.bold) FontWeight.SemiBold else null,
                        fontStyle = if (run.italic) FontStyle.Italic else null,
                        textDecoration = if (run.strike) TextDecoration.LineThrough else null,
                        fontFamily = if (run.code) FontFamily.Monospace else null,
                        background = if (run.code) colors.surfaceVariant else androidx.compose.ui.graphics.Color.Unspecified,
                    )
                    pushStyle(span)
                    if (run.url != null) {
                        pushLink(LinkAnnotation.Url(run.url, TextLinkStyles(style = SpanStyle(
                            color = colors.primary, textDecoration = TextDecoration.Underline)),
                            linkInteractionListener = { runCatching { uriHandler.openUri(run.url) } }))
                        append(value); pop()
                    } else append(value)
                    pop()
                }
            }
        }
    }
    Text(content, modifier, style = style, textAlign = textAlign)
}

@Composable
private fun MarkdownList(list: Node, start: Int?) {
    Column(Modifier.fillMaxWidth().testTag("markdown-list"), verticalArrangement = Arrangement.spacedBy(5.dp)) {
        list.markdownChildren().forEachIndexed { index, item ->
            val first = item.firstChild
            val plainStart = first?.let { markdownRuns(it).joinToString("") { run -> run.text } }.orEmpty()
            val task = Regex("^\\[([ xX])]\\s+").find(plainStart)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.Top) {
                when {
                    task != null -> Checkbox(checked = task.groupValues[1].equals("x", true), onCheckedChange = null,
                        modifier = Modifier.size(22.dp))
                    start != null -> Text("${start + index}.", style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.widthIn(min = 22.dp), textAlign = TextAlign.End)
                    else -> Text("•", style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.width(16.dp), textAlign = TextAlign.Center)
                }
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    item.markdownChildren().forEach { child ->
                        if (child === first && task != null && child is MarkdownParagraph) InlineMarkdown(child, dropPrefix = task.value.length)
                        else MarkdownBlock(child)
                    }
                }
            }
        }
    }
}

@Composable
private fun MarkdownTable(block: TableBlock) {
    val rows = remember(block) {
        block.markdownChildren().flatMap { it.markdownChildren() }.filterIsInstance<TableRow>()
            .map { it.markdownChildren().filterIsInstance<TableCell>().toList() }.toList()
    }
    val count = rows.maxOfOrNull { it.size } ?: return
    if (count == 0) return
    val outline = MaterialTheme.colorScheme.outlineVariant
    BoxWithConstraints(Modifier.fillMaxWidth().testTag("markdown-table")) {
        val cellWidth = (maxWidth / count.coerceAtMost(3)).coerceIn(120.dp, 240.dp)
        Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).border(1.dp, outline, RoundedCornerShape(10.dp))
            .horizontalScroll(rememberScrollState())) {
            Column(Modifier.width(cellWidth * count)) {
                rows.forEach { row ->
                    Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min)) {
                        row.forEach { cell ->
                            Surface(color = if (cell.isHeader) MaterialTheme.colorScheme.surfaceContainerLow else androidx.compose.ui.graphics.Color.Transparent,
                                modifier = Modifier.width(cellWidth).fillMaxHeight().border(.5.dp, outline)) {
                                InlineMarkdown(cell, Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
                                    style = MaterialTheme.typography.bodyMedium.copy(
                                        fontWeight = if (cell.isHeader) FontWeight.SemiBold else FontWeight.Normal),
                                    textAlign = when (cell.alignment) {
                                        TableCell.Alignment.CENTER -> TextAlign.Center
                                        TableCell.Alignment.RIGHT -> TextAlign.End
                                        else -> TextAlign.Start
                                    })
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun CodeBlock(text: String, language: String) {
    Surface(shape = RoundedCornerShape(14.dp), color = MaterialTheme.colorScheme.surfaceContainerLow,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant), modifier = Modifier.fillMaxWidth().testTag("markdown-code")) {
        Column {
            Row(Modifier.fillMaxWidth().padding(start = 14.dp, end = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(language.ifBlank { "代码" }, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
                CopyTextButton(text, "复制代码")
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            Box(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(14.dp)) {
                Text(text, style = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace,
                    fontSize = 14.sp, lineHeight = 22.sp), softWrap = true, modifier = Modifier.widthIn(max = 4096.dp))
            }
        }
    }
}

@Composable
internal fun CopyTextButton(text: String, label: String) {
    val clipboard = LocalClipboardManager.current
    var copied by remember(text) { mutableStateOf(false) }
    LaunchedEffect(copied) { if (copied) { delay(1600); copied = false } }
    TextButton(onClick = { clipboard.setText(AnnotatedString(text)); copied = true }, modifier = Modifier.heightIn(min = 48.dp)) {
        Text(if (copied) "已复制" else label, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
