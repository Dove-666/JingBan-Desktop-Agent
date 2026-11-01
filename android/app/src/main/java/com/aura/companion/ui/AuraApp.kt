@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch

private enum class AppSheet { CONNECTION, PET, IMAGES }
private data class DesktopSubmission(val serial: Int, val conversationId: String?)

// Small code-native vectors keep the controls recognizable without shipping an icon font.
internal val DesktopMonitorIcon by lazy {
    ImageVector.Builder("ComputerMonitor", 24.dp, 24.dp, 24f, 24f).apply {
        path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.8f,
            strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round) {
            moveTo(4f, 4f); lineTo(20f, 4f); quadTo(21f, 4f, 21f, 5f)
            lineTo(21f, 15f); quadTo(21f, 16f, 20f, 16f); lineTo(4f, 16f)
            quadTo(3f, 16f, 3f, 15f); lineTo(3f, 5f); quadTo(3f, 4f, 4f, 4f); close()
            moveTo(12f, 16f); lineTo(12f, 20f); moveTo(8f, 20f); lineTo(16f, 20f)
        }
    }.build()
}
private val ChatBubbleIcon by lazy {
    ImageVector.Builder("ChatBubble", 24.dp, 24.dp, 24f, 24f).apply {
        path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.8f,
            strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round) {
            moveTo(5f, 4f); lineTo(19f, 4f); quadTo(21f, 4f, 21f, 6f)
            lineTo(21f, 15f); quadTo(21f, 17f, 19f, 17f); lineTo(9f, 17f)
            lineTo(3f, 21f); lineTo(3f, 6f); quadTo(3f, 4f, 5f, 4f); close()
            moveTo(7f, 8f); lineTo(17f, 8f); moveTo(7f, 12f); lineTo(14f, 12f)
        }
    }.build()
}
private val NewChatIcon by lazy {
    ImageVector.Builder("NewChat", 24.dp, 24.dp, 24f, 24f).apply {
        path(fill = null, stroke = SolidColor(Color.Black), strokeLineWidth = 1.8f,
            strokeLineCap = StrokeCap.Round, strokeLineJoin = StrokeJoin.Round) {
            moveTo(20f, 12f); lineTo(20f, 16f); quadTo(20f, 18f, 18f, 18f)
            lineTo(9f, 18f); lineTo(3f, 22f); lineTo(3f, 6f); quadTo(3f, 4f, 5f, 4f)
            lineTo(12f, 4f); moveTo(18f, 2f); lineTo(18f, 10f)
            moveTo(14f, 6f); lineTo(22f, 6f)
        }
    }.build()
}
private val StopSquareIcon by lazy {
    ImageVector.Builder("StopSquare", 24.dp, 24.dp, 24f, 24f).apply {
        path(fill = SolidColor(Color.Black)) {
            moveTo(7f, 6f); lineTo(17f, 6f); quadTo(18f, 6f, 18f, 7f)
            lineTo(18f, 17f); quadTo(18f, 18f, 17f, 18f); lineTo(7f, 18f)
            quadTo(6f, 18f, 6f, 17f); lineTo(6f, 7f); quadTo(6f, 6f, 7f, 6f); close()
        }
    }.build()
}

@Composable
fun AuraApp(state: UiState, actions: UiActions) {
    AuraTheme(state.theme) {
      LiquidScene {
        val drawer = rememberDrawerState(DrawerValue.Closed)
        val scope = rememberCoroutineScope()
        val snackbars = remember { SnackbarHostState() }
        var sheet by remember { mutableStateOf<AppSheet?>(null) }
        var showPairing by rememberSaveable { mutableStateOf(false) }
        var showQueue by rememberSaveable { mutableStateOf(false) }
        var showApiConfig by remember { mutableStateOf(false) }
        var pairPrefill by remember { mutableStateOf("") }
        var draft by rememberSaveable { mutableStateOf("") }
        val desktopAssistance = state.desktopAgentMode
        var draftConversation by rememberSaveable { mutableStateOf(state.selectedConversationId) }
        var preserveFirstDesktopDraft by remember { mutableStateOf(false) }
        var desktopSubmission by remember { mutableStateOf<DesktopSubmission?>(null) }
        var submissionSerial by remember { mutableStateOf(0) }
        val currentConversation by rememberUpdatedState(state.selectedConversationId)
        val currentDraft by rememberUpdatedState(draft)
        LaunchedEffect(state.selectedConversationId) {
            val selected = state.selectedConversationId
            if (draftConversation != selected) {
                if (draftConversation == null && selected != null && preserveFirstDesktopDraft) {
                    desktopSubmission = desktopSubmission?.copy(conversationId = selected)
                } else {
                    draft = ""
                    desktopSubmission = null
                }
                preserveFirstDesktopDraft = false
                draftConversation = selected
            }
        }
        fun cancelDesktopDraftMigration() {
            preserveFirstDesktopDraft = false
            desktopSubmission = null
        }
        fun newConversation() { cancelDesktopDraftMigration(); actions.newConversation() }
        fun setDesktopAgentMode(enabled: Boolean) {
            preserveFirstDesktopDraft = state.selectedConversationId == null
            actions.setDesktopAgentMode(enabled)
            // A deliberate mode selection explains unavailable tools once. Quiet sync
            // changes never open a sheet, submit a task or erase the current draft.
            if (enabled && !state.desktopTaskAvailable) sheet = AppSheet.CONNECTION
        }
        fun pickChatImage() {
            preserveFirstDesktopDraft = state.selectedConversationId == null
            actions.pickImage()
        }
        val attachment = state.pendingAttachment?.takeIf { it.conversationId == state.selectedConversationId }
        val preparingImage = state.preparingImage || attachment?.preparing == true
        val canSend = !preparingImage && !state.directChatBusy && !state.desktopTaskBusy && if (desktopAssistance)
            state.desktopTaskAvailable && !state.desktopTaskRunning && (draft.isNotBlank() || attachment != null)
            else state.directApi.available && (draft.isNotBlank() || attachment != null)
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
            gesturesEnabled = state.chatAvailable,
            drawerContent = {
                HistoryDrawer(state, actions,
                    onSelect = { cancelDesktopDraftMigration(); actions.selectConversation(it); scope.launch { drawer.close() } },
                    onNew = { newConversation(); scope.launch { drawer.close() } },
                    onSettings = { openSheet(AppSheet.CONNECTION) },
                    onPet = { openSheet(AppSheet.PET) },
                    onQueue = { showQueue = true; scope.launch { drawer.close() } },
                    onImages = { openSheet(AppSheet.IMAGES); actions.refreshImages() })
            },
        ) {
            Scaffold(
                containerColor = Color.Transparent,
                contentColor = MaterialTheme.colorScheme.onBackground,
                topBar = {
                    AppHeader(state,
                        onMenu = { scope.launch { drawer.open() } },
                        onNew = ::newConversation,
                        onSettings = { openSheet(AppSheet.CONNECTION) },
                        onPet = { openSheet(AppSheet.PET) },
                        onQueue = { showQueue = true },
                        onImages = { openSheet(AppSheet.IMAGES); actions.refreshImages() },
                        onScreenshot = actions::requestDesktopScreenshot)
                },
                snackbarHost = { SnackbarHost(snackbars) },
                bottomBar = {
                    if (state.chatAvailable) {
                        MessageComposer(draft, { draft = it }, state, attachment, preparingImage, desktopAssistance, canSend,
                            onDesktopAssistance = ::setDesktopAgentMode,
                            onStop = actions::cancelDesktopTask,
                            onAttach = ::pickChatImage,
                            onConfigureApi = { showApiConfig = true },
                            onRemoveImage = actions::removePendingImage,
                            onSend = { if (canSend) {
                                if (desktopAssistance) {
                                    val origin = state.selectedConversationId
                                    val submitted = draft
                                    val serial = ++submissionSerial
                                    preserveFirstDesktopDraft = origin == null
                                    desktopSubmission = DesktopSubmission(serial, origin)
                                    actions.sendDesktopMessage(submitted.trim()) { accepted ->
                                        val active = desktopSubmission?.takeIf { it.serial == serial }
                                        val target = active?.conversationId ?: currentConversation.takeIf { preserveFirstDesktopDraft }
                                        if (accepted && active != null && currentConversation == target) {
                                            if (currentDraft == submitted) draft = ""
                                        }
                                        if (active != null) desktopSubmission = null
                                    }
                                } else { cancelDesktopDraftMigration(); actions.sendMessage(draft.trim()); draft = "" }
                            } })
                    }
                },
            ) { insets ->
                Column(Modifier.fillMaxSize().padding(insets), horizontalAlignment = Alignment.CenterHorizontally) {
                    if (!state.chatAvailable) {
                        Welcome(Modifier.weight(1f), onPair = { pairPrefill = ""; showPairing = true },
                            onConfigureApi = { showApiConfig = true })
                    } else {
                        if (state.messages.isEmpty()) {
                            EmptyConversation(Modifier.weight(1f), !state.directApi.available,
                                onSuggestion = { draft = it }, onPet = { openSheet(AppSheet.PET) })
                        } else {
                            key(state.selectedConversationId) {
                                ConversationMessages(state.messages, Modifier.weight(1f), actions::retryMessage, actions::openImage,
                                    phoneAvailable = state.directApi.available)
                            }
                        }
                    }
                }
            }
        }
        when (sheet) {
            AppSheet.CONNECTION -> ConnectionSheet(state, actions,
                onDismiss = { sheet = null }, onQueue = { sheet = null; showQueue = true },
                onConfigureApi = { sheet = null; showApiConfig = true },
                onPair = { sheet = null; pairPrefill = ""; showPairing = true })
            AppSheet.PET -> PetSheet(state, actions, onDismiss = { sheet = null })
            AppSheet.IMAGES -> ImagesSheet(state.images, actions, notice = state.notice,
                onPick = { sheet = null; pickChatImage() }, onDismiss = { sheet = null })
            null -> Unit
        }
        if (showPairing) PairingDialog(state, actions, initialPayload = pairPrefill,
            onDismiss = { showPairing = false; pairPrefill = "" })
        if (showQueue) QueueDialog(state.queue, actions, notice = state.notice,
            directApiAvailable = state.directApi.available, busy = state.directChatBusy,
            onConfigureApi = { showQueue = false; showApiConfig = true }, onDismiss = { showQueue = false })
        if (showApiConfig) ApiConfigDialog(state, actions, onDismiss = { showApiConfig = false })
      }
    }
}

@Composable
private fun AppHeader(state: UiState, onMenu: () -> Unit, onNew: () -> Unit,
                      onSettings: () -> Unit, onPet: () -> Unit, onQueue: () -> Unit, onImages: () -> Unit,
                      onScreenshot: () -> Unit) {
    var menu by remember { mutableStateOf(false) }
    Box(Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 12.dp, vertical = 6.dp),
        contentAlignment = Alignment.Center) {
      LiquidSurface(Modifier.widthIn(max = 800.dp).fillMaxWidth(),
          shape = RoundedCornerShape(24.dp), prominent = true, elevation = 1.dp) {
       Row(Modifier.fillMaxWidth().padding(horizontal = 2.dp, vertical = 2.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(2.dp)) {
        if (state.chatAvailable) IconButton(onMenu, Modifier.size(48.dp)) {
            Icon(Icons.Default.Menu, contentDescription = "打开对话历史")
        } else AuraMark(Modifier.size(48.dp))
        val title = state.conversations.firstOrNull { it.id == state.selectedConversationId }
            ?.title?.trim()?.takeIf { it.isNotEmpty() } ?: if (state.chatAvailable) "新对话" else "AURA"
        Text(title, style = MaterialTheme.typography.titleMedium,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f).testTag("conversation-title").semantics { heading() })
        ConnectionIndicator(state, onSettings)
        Box {
            IconButton(onClick = { menu = true }, Modifier.size(48.dp)) {
                Icon(Icons.Default.MoreVert, contentDescription = "更多选项")
            }
            DropdownMenu(expanded = menu, onDismissRequest = { menu = false },
                shape = RoundedCornerShape(24.dp), containerColor = glassSheetColor(),
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
                if (state.chatAvailable) DropdownMenuItem(text = { Text("新建对话") },
                    modifier = Modifier.semantics { contentDescription = "新建对话" },
                    leadingIcon = { Icon(NewChatIcon, null) },
                    onClick = { menu = false; onNew() })
                if (state.chatAvailable) DropdownMenuItem(text = { Text("查看电脑桌面") },
                    leadingIcon = { Icon(DesktopMonitorIcon, null) },
                    enabled = state.desktopTaskAvailable && !state.desktopTaskBusy &&
                        !state.desktopTaskRunning && !state.directChatBusy && !state.preparingImage,
                    onClick = { menu = false; onScreenshot() })
                if (state.paired) {
                    DropdownMenuItem(text = { Text("桌宠控制") },
                        leadingIcon = { Icon(Icons.Default.Face, null) },
                        onClick = { menu = false; onPet() })
                    DropdownMenuItem(text = { Text("图片收件箱") },
                        leadingIcon = { Icon(Icons.AutoMirrored.Filled.List, null) },
                        onClick = { menu = false; onImages() })
                }
                if (state.chatAvailable || state.queue.isNotEmpty()) DropdownMenuItem(
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
    }
}

@Composable
private fun ConnectionIndicator(state: UiState, onClick: () -> Unit) {
    val deviceName = state.desktopName.trim().ifBlank { "我的电脑" }
    val availability = when (state.connection) {
        ConnectionState.CONNECTED -> "在线"
        ConnectionState.CONNECTING -> "连接中"
        ConnectionState.UNAUTHORIZED -> "需要重新配对"
        ConnectionState.ERROR -> "连接失败"
        ConnectionState.DISCONNECTED -> "离线"
    }
    val status = if (state.connection == ConnectionState.CONNECTED && !state.desktopTaskAvailable)
        "$availability，电脑 Agent 暂不可用" else availability
    val online = state.connection == ConnectionState.CONNECTED
    val indicatorInk = if (online) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant
    val indicatorCutout = MaterialTheme.colorScheme.surface
    IconButton(onClick, Modifier.size(48.dp).testTag("connection-status").semantics {
        contentDescription = "$deviceName，连接设置"
        stateDescription = status
    }) {
        Box(Modifier.size(24.dp)) {
            Icon(DesktopMonitorIcon, null, Modifier.size(22.dp),
                tint = MaterialTheme.colorScheme.onSurfaceVariant)
            androidx.compose.foundation.Canvas(Modifier.size(8.dp).align(Alignment.BottomEnd)) {
                drawCircle(indicatorInk, radius = size.minDimension / 2)
                if (!online) drawLine(indicatorCutout, androidx.compose.ui.geometry.Offset(size.width * .25f, size.height * .75f),
                    androidx.compose.ui.geometry.Offset(size.width * .75f, size.height * .25f), strokeWidth = 1.1.dp.toPx())
            }
        }
    }
}

@Composable
private fun Welcome(modifier: Modifier, onPair: () -> Unit, onConfigureApi: () -> Unit) {
    Column(modifier.widthIn(max = 560.dp).fillMaxWidth().verticalScroll(rememberScrollState())
        .padding(horizontal = 28.dp, vertical = 24.dp),
        verticalArrangement = Arrangement.Center) {
        LiquidSurface(shape = RoundedCornerShape(28.dp)) {
            AuraMark(Modifier.padding(10.dp).size(60.dp))
        }
        Spacer(Modifier.height(20.dp))
        Text("把陪伴，\n带在身边。", style = MaterialTheme.typography.headlineLarge,
            modifier = Modifier.semantics { heading() })
        Spacer(Modifier.height(12.dp))
        Text("在手机上和 AURA 聊聊天，\n也可以与电脑接着上次的话题。",
            style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(24.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            FeatureRow("对话随行", "接着上次的话题。", Modifier.weight(1f))
            FeatureRow("轻轻遥控", "给桌宠一个回应。", Modifier.weight(1f))
        }
        Spacer(Modifier.height(24.dp))
        Button(onClick = onConfigureApi, shape = RoundedCornerShape(20.dp),
            modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) {
            Text("配置模型，直接聊天")
            Spacer(Modifier.width(8.dp))
            Icon(Icons.AutoMirrored.Filled.ArrowForward, contentDescription = null, modifier = Modifier.size(18.dp))
        }
        Spacer(Modifier.height(8.dp))
        OutlinedButton(onClick = onPair, shape = RoundedCornerShape(20.dp),
            modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("连接我的电脑") }
        Spacer(Modifier.height(16.dp))
        Text("在电脑端打开「远程连接」，复制配对信息。", style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Center, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.fillMaxWidth())
    }
}

@Composable
private fun FeatureRow(title: String, description: String, modifier: Modifier = Modifier) {
    LiquidSurface(modifier, shape = RoundedCornerShape(22.dp)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
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
        LiquidSurface(shape = RoundedCornerShape(24.dp)) {
            AuraMark(Modifier.padding(9.dp).size(48.dp))
        }
        Spacer(Modifier.height(20.dp))
        Text("今天，想聊点什么？", style = MaterialTheme.typography.headlineMedium,
            modifier = Modifier.semantics { heading() })
        Spacer(Modifier.height(12.dp))
        Text(if (offline) "配置手机模型，文字和图片都可以直接聊。" else "日常的小事，也可以慢慢说。",
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
    OutlinedButton(onClick, shape = RoundedCornerShape(20.dp),
        colors = ButtonDefaults.outlinedButtonColors(containerColor = MaterialTheme.colorScheme.surface.copy(alpha = .65f)),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant.copy(alpha = .8f)),
        modifier = Modifier.heightIn(min = 48.dp)) {
        Text(text, color = MaterialTheme.colorScheme.onSurface, style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
private fun ConversationMessages(messages: List<UiMessage>, modifier: Modifier, onRetry: (String) -> Unit,
                                 onOpenImage: (String) -> Unit, phoneAvailable: Boolean) {
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
        items(messages, key = { it.id }) { MessageRow(it, onRetry, onOpenImage, phoneAvailable) }
    }
}

@Composable
private fun MessageRow(message: UiMessage, onRetry: (String) -> Unit, onOpenImage: (String) -> Unit, phoneAvailable: Boolean) {
    val user = message.role == MessageRole.USER
    Column(Modifier.fillMaxWidth(), horizontalAlignment = if (user) Alignment.End else Alignment.Start) {
        if (message.role == MessageRole.ASSISTANT && message.execution == null) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                AuraMark(Modifier.size(20.dp))
                Text(if (message.desktopAgent) "AURA · 电脑 Agent" else "AURA", style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(10.dp))
        }
        message.execution?.let { process ->
            ExecutionProcess(process, Modifier.fillMaxWidth(), identity = if (message.role == MessageRole.ASSISTANT)
                if (message.desktopAgent) "AURA · 电脑 Agent" else "AURA" else null)
            if (message.content.isNotBlank()) Spacer(Modifier.height(12.dp))
        }
        val body: @Composable ColumnScope.() -> Unit = {
            if (message.attachmentUri != null || message.attachmentId != null) {
                Surface(onClick = { message.attachmentId?.let(onOpenImage) },
                    enabled = message.attachmentId != null,
                    color = MaterialTheme.colorScheme.surfaceContainerLow, shape = RoundedCornerShape(12.dp),
                    modifier = Modifier.fillMaxWidth().semantics { contentDescription = "预览消息图片" }) {
                    message.attachmentUri?.let { uri ->
                        LocalImage(uri, Modifier.fillMaxWidth().heightIn(min = 120.dp, max = 360.dp),
                            "消息中的图片", expandToAvailableWidth = true)
                    } ?: Box(Modifier.fillMaxWidth().heightIn(min = 160.dp).padding(20.dp),
                        contentAlignment = Alignment.Center) {
                        Text("点按查看图片", style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
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
        if (user) Surface(color = MaterialTheme.colorScheme.primaryContainer.copy(alpha = .64f),
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.surface.copy(alpha = .78f)),
            shape = RoundedCornerShape(22.dp, 22.dp, 6.dp, 22.dp),
            modifier = Modifier.fillMaxWidth(.88f)) {
            Column(Modifier.padding(horizontal = 16.dp, vertical = 12.dp), content = body)
        } else Column(Modifier.fillMaxWidth(), content = body)
        if (message.role == MessageRole.ASSISTANT && message.content.isNotBlank()) {
            CopyTextButton(message.content, "复制回复")
        }
        val delivery = if (message.execution != null) "" else when (message.status) {
            MessageStatus.QUEUED -> if (message.desktopAgent) "电脑任务等待发送" else "尚未发送 · 可在待处理消息中继续"
            MessageStatus.SENDING -> if (message.desktopAgent) "正在提交电脑任务…" else "正在发送…"
            MessageStatus.PENDING -> if (message.desktopAgent) "电脑任务执行中…" else "正在思考…"
            MessageStatus.FAILED -> if (message.desktopAgent) "电脑任务未完成" else "发送失败"
            MessageStatus.UNCERTAIN -> if (message.desktopAgent) "电脑任务状态待确认 · 请先同步" else "送达结果待确认 · 请先同步"
            MessageStatus.SENT -> if (message.desktopAgent && user) "已交给电脑 Agent" else ""
        }
        if (delivery.isNotBlank() || message.detail.isNotBlank()) {
            Row(Modifier.padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Column(Modifier.weight(1f, fill = false)) {
                    if (delivery.isNotBlank()) Text(delivery, style = MaterialTheme.typography.bodySmall,
                        color = if (message.status == MessageStatus.FAILED) MaterialTheme.colorScheme.error
                        else MaterialTheme.colorScheme.onSurfaceVariant)
                    if (message.detail.isNotBlank()) Text(message.detail, style = MaterialTheme.typography.bodySmall,
                        color = if (message.status == MessageStatus.FAILED) MaterialTheme.colorScheme.error
                        else MaterialTheme.colorScheme.onSurfaceVariant)
                }
                val retryOnPhone = phoneAvailable && message.retryOnPhone && message.status == MessageStatus.QUEUED
                if (message.retryable || retryOnPhone) TextButton({ onRetry(message.id) }, Modifier.heightIn(min = 48.dp)) {
                    Text(if (retryOnPhone) "手机直接发送" else "重试")
                }
            }
        }
    }
}

@Composable
private fun MessageComposer(draft: String, onDraft: (String) -> Unit, state: UiState,
                            attachment: UiPendingAttachment?, preparingImage: Boolean,
                            desktopAssistance: Boolean, canSend: Boolean,
                            onDesktopAssistance: (Boolean) -> Unit, onStop: () -> Unit,
                            onConfigureApi: () -> Unit,
                            onAttach: () -> Unit, onRemoveImage: () -> Unit, onSend: () -> Unit) {
    Column(Modifier.fillMaxWidth().navigationBarsPadding().imePadding().testTag("message-composer")
        .padding(start = 12.dp, end = 12.dp, top = 4.dp, bottom = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        LiquidSurface(Modifier.widthIn(max = 800.dp).fillMaxWidth().padding(bottom = 6.dp),
            shape = RoundedCornerShape(24.dp)) {
          Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp), horizontalArrangement = Arrangement.spacedBy(4.dp),
              verticalAlignment = Alignment.CenterVertically) {
            FilterChip(selected = !desktopAssistance, onClick = { onDesktopAssistance(false) },
                label = { Text("手机聊天") }, enabled = !state.desktopTaskBusy && !state.directChatBusy,
                leadingIcon = { Icon(ChatBubbleIcon, null, Modifier.size(18.dp)) },
                shape = RoundedCornerShape(20.dp), border = null,
                colors = liquidModeColors(), modifier = Modifier.weight(1f).heightIn(min = 48.dp))
            FilterChip(selected = desktopAssistance, onClick = { onDesktopAssistance(true) },
                label = { Text("电脑 Agent") }, enabled = !state.desktopTaskBusy && !state.directChatBusy,
                leadingIcon = { Icon(DesktopMonitorIcon, null, Modifier.size(18.dp)) },
                shape = RoundedCornerShape(20.dp), border = null,
                colors = liquidModeColors(), modifier = Modifier.weight(1f).heightIn(min = 48.dp))
          }
        }
        if (preparingImage || attachment != null) {
            LiquidSurface(shape = RoundedCornerShape(22.dp),
                modifier = Modifier.widthIn(max = 800.dp).fillMaxWidth().padding(bottom = 8.dp)) {
                Row(Modifier.padding(10.dp), verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    if (attachment != null) {
                        LocalImage(attachment.localUri, Modifier.size(64.dp).clip(RoundedCornerShape(10.dp)), "待发送图片")
                    } else CircularProgressIndicator(Modifier.padding(16.dp).size(24.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.weight(1f))
                    if (attachment != null) IconButton(onRemoveImage, enabled = !preparingImage && !state.desktopTaskBusy,
                        modifier = Modifier.size(48.dp)) { Icon(Icons.Default.Close, contentDescription = "移除待发送图片") }
                }
            }
        }
        LiquidSurface(shape = RoundedCornerShape(26.dp), prominent = true, elevation = 2.dp,
            modifier = Modifier.widthIn(max = 800.dp).fillMaxWidth()) {
            Row(Modifier.padding(4.dp), verticalAlignment = Alignment.Bottom,
                horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                IconButton(onAttach, Modifier.size(48.dp), enabled = !preparingImage && !state.desktopTaskBusy) {
                    Icon(Icons.Default.Add, contentDescription = "添加图片")
                }
                if (!desktopAssistance && !state.directApi.available) IconButton(onConfigureApi,
                    Modifier.size(48.dp), enabled = !preparingImage && !state.desktopTaskBusy) {
                    Icon(Icons.Default.Settings, contentDescription = "配置手机模型")
                }
                BasicTextField(value = draft, onValueChange = { onDraft(it.take(8000)) },
                    textStyle = MaterialTheme.typography.bodyLarge.copy(color = MaterialTheme.colorScheme.onSurface),
                    cursorBrush = SolidColor(MaterialTheme.colorScheme.primary), maxLines = 6,
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).padding(vertical = 12.dp)
                        .semantics { contentDescription = "消息内容" },
                    decorationBox = { inner ->
                        if (draft.isEmpty()) Text(if (desktopAssistance) "告诉电脑 Agent 下一步…" else if (attachment != null) "问问 AURA 这张图片…" else "发消息给 AURA",
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        inner()
                    })
                val showStop = desktopAssistance && state.desktopTaskRunning
                FilledIconButton(if (showStop) onStop else onSend, enabled = showStop || canSend,
                    modifier = Modifier.size(48.dp).semantics {
                        contentDescription = if (showStop) "停止电脑任务" else if (state.desktopTaskBusy) "正在提交电脑任务" else if (state.directChatBusy) "正在回复，请稍候" else "发送消息"
                    }) {
                    if (showStop) Icon(StopSquareIcon, contentDescription = null, modifier = Modifier.size(21.dp))
                    else if (state.directChatBusy || state.desktopTaskBusy) CircularProgressIndicator(Modifier.size(21.dp), strokeWidth = 2.dp)
                    else Icon(Icons.AutoMirrored.Filled.Send, contentDescription = null, modifier = Modifier.size(21.dp))
                }
            }
        }

    }
}

@Composable
private fun liquidModeColors() = FilterChipDefaults.filterChipColors(
    containerColor = Color.Transparent,
    labelColor = MaterialTheme.colorScheme.onSurfaceVariant,
    iconColor = MaterialTheme.colorScheme.onSurfaceVariant,
    selectedContainerColor = MaterialTheme.colorScheme.primaryContainer.copy(alpha = .72f),
    selectedLabelColor = MaterialTheme.colorScheme.onPrimaryContainer,
    selectedLeadingIconColor = MaterialTheme.colorScheme.onPrimaryContainer,
)

@Composable
private fun HistoryDrawer(state: UiState, actions: UiActions, onSelect: (String) -> Unit,
                          onNew: () -> Unit, onSettings: () -> Unit,
                          onPet: () -> Unit, onQueue: () -> Unit, onImages: () -> Unit) {
    var query by rememberSaveable { mutableStateOf("") }
    val filtered = remember(state.conversations, query) {
        state.conversations.filter { it.title.contains(query, true) || it.preview.contains(query, true) }
    }
    ModalDrawerSheet(Modifier.widthIn(max = 340.dp).fillMaxHeight(),
        drawerShape = RoundedCornerShape(topEnd = 30.dp, bottomEnd = 30.dp),
        drawerContainerColor = glassSheetColor(), drawerContentColor = MaterialTheme.colorScheme.onSurface) {
        Row(Modifier.fillMaxWidth().padding(20.dp), verticalAlignment = Alignment.CenterVertically) {
            AuraMark(Modifier.size(36.dp))
            Text("AURA", modifier = Modifier.padding(start = 10.dp),
                style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Medium)
        }
        OutlinedButton(onNew, Modifier.fillMaxWidth().padding(horizontal = 20.dp).heightIn(min = 48.dp),
            shape = RoundedCornerShape(20.dp), border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
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

@Preview(name = "AURA · Phone API", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun DirectApiPreview() {
    AuraApp(previewChatState().copy(connection = ConnectionState.DISCONNECTED, sync = SyncState.IDLE,
        directApi = UiDirectApi(available = true, model = "示例模型", sync = ApiConfigSyncState.READY),
        historyPendingCount = 2), PreviewActions)
}

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
    override fun removePendingImage() = Unit
    override fun petCommand(action: String, value: PetValue) = Unit
    override fun refreshImages() = Unit
    override fun openImage(id: String) = Unit
    override fun deleteImage(id: String) = Unit
    override fun dismissNotice() = Unit
    override fun setTheme(mode: ThemeMode) = Unit
    override fun saveDirectApiConfig(input: UiApiConfigInput) = Unit
    override fun sendQueuedOnPhone(requestId: String) = Unit
    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) { onAccepted(false) }
    override fun requestDesktopScreenshot() = Unit
}
