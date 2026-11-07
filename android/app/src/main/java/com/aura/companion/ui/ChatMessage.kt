@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

private val ReplyCopyIcon by lazy {
    ImageVector.Builder("ReplyCopy", 24.dp, 24.dp, 24f, 24f).apply {
        path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.65f,
            strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round) {
            moveTo(8f, 8f); lineTo(19f, 8f); quadTo(20f, 8f, 20f, 9f)
            lineTo(20f, 20f); quadTo(20f, 21f, 19f, 21f); lineTo(8f, 21f)
            quadTo(7f, 21f, 7f, 20f); lineTo(7f, 9f); quadTo(7f, 8f, 8f, 8f); close()
            moveTo(16f, 4f); lineTo(5f, 4f); quadTo(4f, 4f, 4f, 5f); lineTo(4f, 16f)
        }
    }.build()
}

@Composable
internal fun MessageRow(message: UiMessage, onRetry: (String) -> Unit,
                        onOpenImage: (String) -> Unit, phoneAvailable: Boolean) {
    val user = message.role == MessageRole.USER
    val reply = displayedChatReply(message)
    val colors = MaterialTheme.colorScheme
    Column(Modifier.fillMaxWidth().testTag("message-${message.id}"),
        horizontalAlignment = if (user) Alignment.End else Alignment.Start) {
        if (message.role == MessageRole.ASSISTANT && message.execution == null) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                AuraMark(Modifier.size(18.dp))
                Text("鲸伴", style = MaterialTheme.typography.labelMedium, color = colors.onSurfaceVariant)
            }
            Spacer(Modifier.height(8.dp))
        }
        message.execution?.let { process ->
            ExecutionProcess(process, Modifier.fillMaxWidth(), detail = executionMessageDetail(message))
            if (reply.isNotBlank()) Spacer(Modifier.height(8.dp))
        }
        val body: @Composable ColumnScope.() -> Unit = {
            if (message.attachmentUri != null || message.attachmentId != null) {
                Surface(onClick = { message.attachmentId?.let(onOpenImage) },
                    enabled = message.attachmentId != null,
                    color = colors.surfaceContainerLow, shape = RoundedCornerShape(14.dp),
                    modifier = Modifier.fillMaxWidth().semantics { contentDescription = "预览消息图片" }) {
                    message.attachmentUri?.let { uri ->
                        LocalImage(uri, Modifier.fillMaxWidth().heightIn(min = 120.dp, max = 360.dp),
                            "消息中的图片", expandToAvailableWidth = true)
                    } ?: Box(Modifier.fillMaxWidth().heightIn(min = 160.dp).padding(20.dp),
                        contentAlignment = Alignment.Center) {
                        Text("点按查看图片", style = MaterialTheme.typography.bodyMedium,
                            color = colors.onSurfaceVariant)
                    }
                }
                if (reply.isNotBlank()) Spacer(Modifier.height(8.dp))
            }
            if (reply.isNotBlank()) SelectionContainer {
                if (message.role == MessageRole.ASSISTANT) MessageText(reply)
                else Text(reply, style = if (message.role == MessageRole.SYSTEM)
                    MaterialTheme.typography.bodyMedium else MaterialTheme.typography.bodyLarge,
                    color = if (message.role == MessageRole.SYSTEM) colors.onSurfaceVariant else colors.onSurface)
            }
        }
        if (user) BoxWithConstraints(Modifier.fillMaxWidth(), contentAlignment = Alignment.TopEnd) {
            val bubbleMaxWidth = minOf(maxWidth * .88f, 640.dp)
            Surface(color = colors.primaryContainer.copy(alpha = .42f),
                shape = RoundedCornerShape(22.dp, 22.dp, 6.dp, 22.dp),
                modifier = Modifier.widthIn(max = bubbleMaxWidth).testTag("message-bubble-${message.id}")) {
                Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp)
                    .testTag("message-body-${message.id}"), content = body)
            }
        } else Column(Modifier.fillMaxWidth().testTag("message-body-${message.id}"), content = body)
        if (message.role == MessageRole.ASSISTANT && reply.isNotBlank()) {
            ReplyCopyButton(message.content)
        }
        val retryOnPhone = phoneAvailable && message.retryOnPhone && message.status == MessageStatus.QUEUED
        // Execution rows own their outcome and detail; do not duplicate a failed
        // request label or the same explanation under the assistant answer.
        if (message.execution == null) {
            val delivery = when (message.status) {
                MessageStatus.QUEUED -> "待发送"
                MessageStatus.SENDING -> "正在发送…"
                MessageStatus.PENDING -> if (message.desktopAgent) "正在处理…" else "正在回复…"
                MessageStatus.FAILED -> if (message.desktopAgent) "未完成" else "发送失败"
                MessageStatus.UNCERTAIN -> "送达待确认"
                MessageStatus.SENT -> ""
            }
            var showDetail by rememberSaveable(message.id) { mutableStateOf(false) }
            val hasDetail = message.detail.isNotBlank()
            if (delivery.isNotBlank() || hasDetail || message.retryable || retryOnPhone) {
                Row(Modifier.padding(top = 4.dp), verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (delivery.isNotBlank()) Text(delivery, style = MaterialTheme.typography.bodySmall,
                        color = colors.onSurfaceVariant,
                        modifier = Modifier.padding(end = 4.dp).testTag("message-delivery-state"))
                    if (hasDetail) TextButton({ showDetail = !showDetail }, Modifier.heightIn(min = 48.dp)) {
                        Text(if (showDetail) "收起详情" else "详情", style = MaterialTheme.typography.bodySmall,
                            color = colors.onSurfaceVariant)
                    }
                    if (message.retryable || retryOnPhone) TextButton({ onRetry(message.id) },
                        Modifier.heightIn(min = 48.dp)) {
                        Text(if (retryOnPhone) "手机直接发送" else "重试")
                    }
                }
                if (showDetail && hasDetail) SelectionContainer {
                    Text(message.detail, style = MaterialTheme.typography.bodySmall,
                        color = colors.onSurfaceVariant,
                        modifier = Modifier.padding(top = 4.dp).testTag("message-detail-${message.id}"))
                }
            }
        } else if (message.retryable || retryOnPhone) {
            // Only the repository-projected retry permission exposes an action;
            // a terminal Agent outcome alone never authorizes replay.
            TextButton({ onRetry(message.id) }, Modifier.heightIn(min = 48.dp)) {
                Text(if (retryOnPhone) "手机直接发送" else "重试")
            }
        }
    }
}

@Composable
private fun ReplyCopyButton(text: String) {
    val clipboard = LocalClipboardManager.current
    var copied by remember(text) { mutableStateOf(false) }
    LaunchedEffect(copied) { if (copied) { delay(1600); copied = false } }
    IconButton(onClick = { clipboard.setText(AnnotatedString(text)); copied = true },
        modifier = Modifier.size(48.dp).testTag("copy-reply")) {
        Icon(if (copied) Icons.Default.Check else ReplyCopyIcon,
            contentDescription = if (copied) "已复制" else "复制回复",
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(16.dp))
    }
}
