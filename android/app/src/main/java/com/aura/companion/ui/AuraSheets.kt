@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import android.graphics.BitmapFactory
import android.net.Uri
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import kotlin.math.max
import kotlin.math.roundToInt

@Composable
internal fun PairingDialog(state: UiState, actions: UiActions, initialPayload: String = "", onDismiss: () -> Unit) {
    // Keep temporary authorization material out of saved-instance state.
    var payload by remember(initialPayload) { mutableStateOf(initialPayload) }
    AlertDialog(onDismissRequest = onDismiss,
        icon = { AuraMark(Modifier.size(40.dp)) },
        title = { Text("连接你的电脑") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("在电脑端点击「复制配对信息」，粘贴完整链接。5 分钟内有效。")
                OutlinedTextField(payload, { payload = it.take(32768) },
                    label = { Text("完整配对链接") },
                    supportingText = { Text("以 aura://pair/ 开头，也支持完整 JSON；不要只填短码。") },
                    isError = state.pairingError != null, minLines = 3, maxLines = 6,
                    enabled = !state.pairing, shape = RoundedCornerShape(14.dp),
                    modifier = Modifier.fillMaxWidth())
                state.pairingError?.let { Text(it, color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
                if (state.pairing) Row(horizontalArrangement = Arrangement.spacedBy(10.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    Text("正在安全连接…", style = MaterialTheme.typography.bodyMedium)
                }
            }
        },
        confirmButton = {
            Button(onClick = { actions.pair(payload.trim()) }, enabled = payload.isNotBlank() && !state.pairing,
                modifier = Modifier.heightIn(min = 48.dp)) { Text(if (state.pairing) "连接中" else "连接电脑") }
        },
        dismissButton = { TextButton(onDismiss, modifier = Modifier.heightIn(min = 48.dp)) {
            Text(if (state.pairing) "在后台连接" else "暂时跳过") } },
        containerColor = MaterialTheme.colorScheme.surface,
    )
}

@Composable
internal fun ConnectionSheet(state: UiState, actions: UiActions, onDismiss: () -> Unit,
                             onQueue: () -> Unit, onPair: () -> Unit) {
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = MaterialTheme.colorScheme.surface,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())
            .padding(start = 24.dp, end = 24.dp, bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(18.dp)) {
            SheetHeading("连接与外观", "你的对话，留在自己的设备。")
            state.notice?.let { InlineNotice(it) }
            Surface(color = MaterialTheme.colorScheme.surfaceContainerLow,
                shape = RoundedCornerShape(18.dp), modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        Icon(Icons.Default.Home, null, Modifier.size(22.dp))
                        Text(if (state.paired) state.desktopName else "还没有连接电脑",
                            style = MaterialTheme.typography.titleMedium)
                    }
                    Text(when (state.connection) {
                        ConnectionState.CONNECTED -> "已连接 · " + when (state.sync) {
                            SyncState.SYNCING -> "正在同步对话"
                            SyncState.READY -> "对话已同步"
                            SyncState.ERROR -> "同步需要重试"
                            else -> "可以开始聊天"
                        }
                        ConnectionState.CONNECTING -> "正在连接电脑…"
                        ConnectionState.UNAUTHORIZED -> "配对已失效，请在电脑端重新生成配对信息。"
                        else -> if (state.paired) "当前离线，历史记录仍可查看。" else "完成一次配对，以后打开即可继续。"
                    }, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (state.connectionDetail.isNotBlank()) Text(state.connectionDetail,
                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (state.queuedCount > 0) Text("${state.queuedCount} 条消息待发送",
                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.primary)
                }
            }
            if (state.queue.isNotEmpty()) TextButton(onQueue, Modifier.heightIn(min = 48.dp)) {
                Text("查看待处理内容 (${state.queue.size})")
            }
            if (state.paired) {
                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedButton(actions::reconnect, enabled = state.connection != ConnectionState.CONNECTING,
                        modifier = Modifier.weight(1f).heightIn(min = 48.dp)) { Text("重新连接") }
                    OutlinedButton(actions::disconnect, modifier = Modifier.weight(1f).heightIn(min = 48.dp)) {
                        Text("暂时断开")
                    }
                }
            }
            TextButton(onPair, modifier = Modifier.heightIn(min = 48.dp)) {
                Icon(Icons.Default.Add, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp))
                Text(if (state.paired) "使用新的配对信息" else "连接我的电脑")
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            Text("外观", style = MaterialTheme.typography.titleMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(ThemeMode.SYSTEM to "跟随系统", ThemeMode.LIGHT to "浅色", ThemeMode.DARK to "深色")
                    .forEach { (mode, label) ->
                        FilterChip(selected = state.theme == mode, onClick = { actions.setTheme(mode) },
                            label = { Text(label) }, modifier = Modifier.heightIn(min = 48.dp))
                    }
            }
            Text("电脑需保持开机联网。离线消息保存在手机，重新连接后补发；不确定是否送达的消息会先核对结果。",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
internal fun PetSheet(state: UiState, actions: UiActions, onDismiss: () -> Unit) {
    val connected = state.connection == ConnectionState.CONNECTED
    var scale by remember(state.petScale) { mutableFloatStateOf(state.petScale.toFloat()) }
    var say by rememberSaveable { mutableStateOf("") }
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = MaterialTheme.colorScheme.surface,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())
            .padding(start = 24.dp, end = 24.dp, bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(20.dp)) {
            SheetHeading("桌宠控制", if (connected) "给桌面上的它，一个小小的回应。" else "电脑当前离线，连接后再试。")
            state.notice?.let { InlineNotice(it) }
            Text("换个表情", style = MaterialTheme.typography.titleMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("neutral" to "日常", "happy" to "开心", "surprised" to "惊讶",
                    "sleepy" to "困倦", "thinking" to "思考", "angry" to "生气").forEach { (value, label) ->
                    OutlinedButton(onClick = { actions.petCommand("expression", PetValue.Text(value)) },
                        enabled = connected, shape = RoundedCornerShape(14.dp),
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                        modifier = Modifier.heightIn(min = 48.dp)) { Text(label) }
                }
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            Column {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("桌宠大小", style = MaterialTheme.typography.titleMedium)
                    Text("${scale.roundToInt()}%", style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Slider(value = scale, onValueChange = { scale = it },
                    onValueChangeFinished = { actions.petCommand("scale", PetValue.Number(scale.roundToInt())) },
                    enabled = connected, valueRange = 50f..200f, steps = 29,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                        .semantics { contentDescription = "桌宠大小百分比" })
            }
            Row(Modifier.fillMaxWidth().heightIn(min = 48.dp), verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("显示桌宠", style = MaterialTheme.typography.titleMedium)
                    Text("在电脑桌面上显示", style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Switch(checked = state.petVisible, onCheckedChange = { actions.petCommand("visibility", PetValue.Toggle(it)) },
                    enabled = connected, modifier = Modifier.semantics { contentDescription = "显示桌宠" })
            }
            OutlinedTextField(say, { say = it.take(1000) }, label = { Text("让桌宠说句话") },
                supportingText = { Text("显示在桌面气泡中，不会请求 AI。") },
                modifier = Modifier.fillMaxWidth(), minLines = 2, maxLines = 4,
                shape = RoundedCornerShape(14.dp))
            Button(onClick = { actions.petCommand("say", PetValue.Text(say.trim())); say = "" },
                enabled = connected && say.isNotBlank(), modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                shape = RoundedCornerShape(14.dp)) { Text("送到桌面") }
        }
    }
}

@Composable
internal fun ImagesSheet(images: List<UiImage>, actions: UiActions, notice: String? = null, onDismiss: () -> Unit) {
    var deleting by remember { mutableStateOf<UiImage?>(null) }
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = MaterialTheme.colorScheme.surface,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().heightIn(max = 720.dp)
            .padding(horizontal = 24.dp).padding(bottom = 24.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { SheetHeading("图片收件箱", "传递一张图片，分享眼前的一刻。") }
                IconButton(actions::refreshImages, Modifier.size(48.dp)) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新图片列表")
                }
            }
            notice?.let { Spacer(Modifier.height(12.dp)); InlineNotice(it) }
            OutlinedButton(actions::pickImage, modifier = Modifier.fillMaxWidth().padding(vertical = 16.dp)
                .heightIn(min = 48.dp), shape = RoundedCornerShape(14.dp)) {
                Icon(Icons.Default.Add, null, Modifier.size(20.dp)); Spacer(Modifier.width(8.dp)); Text("从手机选择图片")
            }
            if (images.isEmpty()) Text("你发送的图片会出现在这里。\n目前还没有图片。",
                style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(vertical = 28.dp))
            LazyColumn(Modifier.fillMaxWidth().weight(1f, fill = false).heightIn(max = 480.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp), contentPadding = PaddingValues(bottom = 16.dp)) {
                items(images, key = { it.id }) { image ->
                    Surface(shape = RoundedCornerShape(16.dp), color = MaterialTheme.colorScheme.surfaceContainerLow,
                        modifier = Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(14.dp)) {
                            Row(horizontalArrangement = Arrangement.spacedBy(12.dp),
                                verticalAlignment = Alignment.CenterVertically) {
                                image.localUri?.let { LocalImage(it, Modifier.size(64.dp).clip(RoundedCornerShape(10.dp)),
                                    "${image.filename}的缩略图") }
                                Column(Modifier.weight(1f)) {
                                    Text(image.filename, style = MaterialTheme.typography.titleMedium,
                                        maxLines = 2, overflow = TextOverflow.Ellipsis)
                                    if (image.sizeLabel.isNotBlank()) Text(image.sizeLabel,
                                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    if (image.status.isNotBlank()) Text(image.status,
                                        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                                }
                            }
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                                TextButton({ actions.openImage(image.id) }, Modifier.heightIn(min = 48.dp)) { Text("查看图片") }
                                if (image.canDelete) TextButton({ deleting = image }, Modifier.heightIn(min = 48.dp)) {
                                    Text("删除", color = MaterialTheme.colorScheme.error)
                                }
                            }
                        }
                    }
                }
            }
            Text("单张不超过 4 MiB。接收图片不会自动识图。", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    deleting?.let { image ->
        AlertDialog(onDismissRequest = { deleting = null }, title = { Text("删除这张图片？") },
            text = { Text("${image.filename}\n删除后将从图片收件箱移除。") },
            confirmButton = { TextButton({ actions.deleteImage(image.id); deleting = null },
                Modifier.heightIn(min = 48.dp)) { Text("删除图片", color = MaterialTheme.colorScheme.error) } },
            dismissButton = { TextButton({ deleting = null }, Modifier.heightIn(min = 48.dp)) { Text("保留") } })
    }
}

@Composable
private fun SheetHeading(title: String, subtitle: String) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(title, style = MaterialTheme.typography.titleLarge, modifier = Modifier.semantics { heading() })
        Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun InlineNotice(text: String) {
    Surface(color = MaterialTheme.colorScheme.primaryContainer, shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite }) {
        Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onPrimaryContainer,
            modifier = Modifier.padding(14.dp))
    }
}

/** Decode local thumbnails off the UI thread, bounded to 512 px per side. */
@Composable
internal fun LocalImage(uri: String, modifier: Modifier, description: String) {
    val context = LocalContext.current
    val bitmap by produceState<ImageBitmap?>(initialValue = null, uri) {
        value = withContext(Dispatchers.IO) {
            runCatching {
                fun open() = if (Uri.parse(uri).scheme.isNullOrEmpty()) File(uri).inputStream()
                    else requireNotNull(context.contentResolver.openInputStream(Uri.parse(uri)))
                val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                open().use { BitmapFactory.decodeStream(it, null, bounds) }
                require(bounds.outWidth > 0 && bounds.outHeight > 0)
                var sample = 1
                while (max(bounds.outWidth, bounds.outHeight) / sample > 512) sample *= 2
                val options = BitmapFactory.Options().apply { inSampleSize = sample }
                open().use { BitmapFactory.decodeStream(it, null, options) }?.asImageBitmap()
            }.getOrNull()
        }
    }
    Box(modifier.background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
        if (bitmap != null) Image(requireNotNull(bitmap), description, Modifier.fillMaxSize(), contentScale = ContentScale.Fit)
        else Text("图片", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
