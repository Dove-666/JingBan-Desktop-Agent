@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)
package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/** A question lives in its task's message area, never as a composer footnote. */
@Composable
internal fun AgentInteractionCard(interaction: UiAgentInteraction, busy: Boolean,
    onAnswer: (String, UiAgentAnswer, (Boolean) -> Unit) -> Unit) {
    var text by rememberSaveable(interaction.id) { mutableStateOf("") }
    var answered by rememberSaveable(interaction.id) { mutableStateOf(false) }
    var error by remember(interaction.id) { mutableStateOf(false) }
    val enabled = !busy && !answered
    fun answer(value: UiAgentAnswer) {
        if (!enabled) return
        error = false
        onAnswer(interaction.id, value) { accepted -> answered = accepted; error = !accepted }
    }
    LiquidSurface(shape = RoundedCornerShape(18.dp), elevation = 1.dp,
        modifier = Modifier.fillMaxWidth().testTag("agent-interaction")) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Icon(if (answered) Icons.Default.Check else if (interaction.resource != null) Icons.Default.Lock else Icons.Default.Info,
                    contentDescription = null, modifier = Modifier.size(19.dp), tint = MaterialTheme.colorScheme.primary)
                Text(if (answered) "已提交" else if (interaction.resource != null) "需要你的授权" else "需要你确认",
                    style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
                if (busy) CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
            }
            Text(interaction.question, style = MaterialTheme.typography.bodyLarge)
            val resource = interaction.resource
            if (resource != null) {
                Text((if (resource.access == "write") "读写" else "只读") + " · " + if (resource.directory) "文件夹" else "文件",
                    style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                SelectionContainer { Text(resource.path, style = MaterialTheme.typography.bodySmall,
                    fontFamily = FontFamily.Monospace, modifier = Modifier.fillMaxWidth().testTag("agent-resource-path")) }
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { answer(UiAgentAnswer.Permission(false)) }, enabled = enabled) { Text("拒绝") }
                    Button(onClick = { answer(UiAgentAnswer.Permission(true)) }, enabled = enabled,
                        modifier = Modifier.testTag("agent-allow-access")) { Text("允许这次访问") }
                }
            } else {
                if (interaction.options.isNotEmpty()) FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    interaction.options.forEach { option ->
                        OutlinedButton(onClick = { answer(UiAgentAnswer.Text(option)) }, enabled = enabled) { Text(option) }
                    }
                }
                OutlinedTextField(value = text, onValueChange = { text = it.take(8000) },
                    enabled = enabled, modifier = Modifier.fillMaxWidth().testTag("agent-answer-input"),
                    placeholder = { Text("补充回答…") }, maxLines = 4, shape = RoundedCornerShape(12.dp))
                Button(onClick = { answer(UiAgentAnswer.Text(text.trim())) }, enabled = enabled && text.isNotBlank()) { Text("继续") }
            }
            if (error) Text("问题已更新或连接暂不可用，请检查后重试。", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error)
        }
    }
}
