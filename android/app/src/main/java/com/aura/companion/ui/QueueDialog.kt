@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@Composable
internal fun QueueDialog(items: List<UiQueueItem>, actions: UiActions, notice: String? = null,
                         directApiAvailable: Boolean = false, busy: Boolean = false,
                         onConfigureApi: () -> Unit = {}, onDismiss: () -> Unit) {
    var discarding by remember { mutableStateOf<UiQueueItem?>(null) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("待处理消息") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(16.dp)) {
                Text(if (items.any { it.isHistorySync }) "手机已经完成的对话会在电脑连接后同步。更换电脑或删除会话后，需要手动处理的内容也会保留在这里。"
                    else "未确认的请求会先核对结果，不会自动重复请求 AI。更换电脑或删除会话后，也可以在这里找回内容。",
                    style = MaterialTheme.typography.bodyMedium)
                notice?.let { Text(it, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant) }
                if (items.isEmpty()) Text("目前没有待处理消息。", color = MaterialTheme.colorScheme.onSurfaceVariant)
                LazyColumn(Modifier.fillMaxWidth().heightIn(max = 420.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp)) {
                    items(items, key = { it.requestId }) { item ->
                        var expanded by remember(item.requestId) { mutableStateOf(false) }
                        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            Text(when {
                                item.isHistorySync && item.status == QueueStatus.QUEUED -> "记录等待同步"
                                item.isHistorySync && item.status == QueueStatus.SENDING -> "正在同步记录"
                                item.isHistorySync && item.status == QueueStatus.UNKNOWN -> "同步结果待确认"
                                item.isHistorySync && item.status == QueueStatus.PENDING -> "记录等待电脑确认"
                                item.isHistorySync && item.status == QueueStatus.FAILED -> "记录同步失败"
                                item.isHistorySync && item.status == QueueStatus.NEEDS_ATTENTION -> "记录需要手动处理"
                                else -> when (item.status) {
                                    QueueStatus.QUEUED -> if (item.canSendOnPhone && directApiAvailable) "尚未发送 · 可用手机模型继续" else "等待连接"
                                    QueueStatus.SENDING -> "正在发送"
                                    QueueStatus.UNKNOWN -> "送达结果待确认"
                                    QueueStatus.PENDING -> "等待电脑回复"
                                    QueueStatus.FAILED -> "发送失败"
                                    QueueStatus.NEEDS_ATTENTION -> "需要手动处理"
                                }
                            }, style = MaterialTheme.typography.labelLarge,
                                color = if (item.status == QueueStatus.FAILED) MaterialTheme.colorScheme.error
                                else MaterialTheme.colorScheme.primary)
                            if (item.conversationTitle.isNotBlank()) Text(item.conversationTitle,
                                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Text(item.content.ifBlank { item.attachmentName?.let { "图片：$it" } ?: "待处理操作" },
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = if (expanded) Int.MAX_VALUE else 5, overflow = TextOverflow.Ellipsis)
                            if (!item.attachmentName.isNullOrBlank() && item.content.isNotBlank()) Text("图片 · ${item.attachmentName}",
                                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            if (item.detail.isNotBlank()) Text(item.detail, style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                                if (item.canSendOnPhone && item.status == QueueStatus.QUEUED && !item.isHistorySync) {
                                    TextButton({ if (directApiAvailable) actions.sendQueuedOnPhone(item.requestId) else onConfigureApi() },
                                        enabled = !busy, modifier = Modifier.heightIn(min = 48.dp)) {
                                        Text(if (directApiAvailable) "手机直接发送" else "配置模型后手机发送")
                                    }
                                }
                                if (item.content.length > 180) TextButton({ expanded = !expanded },
                                    Modifier.heightIn(min = 48.dp)) { Text(if (expanded) "收起" else "展开") }
                                CopyTextButton(item.content.ifBlank { item.attachmentName.orEmpty() }, "复制内容")
                                TextButton({ discarding = item }, Modifier.heightIn(min = 48.dp)) {
                                    Text("丢弃", color = MaterialTheme.colorScheme.error)
                                }
                            }
                            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                        }
                    }
                }
            }
        },
        confirmButton = { TextButton(onDismiss, Modifier.heightIn(min = 48.dp)) { Text("完成") } },
        shape = RoundedCornerShape(30.dp), tonalElevation = 0.dp,
        containerColor = glassSheetColor(),
    )
    discarding?.let { item ->
        AlertDialog(onDismissRequest = { discarding = null }, title = { Text("丢弃这条待处理内容？") },
            shape = RoundedCornerShape(30.dp), containerColor = glassSheetColor(), tonalElevation = 0.dp,
            text = { Text("仅取消手机上的待处理项，无法撤回电脑已经收到的内容。需要保留文字时，请先复制。") },
            confirmButton = { TextButton({ actions.discardOutbox(item.requestId); discarding = null },
                Modifier.heightIn(min = 48.dp)) { Text("确认丢弃", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton({ discarding = null }, Modifier.heightIn(min = 48.dp)) { Text("保留") } })
    }
}
