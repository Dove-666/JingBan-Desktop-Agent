package com.aura.companion.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

private sealed interface MessageBlock {
    data class Prose(val text: String) : MessageBlock
    data class Heading(val text: String, val level: Int) : MessageBlock
    data class Code(val text: String, val language: String) : MessageBlock
}

/** Small local renderer: no remote content, HTML, scripts or extra dependencies. */
@Composable
internal fun MessageText(text: String) {
    val blocks = remember(text) { parseBlocks(text) }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
        blocks.forEach { block ->
            when (block) {
                is MessageBlock.Prose -> Text(inlineText(block.text), style = MaterialTheme.typography.bodyLarge)
                is MessageBlock.Heading -> Text(inlineText(block.text),
                    style = if (block.level <= 2) MaterialTheme.typography.titleLarge
                    else MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.SemiBold, modifier = Modifier.semantics { heading() })
                is MessageBlock.Code -> CodeBlock(block)
            }
        }
    }
}

private fun parseBlocks(text: String): List<MessageBlock> {
    val result = mutableListOf<MessageBlock>()
    val paragraph = mutableListOf<String>()
    var language: String? = null
    val code = mutableListOf<String>()
    fun flush() {
        if (paragraph.isNotEmpty()) { result += MessageBlock.Prose(paragraph.joinToString("\n")); paragraph.clear() }
    }
    text.lineSequence().forEach { line ->
        val trimmed = line.trimStart()
        when {
            trimmed.startsWith("```") -> {
                flush()
                if (language == null) language = trimmed.removePrefix("```").trim().take(32)
                else { result += MessageBlock.Code(code.joinToString("\n"), language.orEmpty()); code.clear(); language = null }
            }
            language != null -> code += line
            line.isBlank() -> flush()
            Regex("^#{1,6}\\s+.+").matches(trimmed) -> {
                flush()
                val level = trimmed.takeWhile { it == '#' }.length
                result += MessageBlock.Heading(trimmed.drop(level).trim(), level)
            }
            trimmed.startsWith("- ") || trimmed.startsWith("* ") -> paragraph += "• ${trimmed.drop(2)}"
            else -> paragraph += line
        }
    }
    flush()
    // Render an unfinished streamed fence as code, preserving all received text.
    if (language != null) result += MessageBlock.Code(code.joinToString("\n"), language.orEmpty())
    return result
}

@Composable
private fun inlineText(text: String): AnnotatedString {
    val codeBackground = MaterialTheme.colorScheme.surfaceVariant
    val codeForeground = MaterialTheme.colorScheme.onSurface
    return buildAnnotatedString {
        var end = 0
        Regex("`([^`\\n]+)`|\\*\\*([^*]+)\\*\\*").findAll(text).forEach { match ->
            append(text.substring(end, match.range.first))
            if (match.groups[1] != null) {
                pushStyle(SpanStyle(fontFamily = FontFamily.Monospace, fontSize = 14.sp,
                    background = codeBackground, color = codeForeground))
                append(match.groupValues[1])
            } else {
                pushStyle(SpanStyle(fontWeight = FontWeight.SemiBold))
                append(match.groupValues[2])
            }
            pop()
            end = match.range.last + 1
        }
        append(text.substring(end))
    }
}

@Composable
private fun CodeBlock(block: MessageBlock.Code) {
    Surface(shape = RoundedCornerShape(14.dp), color = MaterialTheme.colorScheme.surfaceContainerLow,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant), modifier = Modifier.fillMaxWidth()) {
        Column {
            Row(Modifier.fillMaxWidth().padding(start = 14.dp, end = 4.dp),
                verticalAlignment = Alignment.CenterVertically) {
                Text(block.language.ifBlank { "代码" }, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.weight(1f))
                CopyTextButton(block.text, "复制代码")
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            Box(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(14.dp)) {
                Text(block.text, style = MaterialTheme.typography.bodyMedium.copy(
                    fontFamily = FontFamily.Monospace, fontSize = 14.sp, lineHeight = 22.sp),
                    // Keep unusually long generated lines within Compose's
                    // representable layout size while preserving every byte.
                    softWrap = true, modifier = Modifier.widthIn(max = 4096.dp))
            }
        }
    }
}

@Composable
internal fun CopyTextButton(text: String, label: String) {
    val clipboard = LocalClipboardManager.current
    var copied by remember(text) { mutableStateOf(false) }
    LaunchedEffect(copied) { if (copied) { delay(1600); copied = false } }
    TextButton(onClick = { clipboard.setText(AnnotatedString(text)); copied = true },
        modifier = Modifier.heightIn(min = 48.dp)) {
        Text(if (copied) "已复制" else label, style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
