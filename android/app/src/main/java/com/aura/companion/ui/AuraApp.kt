@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

private enum class AppSheet { CONNECTION, PET, IMAGES }

@Composable
fun AuraApp(state: UiState, actions: UiActions) {
    AuraTheme(state.theme) {
        val drawer = rememberDrawerState(DrawerValue.Closed)
        val scope = rememberCoroutineScope()
        val snackbars = remember { SnackbarHostState() }
        var sheet by remember { mutableStateOf<AppSheet?>(null) }
        var showPairing by rememberSaveable { mutableStateOf(false) }
        var showQueue by rememberSaveable { mutableStateOf(false) }
        var pairPrefill by remember { mutableStateOf("") }
        var draft by rememberSaveable(state.selectedConversationId) { mutableStateOf("") }
        var pairingObserved by remember { mutableStateOf(false) }
        LaunchedEffect(state.pendingPairInfo) {
            state.pendingPairInfo?.takeIf { it.isNotBlank() }?.let {
                pairPrefill = it
                showPairing = true
                actions.consumePairInfo()
            }
        }
        LaunchedEffect(state.notice) {
            state.notice?.takeIf { it.isNotBlank() }?.let {
                snackbars.showSnackbar(it, actionLabel = "知道了", duration = SnackbarDuration.Long)
                actions.dismissNotice()
            }
        }
        LaunchedEffect(state.pairing, state.paired, state.pairingError) {
            if (state.pairing) pairingObserved = true
            if (pairingObserved && !state.pairing && state.paired && state.pairingError == null) {
                showPairing = false
                pairPrefill = ""
                pairingObserved = false
            }
        }
        fun openSheet(value: AppSheet) {
            sheet = value
            scope.launch { drawer.close() }
        }
        ModalNavigationDrawer(
            drawerState = drawer,
            gesturesEnabled = state.paired,
            drawerContent = {
                HistoryDrawer(state, actions,
                    onSelect = { actions.selectConversation(it); scope.launch { drawer.close() } },
                    onNew = { actions.newConversation(); scope.launch { drawer.close() } },
                    onSettings = { openSheet(AppSheet.CONNECTION) },
                    onPet = { openSheet(AppSheet.PET) },
                    onQueue = { showQueue = true; scope.launch { drawer.close() } },
                    onImages = { openSheet(AppSheet.IMAGES); actions.refreshImages() })
            },
        ) {
            Scaffold(
                containerColor = MaterialTheme.colorScheme.background,
                topBar = {
                    AppHeader(state,
                        onMenu = { scope.launch { drawer.open() } },
                        onNew = actions::newConversation,
                        onSettings = { openSheet(AppSheet.CONNECTION) },
                        onPet = { openSheet(AppSheet.PET) },
                        onQueue = { showQueue = true },
                        onImages = { openSheet(AppSheet.IMAGES); actions.refreshImages() })
                },
                snackbarHost = { SnackbarHost(snackbars) },
                bottomBar = {
                    if (state.paired) {
                        MessageComposer(draft, { draft = it }, state,
                            onAttach = actions::pickImage,
                            onSend = { if (draft.isNotBlank()) { actions.sendMessage(draft.trim()); draft = "" } })
                    }
                },
            ) { insets ->
                Column(Modifier.fillMaxSize().padding(insets), horizontalAlignment = Alignment.CenterHorizontally) {
                    if (!state.paired) {
                        Welcome(Modifier.weight(1f), onPair = { pairPrefill = ""; showPairing = true })
                    } else {
                        ConnectionStrip(state, actions::reconnect, actions::syncNow,
                            onSettings = { openSheet(AppSheet.CONNECTION) })
                        if (state.messages.isEmpty()) {
                            EmptyConversation(Modifier.weight(1f), state.connection != ConnectionState.CONNECTED,
                                onSuggestion = { draft = it }, onPet = { openSheet(AppSheet.PET) })
                        } else {
                            key(state.selectedConversationId) {
                                ConversationMessages(state.messages, Modifier.weight(1f), actions::retryMessage)
                            }
                        }
                    }
                }
            }
        }
        when (sheet) {
            AppSheet.CONNECTION -> ConnectionSheet(state, actions,
                onDismiss = { sheet = null }, onQueue = { sheet = null; showQueue = true },
                onPair = { sheet = null; pairPrefill = ""; showPairing = true })
            AppSheet.PET -> PetSheet(state, actions, onDismiss = { sheet = null })
            AppSheet.IMAGES -> ImagesSheet(state.images, actions, notice = state.notice, onDismiss = { sheet = null })
            null -> Unit
        }
        if (showPairing) PairingDialog(state, actions, initialPayload = pairPrefill,
            onDismiss = { showPairing = false; pairPrefill = "" })
        if (showQueue) QueueDialog(state.queue, actions, notice = state.notice, onDismiss = { showQueue = false })
    }
}

@Composable
private fun AppHeader(state: UiState, onMenu: () -> Unit, onNew: () -> Unit,
                      onSettings: () -> Unit, onPet: () -> Unit, onQueue: () -> Unit, onImages: () -> Unit) {
    var menu by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        if (state.paired) IconButton(onMenu, Modifier.size(48.dp)) {
            Icon(Icons.Default.Menu, contentDescription = "打开对话历史")
        } else AuraMark(Modifier.size(48.dp))
        Column(Modifier.weight(1f)) {
            val title = if (state.paired) state.conversations.firstOrNull {
                it.id == state.selectedConversationId }?.title ?: "新对话" else "AURA"
            Text(title, style = MaterialTheme.typography.titleMedium, maxLines = 1,
                overflow = TextOverflow.Ellipsis)
            if (state.paired) Text("AURA · ${state.desktopName}", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        if (state.paired) IconButton(onNew, Modifier.size(48.dp)) {
            Icon(Icons.Default.Edit, contentDescription = "新建对话")
        }
        Box {
            IconButton(onClick = { menu = true }, Modifier.size(48.dp)) {
                Icon(Icons.Default.MoreVert, contentDescription = "更多选项")
            }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                if (state.paired) {
                    DropdownMenuItem(text = { Text("桌宠控制") },
                        leadingIcon = { Icon(Icons.Default.Face, null) },
                        onClick = { menu = false; onPet() })
                    DropdownMenuItem(text = { Text("图片收件箱") },
                        leadingIcon = { Icon(Icons.AutoMirrored.Filled.List, null) },
                        onClick = { menu = false; onImages() })
                }
                if (state.paired || state.queue.isNotEmpty()) DropdownMenuItem(
                    text = { Text("待处理消息${if (state.queue.isEmpty()) "" else " (${state.queue.size})"}") },
                    leadingIcon = { Icon(Icons.Default.Info, null) },
                    onClick = { menu = false; onQueue() })
                DropdownMenuItem(text = { Text("连接与外观") },
                    leadingIcon = { Icon(Icons.Default.Settings, null) },
                    onClick = { menu = false; onSettings() })
            }
        }
    }
}

@Composable
private fun Welcome(modifier: Modifier, onPair: () -> Unit) {
    Column(modifier.widthIn(max = 560.dp).fillMaxWidth().verticalScroll(rememberScrollState())
        .padding(horizontal = 32.dp, vertical = 40.dp),
        verticalArrangement = Arrangement.Center) {
        AuraMark(Modifier.size(76.dp))
        Spacer(Modifier.height(24.dp))
        Text("把陪伴，\n带在身边。", style = MaterialTheme.typography.headlineLarge,
            modifier = Modifier.semantics { heading() })
        Spacer(Modifier.height(16.dp))
        Text("和家里的 AURA 继续聊天，\n也把一个小小的回应，送回桌面。",
            style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(40.dp))
        FeatureRow("对话随行", "手机和电脑，接着上次的话题。")
        Spacer(Modifier.height(20.dp))
        FeatureRow("轻轻遥控", "传张图片，或让桌宠开心一下。")
        Spacer(Modifier.height(40.dp))
        Button(onClick = onPair, shape = RoundedCornerShape(16.dp),
            modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) {
            Text("连接我的电脑")
            Spacer(Modifier.width(8.dp))
            Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = null, modifier = Modifier.size(18.dp))
        }
        Spacer(Modifier.height(16.dp))
        Text("在电脑端打开「远程连接」，复制配对信息。", style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Center, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.fillMaxWidth())
    }
}

@Composable
private fun FeatureRow(title: String, description: String) {
    Row(horizontalArrangement = Arrangement.spacedBy(14.dp), verticalAlignment = Alignment.Top) {
        Box(Modifier.padding(top = 7.dp).size(7.dp).clip(CircleShape)
            .background(MaterialTheme.colorScheme.secondary))
        Column {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(description, style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun EmptyConversation(modifier: Modifier, offline: Boolean, onSuggestion: (String) -> Unit,
                              onPet: () -> Unit) {
    Column(modifier.widthIn(max = 620.dp).fillMaxWidth().verticalScroll(rememberScrollState())
        .padding(horizontal = 28.dp, vertical = 32.dp), verticalArrangement = Arrangement.Center) {
        AuraMark(Modifier.size(52.dp))
        Spacer(Modifier.height(20.dp))
        Text("今天，想聊点什么？", style = MaterialTheme.typography.headlineMedium,
            modifier = Modifier.semantics { heading() })
        Spacer(Modifier.height(12.dp))
        Text(if (offline) "先记下想说的话，连接后继续。" else "日常的小事，也可以慢慢说。",
            style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(28.dp))
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Suggestion("陪我聊一会儿") { onSuggestion("陪我聊一会儿") }
            Suggestion("整理一下今天") { onSuggestion("帮我整理一下今天的想法") }
            Suggestion("看看桌宠") { onPet() }
        }
    }
}

@Composable
private fun Suggestion(text: String, onClick: () -> Unit) {
    OutlinedButton(onClick, shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        modifier = Modifier.heightIn(min = 48.dp)) {
        Text(text, color = MaterialTheme.colorScheme.onSurface, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun ConnectionStrip(state: UiState, onReconnect: () -> Unit, onSync: () -> Unit,
                            onSettings: () -> Unit) {
    val connected = state.connection == ConnectionState.CONNECTED
    val busy = state.connection == ConnectionState.CONNECTING || state.sync == SyncState.SYNCING
    val text = when {
        state.connection == ConnectionState.UNAUTHORIZED -> "配对已失效，请重新连接"
        state.connection == ConnectionState.CONNECTING -> "正在连接电脑…"
        !connected -> "电脑暂时离线 · 消息会保留"
        state.sync == SyncState.SYNCING -> "正在同步对话…"
        state.sync == SyncState.ERROR -> "连接正常 · 对话待同步"
        state.queuedCount > 0 -> "已连接 · ${state.queuedCount} 条消息待发送"
        state.sync == SyncState.READY -> "已连接 · 对话已同步"
        else -> "已连接到电脑"
    }
    Surface(onClick = onSettings, color = MaterialTheme.colorScheme.surfaceContainerLow,
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 4.dp)) {
        Row(Modifier.heightIn(min = 48.dp).padding(start = 14.dp, end = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
            if (busy) CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 1.5.dp)
            else Box(Modifier.size(6.dp).clip(CircleShape).background(
                if (connected) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.outline))
            Text(text, style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f).semantics { liveRegion = LiveRegionMode.Polite })
            if (!busy && state.connection != ConnectionState.UNAUTHORIZED) {
                TextButton(onClick = if (connected) onSync else onReconnect,
                    modifier = Modifier.heightIn(min = 48.dp)) { Text(if (connected) "同步" else "重连") }
            }
        }
    }
}

@Composable
private fun ConversationMessages(messages: List<UiMessage>, modifier: Modifier, onRetry: (String) -> Unit) {
    val listState = rememberLazyListState()
    var initiallyPositioned by remember { mutableStateOf(false) }
    LaunchedEffect(messages.lastOrNull()?.id, messages.lastOrNull()?.content, messages.lastOrNull()?.status) {
        val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
        if (!initiallyPositioned || lastVisible >= messages.size - 3 || listState.layoutInfo.totalItemsCount == 0) {
            if (messages.isNotEmpty()) {
                listState.scrollToItem(messages.lastIndex)
                initiallyPositioned = true
            }
        }
    }
    LazyColumn(state = listState, modifier = modifier.widthIn(max = 800.dp).fillMaxWidth(),
        contentPadding = PaddingValues(horizontal = 24.dp, vertical = 24.dp),
        verticalArrangement = Arrangement.spacedBy(28.dp)) {
        items(messages, key = { it.id }) { MessageRow(it, onRetry) }
    }
}

@Composable
private fun MessageRow(message: UiMessage, onRetry: (String) -> Unit) {
    val user = message.role == MessageRole.USER
    Column(Modifier.fillMaxWidth(), horizontalAlignment = if (user) Alignment.End else Alignment.Start) {
        if (message.role == MessageRole.ASSISTANT) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                AuraMark(Modifier.size(20.dp))
                Text("AURA", style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(10.dp))
        }
        val body: @Composable ColumnScope.() -> Unit = {
            message.attachmentUri?.let { uri ->
                LocalImage(uri, Modifier.fillMaxWidth().heightIn(min = 100.dp, max = 240.dp)
                    .clip(RoundedCornerShape(12.dp)), "消息中的图片")
                if (message.content.isNotBlank()) Spacer(Modifier.height(8.dp))
            }
            if (message.content.isNotBlank()) SelectionContainer {
                if (message.role == MessageRole.ASSISTANT) MessageText(message.content)
                else Text(message.content, style = if (message.role == MessageRole.SYSTEM)
                    MaterialTheme.typography.bodyMedium else MaterialTheme.typography.bodyLarge,
                    color = if (message.role == MessageRole.SYSTEM) MaterialTheme.colorScheme.onSurfaceVariant
                    else MaterialTheme.colorScheme.onSurface)
            }
        }
        if (user) Surface(color = MaterialTheme.colorScheme.surfaceContainer,
            shape = RoundedCornerShape(20.dp, 20.dp, 5.dp, 20.dp),
            modifier = Modifier.fillMaxWidth(.88f)) {
            Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp), content = body)
        } else Column(Modifier.fillMaxWidth(), content = body)
        if (message.role == MessageRole.ASSISTANT && message.content.isNotBlank()) {
            CopyTextButton(message.content, "复制回复")
        }
        val delivery = when (message.status) {
            MessageStatus.QUEUED -> "等待连接后发送"
            MessageStatus.SENDING -> "正在发送…"
            MessageStatus.PENDING -> "电脑正在思考…"
            MessageStatus.FAILED -> "发送失败"
            MessageStatus.UNCERTAIN -> "送达结果待确认 · 请先同步"
            MessageStatus.SENT -> ""
        }
        if (delivery.isNotBlank() || message.detail.isNotBlank()) {
            Row(Modifier.padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Column(Modifier.weight(1f, fill = false)) {
                    Text(delivery, style = MaterialTheme.typography.bodySmall,
                        color = if (message.status == MessageStatus.FAILED) MaterialTheme.colorScheme.error
                        else MaterialTheme.colorScheme.onSurfaceVariant)
                    if (message.detail.isNotBlank()) Text(message.detail, style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (message.retryable) TextButton({ onRetry(message.id) }, Modifier.heightIn(min = 48.dp)) {
                    Text("重试")
                }
            }
        }
    }
}

@Composable
private fun MessageComposer(draft: String, onDraft: (String) -> Unit, state: UiState,
                            onAttach: () -> Unit, onSend: () -> Unit) {
    Column(Modifier.fillMaxWidth().navigationBarsPadding().imePadding()
        .padding(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(shape = RoundedCornerShape(26.dp), color = MaterialTheme.colorScheme.surfaceContainerLowest,
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            modifier = Modifier.widthIn(max = 800.dp).fillMaxWidth()) {
            Row(Modifier.padding(6.dp), verticalAlignment = Alignment.Bottom,
                horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                IconButton(onAttach, Modifier.size(48.dp)) {
                    Icon(Icons.Default.Add, contentDescription = "添加图片")
                }
                BasicTextField(value = draft, onValueChange = { onDraft(it.take(8000)) },
                    textStyle = MaterialTheme.typography.bodyLarge.copy(color = MaterialTheme.colorScheme.onSurface),
                    cursorBrush = SolidColor(MaterialTheme.colorScheme.primary), maxLines = 6,
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).padding(vertical = 12.dp)
                        .semantics { contentDescription = "消息内容" },
                    decorationBox = { inner ->
                        if (draft.isEmpty()) Text("发消息给 AURA", style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        inner()
                    })
                FilledIconButton(onSend, enabled = draft.isNotBlank(), modifier = Modifier.size(48.dp)) {
                    Icon(Icons.AutoMirrored.Filled.Send, contentDescription = if (state.connection == ConnectionState.CONNECTED)
                        "发送消息" else "保存消息，连接后发送", modifier = Modifier.size(21.dp))
                }
            }
        }
        Text(if (state.connection == ConnectionState.CONNECTED) "与你的桌面，保持一点联系。"
            else "离线也能记录，重新连接后自动发送。", style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
    }
}

@Composable
private fun HistoryDrawer(state: UiState, actions: UiActions, onSelect: (String) -> Unit,
                          onNew: () -> Unit, onSettings: () -> Unit,
                          onPet: () -> Unit, onQueue: () -> Unit, onImages: () -> Unit) {
    var query by rememberSaveable { mutableStateOf("") }
    val filtered = remember(state.conversations, query) {
        state.conversations.filter { it.title.contains(query, true) || it.preview.contains(query, true) }
    }
    ModalDrawerSheet(Modifier.widthIn(max = 340.dp).fillMaxHeight(),
        drawerContainerColor = MaterialTheme.colorScheme.surfaceContainerLow) {
        Row(Modifier.fillMaxWidth().padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
            AuraMark(Modifier.size(36.dp))
            Text("AURA", modifier = Modifier.padding(start = 10.dp),
                style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Medium)
        }
        OutlinedButton(onNew, Modifier.fillMaxWidth().padding(horizontal = 20.dp).heightIn(min = 48.dp),
            shape = RoundedCornerShape(14.dp), border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
            Icon(Icons.Default.Add, null, Modifier.size(20.dp)); Spacer(Modifier.width(8.dp)); Text("新建对话")
        }
        OutlinedTextField(query, { query = it }, label = { Text("搜索对话") }, singleLine = true,
            leadingIcon = { Icon(Icons.Default.Search, null) },
            modifier = Modifier.fillMaxWidth().padding(20.dp), shape = RoundedCornerShape(14.dp))
        Text("最近对话", style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 24.dp))
        if (filtered.isEmpty()) Text(if (query.isBlank()) "对话会保存在这里。" else "没有找到相关对话。",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(24.dp))
        LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp)) {
            items(filtered, key = { it.id }) { conversation ->
                Surface(onClick = { onSelect(conversation.id) }, shape = RoundedCornerShape(14.dp),
                    color = if (conversation.id == state.selectedConversationId) MaterialTheme.colorScheme.primaryContainer
                    else MaterialTheme.colorScheme.surfaceContainerLow,
                    modifier = Modifier.fillMaxWidth().semantics {
                        selected = conversation.id == state.selectedConversationId
                    }) {
                    Column(Modifier.padding(horizontal = 14.dp, vertical = 12.dp)) {
                        Text(conversation.title.ifBlank { "新对话" }, style = MaterialTheme.typography.titleMedium,
                            maxLines = 2, overflow = TextOverflow.Ellipsis)
                        if (conversation.preview.isNotBlank()) Text(conversation.preview,
                            style = MaterialTheme.typography.bodySmall, maxLines = 2,
                            overflow = TextOverflow.Ellipsis, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        if (conversation.localOnly || conversation.updatedAt.isNotBlank()) Text(
                            if (conversation.localOnly) "保存在手机" else conversation.updatedAt,
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
        HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            DrawerAction("桌宠控制", Icons.Default.Face, onPet)
            DrawerAction("图片收件箱", Icons.AutoMirrored.Filled.List, onImages)
            DrawerAction("待处理消息${if (state.queue.isEmpty()) "" else " (${state.queue.size})"}", Icons.Default.Info, onQueue)
            DrawerAction("连接与外观", Icons.Default.Settings, onSettings)
        }
    }
}

@Composable
private fun DrawerAction(label: String, icon: androidx.compose.ui.graphics.vector.ImageVector, onClick: () -> Unit) {
    TextButton(onClick, Modifier.fillMaxWidth().heightIn(min = 48.dp)) {
        Icon(icon, null, Modifier.size(20.dp)); Spacer(Modifier.width(12.dp))
        Text(label, Modifier.weight(1f), textAlign = TextAlign.Start, color = MaterialTheme.colorScheme.onSurface)
    }
}

@Preview(name = "AURA · Welcome", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun WelcomePreview() { AuraApp(UiState(theme = ThemeMode.LIGHT), PreviewActions) }

@Preview(name = "AURA · Dark", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun DarkPreview() { AuraApp(previewChatState().copy(theme = ThemeMode.DARK), PreviewActions) }

@Preview(name = "AURA · Conversation", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun ChatPreview() { AuraApp(previewChatState(), PreviewActions) }

/** Preview fixtures are deliberately never passed into the production app. */
private fun previewChatState() = UiState(paired = true, desktopName = "家里的电脑",
    connection = ConnectionState.CONNECTED, sync = SyncState.READY, theme = ThemeMode.LIGHT,
    conversations = listOf(UiConversation("preview", "周末的一点计划")), selectedConversationId = "preview",
    messages = listOf(UiMessage("preview-u", MessageRole.USER, "今天终于忙完了，想出去走走。"),
        UiMessage("preview-a", MessageRole.ASSISTANT, "那就给自己留一点没有安排的时间。\n\n找一条熟悉的小路，买杯喜欢的饮料，看看今天的天空。你想去公园，还是只在附近转转？")))

private object PreviewActions : UiActions {
    override fun pair(payload: String) = Unit
    override fun consumePairInfo() = Unit
    override fun reconnect() = Unit
    override fun disconnect() = Unit
    override fun syncNow() = Unit
    override fun selectConversation(id: String) = Unit
    override fun newConversation() = Unit
    override fun sendMessage(text: String) = Unit
    override fun retryMessage(id: String) = Unit
    override fun discardOutbox(requestId: String) = Unit
    override fun pickImage() = Unit
    override fun petCommand(action: String, value: PetValue) = Unit
    override fun refreshImages() = Unit
    override fun openImage(id: String) = Unit
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
}
