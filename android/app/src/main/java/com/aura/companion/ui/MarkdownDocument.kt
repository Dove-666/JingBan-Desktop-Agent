package com.aura.companion.ui

import java.net.URI
import org.commonmark.ext.gfm.strikethrough.Strikethrough
import org.commonmark.ext.gfm.strikethrough.StrikethroughExtension
import org.commonmark.ext.gfm.tables.TablesExtension
import org.commonmark.node.*
import org.commonmark.parser.Parser

/** Parse the same CommonMark + GFM constructs used by the desktop chat. */
private val messageParser = Parser.builder().extensions(listOf(
    TablesExtension.builder().maxCells(10_000).build(), StrikethroughExtension.create(),
)).maxOpenBlockParsers(48).build()

internal fun parseMessageMarkdown(text: String): Node = try {
    messageParser.parse(text)
} catch (_: IllegalArgumentException) {
    // Resource limits must not lose the model's answer or crash a streamed turn.
    Document().apply { appendChild(Paragraph().apply { appendChild(Text(text)) }) }
}

internal fun Node.markdownChildren(): Sequence<Node> = sequence {
    var child = firstChild
    while (child != null) {
        yield(child)
        child = child.next
    }
}

internal data class MarkdownRun(
    val text: String, val bold: Boolean = false, val italic: Boolean = false,
    val strike: Boolean = false, val code: Boolean = false, val url: String? = null,
)

/** Only web links are actionable, matching desktop's external-link boundary. */
internal fun safeMarkdownUrl(value: String): String? {
    if (value.any { it <= ' ' || it == '\u007f' }) return null
    return runCatching {
        val uri = URI(value)
        value.takeIf { uri.scheme?.lowercase() in setOf("https", "http") &&
            !uri.host.isNullOrBlank() && uri.rawUserInfo == null }
    }.getOrNull()
}

private val bareWebLink = Regex("https?://[^\\s<>\\u3000，。！？、；：]+", RegexOption.IGNORE_CASE)

internal fun markdownRuns(parent: Node): List<MarkdownRun> {
    val result = mutableListOf<MarkdownRun>()
    fun appendText(value: String, style: MarkdownRun) {
        if (style.code || style.url != null) { result += style.copy(text = value); return }
        var position = 0
        for (match in bareWebLink.findAll(value)) {
            var target = match.value.trimEnd('.', ',', ';', ':', '!', '?')
            while (target.endsWith(')') && target.count { it == ')' } > target.count { it == '(' }) target = target.dropLast(1)
            val url = safeMarkdownUrl(target) ?: continue
            if (position < match.range.first) result += style.copy(text = value.substring(position, match.range.first))
            result += style.copy(text = target, url = url)
            position = match.range.first + target.length
        }
        if (position < value.length) result += style.copy(text = value.substring(position))
    }
    // Use an explicit stack: arbitrary model text cannot overflow the UI stack.
    val pending = ArrayDeque<Pair<Node, MarkdownRun>>()
    parent.markdownChildren().toList().asReversed().forEach { pending.addLast(it to MarkdownRun("")) }
    while (pending.isNotEmpty()) {
        val (node, inherited) = pending.removeLast()
        when (node) {
            is Text -> appendText(node.literal, inherited)
            is Code -> result += inherited.copy(text = node.literal, code = true)
            is SoftLineBreak -> result += inherited.copy(text = " ")
            is HardLineBreak -> result += inherited.copy(text = "\n")
            is HtmlInline, is HtmlBlock, is Image -> Unit // No HTML or remote image loading.
            else -> {
                val style = when (node) {
                    is StrongEmphasis -> inherited.copy(bold = true)
                    is Emphasis -> inherited.copy(italic = true)
                    is Strikethrough -> inherited.copy(strike = true)
                    is Link -> inherited.copy(url = safeMarkdownUrl(node.destination))
                    else -> inherited
                }
                node.markdownChildren().toList().asReversed().forEach { pending.addLast(it to style) }
            }
        }
    }
    return result
}
