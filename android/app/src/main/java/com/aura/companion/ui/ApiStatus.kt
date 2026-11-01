package com.aura.companion.ui

import android.net.Uri
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

@Composable
internal fun ApiSettingsCard(api: UiDirectApi, onEdit: () -> Unit) {
    var detailsExpanded by rememberSaveable { mutableStateOf(false) }
    LiquidSurface(shape = RoundedCornerShape(20.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("模型与 API", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                TextButton(onEdit, Modifier.heightIn(min = 48.dp)) { Text(if (api.available) "编辑" else "配置模型") }
            }
            Text(api.model.ifBlank { "尚未配置模型" }, style = MaterialTheme.typography.bodyLarge)
            val sourceLabel = when (api.source) {
                ApiConfigSource.LOCAL -> "本机配置"
                ApiConfigSource.DESKTOP -> "电脑同步"
                ApiConfigSource.NONE -> "尚未配置"
            }
            Text("$sourceLabel · ${if (api.available) "已配置" else "待配置"}",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (api.sync == ApiConfigSyncState.ERROR) Text(api.configStatus,
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
            TextButton(onClick = { detailsExpanded = !detailsExpanded }, modifier = Modifier.heightIn(min = 48.dp)) {
                Text(if (detailsExpanded) "收起配置详情" else "配置详情")
                Icon(if (detailsExpanded) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown, null)
            }
            if (detailsExpanded) {
                Text(if (api.keySaved) "密钥已安全保存" else "尚未保存密钥", style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                safeApiAddress(api.baseUrl).takeIf { it.isNotBlank() }?.let {
                    Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (api.sync != ApiConfigSyncState.ERROR) Text(api.configStatus,
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (api.lastSyncedAt.isNotBlank()) Text("配置更新时间 · ${api.lastSyncedAt}",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (api.message.isNotBlank()) Text(api.message, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

private val UiDirectApi.configStatus: String get() = when (sync) {
    ApiConfigSyncState.SYNCING -> "API 配置正在自动同步"
    ApiConfigSyncState.READY -> if (source == ApiConfigSource.LOCAL) "手机配置已保存" else "API 配置已自动同步"
    ApiConfigSyncState.ERROR -> if (available) "使用已保存配置 · 同步待重试" else "API 配置同步未完成"
    ApiConfigSyncState.WAITING -> if (available) "使用已保存的 API 配置" else "配置手机模型后即可开始聊天"
}

/** Strip credentials and query tokens even if a provider puts them in its URL. */
internal fun safeApiAddress(value: String): String = runCatching {
    if (value.isBlank()) return@runCatching ""
    val uri = Uri.parse(value)
    val scheme = uri.scheme?.lowercase().orEmpty()
    if (scheme !in setOf("http", "https") || uri.host.isNullOrBlank()) return@runCatching ""
    val host = requireNotNull(uri.host).let { if (':' in it && !it.startsWith('[')) "[$it]" else it }
    val port = uri.port.takeIf { it >= 0 }?.let { ":$it" }.orEmpty()
    "$scheme://$host$port${uri.encodedPath.orEmpty()}"
}.getOrDefault("")
