@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import android.graphics.BitmapFactory
import android.net.Uri
import com.aura.companion.BuildConfig
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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
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
        shape = RoundedCornerShape(30.dp),
        tonalElevation = 0.dp,
        containerColor = glassSheetColor(),
    )
}

@Composable
internal fun ConnectionSheet(state: UiState, actions: UiActions, onDismiss: () -> Unit,
                             onQueue: () -> Unit, onPair: () -> Unit, onConfigureApi: () -> Unit,
                             onCharacterCards: () -> Unit = {}) {
    var themeMenu by remember { mutableStateOf(false) }
    val themes = listOf(ThemeMode.SYSTEM to "跟随系统", ThemeMode.LIGHT to "浅色", ThemeMode.DARK to "深色")
    val themeLabel = themes.first { it.first == state.theme }.second
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = glassSheetColor(),
        contentColor = MaterialTheme.colorScheme.onSurface,
        shape = RoundedCornerShape(topStart = 32.dp, topEnd = 32.dp), tonalElevation = 0.dp,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).testTag("settings-sheet")
            .padding(start = 20.dp, end = 20.dp, bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(20.dp)) {
            Text("设置", style = MaterialTheme.typography.headlineSmall,
                modifier = Modifier.semantics { heading() })
            LiquidSurface(shape = RoundedCornerShape(20.dp), modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(horizontal = 16.dp, vertical = 4.dp)) {
                    Row(Modifier.fillMaxWidth().heightIn(min = 72.dp).testTag("connection-setting"),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Icon(DesktopMonitorIcon, null, Modifier.size(22.dp))
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            Text(state.desktopName.ifBlank { "我的电脑" },
                                style = MaterialTheme.typography.titleMedium,
                                maxLines = 1, overflow = TextOverflow.Ellipsis)
                            Text(when (state.connection) {
                                ConnectionState.CONNECTED -> "在线"
                                ConnectionState.CONNECTING -> "连接中"
                                ConnectionState.UNAUTHORIZED -> "需要重新配对"
                                ConnectionState.ERROR -> "连接失败"
                                ConnectionState.DISCONNECTED -> if (state.paired) "离线" else "未连接"
                            }, style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        when {
                            state.connection == ConnectionState.CONNECTING ->
                                TextButton(actions::disconnect, modifier = Modifier.heightIn(min = 48.dp)) {
                                    Text("取消")
                                }
                            !state.paired || state.connection == ConnectionState.UNAUTHORIZED ->
                                TextButton(onPair, modifier = Modifier.heightIn(min = 48.dp)) {
                                    Text(if (state.paired) "重新配对" else "连接")
                                }
                            state.connection == ConnectionState.CONNECTED ->
                                TextButton(actions::disconnect, modifier = Modifier.heightIn(min = 48.dp)) {
                                    Text("断开")
                                }
                            else -> TextButton(actions::reconnect, modifier = Modifier.heightIn(min = 48.dp)) {
                                Text("连接")
                            }
                        }
                    }
                    if (state.paired && state.connection != ConnectionState.UNAUTHORIZED) {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                            if (state.connection == ConnectionState.CONNECTED && state.sync == SyncState.ERROR) {
                                TextButton(actions::syncNow, modifier = Modifier.heightIn(min = 48.dp)) {
                                    Text("重试同步")
                                }
                            }
                            TextButton(onPair, modifier = Modifier.heightIn(min = 48.dp)) { Text("更换配对") }
                        }
                    }
                    Row(Modifier.fillMaxWidth().heightIn(min = 72.dp).testTag("model-setting"),
                        verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            Text("模型与 API", style = MaterialTheme.typography.titleMedium)
                            Text(if (state.directApi.available) state.directApi.model.ifBlank { "未配置" } else "未配置",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                        TextButton(onConfigureApi, modifier = Modifier.heightIn(min = 48.dp)) {
                            Text("编辑")
                        }
                    }
                    Surface(onClick = onCharacterCards, color = Color.Transparent,
                        shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth().testTag("character-setting")) {
                        Row(Modifier.fillMaxWidth().heightIn(min = 72.dp), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                                Text("角色卡", style = MaterialTheme.typography.titleMedium)
                                Text(state.activeCharacterName.ifBlank { "鲸鱼娘" },
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis)
                            }
                            Icon(Icons.Default.KeyboardArrowRight, contentDescription = null,
                                tint = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.size(22.dp))
                        }
                    }
                    if (state.queue.isNotEmpty()) {
                        Row(Modifier.fillMaxWidth().heightIn(min = 56.dp).testTag("pending-messages-setting"),
                            verticalAlignment = Alignment.CenterVertically) {
                            Text("待处理消息", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                            TextButton(onQueue, modifier = Modifier.heightIn(min = 48.dp)) {
                                Text("查看 (${state.queue.size})")
                            }
                        }
                    }
                    if (state.paired) {
                        Row(Modifier.fillMaxWidth().heightIn(min = 56.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text("任务完成提醒", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                            Switch(checked = state.taskNotificationsEnabled, onCheckedChange = actions::setTaskNotifications,
                                modifier = Modifier.semantics { contentDescription = "电脑任务完成提醒" })
                        }
                    }
                    Row(Modifier.fillMaxWidth().heightIn(min = 56.dp).testTag("appearance-setting"),
                        verticalAlignment = Alignment.CenterVertically) {
                        Text("外观", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                        Box {
                            TextButton(onClick = { themeMenu = true }, modifier = Modifier.heightIn(min = 48.dp).semantics {
                                contentDescription = "选择外观"
                                stateDescription = themeLabel
                            }) {
                                Text(themeLabel)
                                Spacer(Modifier.width(4.dp))
                                Icon(Icons.Default.KeyboardArrowDown, null, Modifier.size(18.dp))
                            }
                            DropdownMenu(expanded = themeMenu, onDismissRequest = { themeMenu = false },
                                shape = RoundedCornerShape(16.dp), containerColor = glassSheetColor()) {
                                themes.forEach { (mode, label) ->
                                    DropdownMenuItem(text = { Text(label) },
                                        onClick = { themeMenu = false; actions.setTheme(mode) })
                                }
                            }
                        }
                    }
                }
            }
            Text(BuildConfig.VERSION_NAME, style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.align(Alignment.CenterHorizontally).testTag("app-version"))
        }
    }
}

@Composable
internal fun PetSheet(state: UiState, actions: UiActions, onDismiss: () -> Unit) {
    val connected = state.connection == ConnectionState.CONNECTED
    var scale by remember(state.petScale) { mutableFloatStateOf(state.petScale.toFloat()) }
    var say by rememberSaveable { mutableStateOf("") }
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = glassSheetColor(),
        contentColor = MaterialTheme.colorScheme.onSurface,
        shape = RoundedCornerShape(topStart = 32.dp, topEnd = 32.dp), tonalElevation = 0.dp,
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
internal fun ImagesSheet(images: List<UiImage>, actions: UiActions, notice: String? = null,
                         onPick: () -> Unit = actions::pickImage, onDismiss: () -> Unit) {
    var deleting by remember { mutableStateOf<UiImage?>(null) }
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = glassSheetColor(),
        contentColor = MaterialTheme.colorScheme.onSurface,
        shape = RoundedCornerShape(topStart = 32.dp, topEnd = 32.dp), tonalElevation = 0.dp,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
        Column(Modifier.fillMaxWidth().heightIn(max = 720.dp)
            .padding(horizontal = 24.dp).padding(bottom = 24.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { SheetHeading("图片收件箱", "已保存的图片文件，可查看或删除。") }
                IconButton(actions::refreshImages, Modifier.size(48.dp)) {
                    Icon(Icons.Default.Refresh, contentDescription = "刷新图片列表")
                }
            }
            notice?.let { Spacer(Modifier.height(12.dp)); InlineNotice(it) }
            OutlinedButton(onPick, modifier = Modifier.fillMaxWidth().padding(vertical = 16.dp)
                .heightIn(min = 48.dp), shape = RoundedCornerShape(14.dp)) {
                Icon(Icons.Default.Add, null, Modifier.size(20.dp)); Spacer(Modifier.width(8.dp)); Text("选图并返回聊天")
            }
            if (images.isEmpty()) Text("还没有保存的图片。\n选张图片，回到聊天中提问吧。",
                style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(vertical = 28.dp))
            LazyColumn(Modifier.fillMaxWidth().weight(1f, fill = false).heightIn(max = 480.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp), contentPadding = PaddingValues(bottom = 16.dp)) {
                items(images, key = { it.id }) { image ->
                    LiquidSurface(shape = RoundedCornerShape(24.dp),
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
            Text("单张不超过 4 MiB。识图请在聊天中附图后点击发送。", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    deleting?.let { image ->
        AlertDialog(onDismissRequest = { deleting = null }, title = { Text("删除这张图片？") },
            shape = RoundedCornerShape(30.dp), containerColor = glassSheetColor(), tonalElevation = 0.dp,
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
    LiquidSurface(tint = MaterialTheme.colorScheme.primaryContainer, shape = RoundedCornerShape(18.dp),
        modifier = Modifier.fillMaxWidth().semantics { liveRegion = LiveRegionMode.Polite }) {
        Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onPrimaryContainer,
            modifier = Modifier.padding(14.dp))
    }
}

/** Decode local thumbnails off the UI thread, bounded to 512 px per side. */
@Composable
internal fun LocalImage(uri: String, modifier: Modifier, description: String,
                        expandToAvailableWidth: Boolean = false) {
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
    if (expandToAvailableWidth) {
        BoxWithConstraints(modifier.background(MaterialTheme.colorScheme.surfaceVariant),
            contentAlignment = Alignment.Center) {
            bitmap?.let { image ->
                // The sampled bitmap's intrinsic height is expressed in px.
                // Size the message frame in dp from its real aspect ratio,
                // then Fit the entire image into it without cropping/stretching.
                val ratio = image.width.toFloat() / image.height.toFloat()
                val frameHeight = (maxWidth / ratio).coerceIn(120.dp, 360.dp)
                Image(image, description, Modifier.fillMaxWidth().height(frameHeight), contentScale = ContentScale.Fit)
            } ?: Text("图片", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    } else {
        Box(modifier.background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
            if (bitmap != null) Image(requireNotNull(bitmap), description, Modifier.fillMaxSize(), contentScale = ContentScale.Fit)
            else Text("图片", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
