@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@Composable
internal fun QueueDialog(items: List<UiQueueItem>, actions: UiActions, notice: String? = null, onDismiss: () -> Unit) {
    var discarding by remember { mutableStateOf<UiQueueItem?>(null) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("待处理消息") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(16.dp)) {
                Text("未确认的请求会先核对结果，不会自动重复请求 AI。更换电脑或删除会话后，也可以在这里找回内容。",
                    style = MaterialTheme.typography.bodyMedium)
                notice?.let { Text(it, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant) }
                if (items.isEmpty()) Text("目前没有待处理消息。", color = MaterialTheme.colorScheme.onSurfaceVariant)
                LazyColumn(Modifier.fillMaxWidth().heightIn(max = 420.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp)) {
                    items(items, key = { it.requestId }) { item ->
                        var expanded by remember(item.requestId) { mutableStateOf(false) }
                        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                            Text(when (item.status) {
                                QueueStatus.QUEUED -> "等待连接"
                                QueueStatus.SENDING -> "正在发送"
                                QueueStatus.UNKNOWN -> "送达结果待确认"
                                QueueStatus.PENDING -> "等待电脑回复"
                                QueueStatus.FAILED -> "发送失败"
                                QueueStatus.NEEDS_ATTENTION -> "需要手动处理"
                            }, style = MaterialTheme.typography.labelLarge,
                                color = if (item.status == QueueStatus.FAILED) MaterialTheme.colorScheme.error
                                else MaterialTheme.colorScheme.primary)
                            if (item.conversationTitle.isNotBlank()) Text(item.conversationTitle,
                                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            Text(item.content.ifBlank { item.attachmentName?.let { "图片：$it" } ?: "待处理操作" },
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = if (expanded) Int.MAX_VALUE else 5, overflow = TextOverflow.Ellipsis)
                            if (item.detail.isNotBlank()) Text(item.detail, style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
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
        containerColor = MaterialTheme.colorScheme.surface,
    )
    discarding?.let { item ->
        AlertDialog(onDismissRequest = { discarding = null }, title = { Text("丢弃这条待处理内容？") },
            text = { Text("仅取消手机上的待处理项，无法撤回电脑已经收到的内容。需要保留文字时，请先复制。") },
            confirmButton = { TextButton({ actions.discardOutbox(item.requestId); discarding = null },
                Modifier.heightIn(min = 48.dp)) { Text("确认丢弃", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton({ discarding = null }, Modifier.heightIn(min = 48.dp)) { Text("保留") } })
    }
}
