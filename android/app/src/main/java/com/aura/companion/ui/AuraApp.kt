@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.aura.companion.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.selection.selectable
import androidx.compose.ui.semantics.Role
import androidx.compose.foundation.BorderStroke
import androidx.activity.compose.BackHandler
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
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.path
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.nestedscroll.NestedScrollConnection
import androidx.compose.ui.input.nestedscroll.NestedScrollSource
import androidx.compose.ui.input.nestedscroll.nestedScroll
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
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.launch

private enum class AppSheet { CONNECTION }

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
internal fun AuraApp(state: UiState, actions: UiActions, draftStore: DraftStore? = null) {
    AuraTheme(state.theme) {
      LiquidScene {
        val drawer = rememberDrawerState(DrawerValue.Closed)
        val scope = rememberCoroutineScope()
        val snackbars = remember { SnackbarHostState() }
        var sheet by remember { mutableStateOf<AppSheet?>(null) }
        var showPairing by rememberSaveable { mutableStateOf(false) }
        var showQueue by rememberSaveable { mutableStateOf(false) }
        var showApiConfig by remember { mutableStateOf(false) }
        var showCharacterCards by rememberSaveable { mutableStateOf(false) }
        var pairPrefill by remember { mutableStateOf("") }
        val draftFactory = remember(draftStore) { object : ViewModelProvider.Factory {
            @Suppress("UNCHECKED_CAST")
            override fun <T : ViewModel> create(modelClass: Class<T>): T = ConversationDrafts(draftStore) as T
        } }
        val drafts: ConversationDrafts = viewModel(factory = draftFactory)
        val lifecycleOwner = LocalLifecycleOwner.current
        DisposableEffect(lifecycleOwner, drafts) {
            val observer = LifecycleEventObserver { _, event ->
                if (event == Lifecycle.Event.ON_STOP) drafts.requestFlush()
            }
            lifecycleOwner.lifecycle.addObserver(observer)
            onDispose { lifecycleOwner.lifecycle.removeObserver(observer); drafts.requestFlush() }
        }
        BackHandler(enabled = drawer.isOpen) { scope.launch { drawer.close() } }
        val draftCursor = rememberSaveable(saver = CurrentDraftCursor.saver(drafts)) {
            CurrentDraftCursor(drafts, state.selectedConversationId)
        }
        SideEffect { draftCursor.conversationId = state.selectedConversationId }
        val draft = drafts.read(state.selectedConversationId).text
        val latestState by rememberUpdatedState(state)
        var preparingSubmission by remember { mutableStateOf(false) }
        val desktopAssistance = state.desktopAgentMode
        var draftConversation by rememberSaveable { mutableStateOf(state.selectedConversationId) }
        LaunchedEffect(state.selectedConversationId) {
            drafts.requestFlush()
            val selected = state.selectedConversationId
            if (draftConversation != selected) {
                if (draftConversation == null && selected != null && drafts.preserveFirstConversation) {
                    drafts.migrateFirstDraft(selected)
                } else drafts.cancelFirstMigration()
                draftConversation = selected
            }
        }
        fun cancelDesktopDraftMigration() {
            drafts.cancelFirstMigration()
        }
        fun newConversation() { cancelDesktopDraftMigration(); actions.newConversation() }
        fun setDesktopAgentMode(enabled: Boolean) {
            drafts.preserveFirstDraft(state.selectedConversationId)
            actions.setDesktopAgentMode(enabled)
            // A deliberate mode selection explains unavailable tools once. Quiet sync
            // changes never open a sheet, submit a task or erase the current draft.
        }
        fun pickChatImage() {
            drafts.preserveFirstDraft(state.selectedConversationId)
            actions.pickImage()
        }
        val attachment = state.pendingAttachment?.takeIf { it.conversationId == state.selectedConversationId }
        val interaction = state.agentInteraction?.takeIf { it.conversationId == state.selectedConversationId }
        val preparingImage = state.preparingImage || attachment?.preparing == true
        val canSend = drafts.loaded && !preparingSubmission && !drafts.clearingSubmittedDraft && drafts.persistenceError == null && draft.length <= 8000 && !preparingImage && !state.directChatBusy && !state.desktopTaskBusy && !state.agentInteractionBusy && if (desktopAssistance)
            if (state.desktopTaskRunning) state.canSteerDesktop && interaction == null && draft.isNotBlank() && attachment == null
            else state.desktopTaskAvailable && (draft.isNotBlank() || attachment != null)
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
                    onSettings = { openSheet(AppSheet.CONNECTION) })
            },
        ) {
            Scaffold(
                containerColor = Color.Transparent,
                contentColor = MaterialTheme.colorScheme.onBackground,
                topBar = {
                    AppHeader(state,
                        onMenu = { scope.launch { drawer.open() } },
                        onSettings = { openSheet(AppSheet.CONNECTION) })
                },
                snackbarHost = { SnackbarHost(snackbars) },
                bottomBar = {
                    if (state.chatAvailable) {
                      Column {
                        drafts.persistenceError?.let { error -> Text(error,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.padding(horizontal = 24.dp).testTag("draft-storage-error")) }
                        MessageComposer(draft, { drafts.edit(state.selectedConversationId, it) }, state, attachment, preparingImage, desktopAssistance, canSend,
                            onDesktopAssistance = ::setDesktopAgentMode,
                            onStop = { if (!desktopAssistance && state.directChatConversationId == state.selectedConversationId)
                                actions.cancelDirectChat() else actions.cancelDesktopTask() },
                            onAttach = ::pickChatImage,
                            onRemoveImage = actions::removePendingImage,
                            onSend = { if (canSend) {
                                if (!desktopAssistance) cancelDesktopDraftMigration()
                                val origin = state.selectedConversationId
                                val submitted = draft
                                val ticket = drafts.beginSubmission(origin)
                                val submit: (String, (Boolean) -> Unit) -> Unit = when {
                                    !desktopAssistance -> actions::sendPhoneMessage
                                    state.desktopTaskRunning -> actions::steerDesktopTask
                                    else -> actions::sendDesktopMessage
                                }
                                preparingSubmission = true
                                scope.launch {
                                    try {
                                        if (drafts.flush() && drafts.isCurrent(ticket) &&
                                            latestState.selectedConversationId == origin &&
                                            latestState.desktopAgentMode == desktopAssistance &&
                                            latestState.pendingAttachment?.takeIf { it.conversationId == origin }?.id == attachment?.id) {
                                            submit(submitted.trim()) { accepted -> drafts.acknowledge(ticket, accepted) }
                                        } else drafts.acknowledge(ticket, false)
                                    } finally {
                                        preparingSubmission = false
                                    }
                                }
                            } })
                      }
                    }
                },
            ) { insets ->
                Column(Modifier.fillMaxSize().padding(insets), horizontalAlignment = Alignment.CenterHorizontally) {
                    if (!state.chatAvailable) {
                        Welcome(Modifier.weight(1f), onPair = { pairPrefill = ""; showPairing = true },
                            onConfigureApi = { showApiConfig = true })
                    } else {
                        if (state.messages.isEmpty() && interaction == null && state.agentArtifacts.isEmpty()) {
                            EmptyConversation(Modifier.weight(1f))
                        } else {
                            key(state.selectedConversationId) {
                                ConversationMessages(state.messages, Modifier.weight(1f), actions::retryMessage, actions::openImage,
                                    phoneAvailable = state.directApi.available, interaction = interaction,
                                    interactionBusy = state.agentInteractionBusy, onAnswer = actions::answerAgentInteraction,
                                    artifacts = state.agentArtifacts, artifactDownloading = state.artifactDownloading, onSaveArtifact = actions::saveArtifact)
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
                onCharacterCards = { sheet = null; showCharacterCards = true; actions.refreshCharacters() },
                onPair = { sheet = null; pairPrefill = ""; showPairing = true })
            null -> Unit
        }
        if (showCharacterCards) CharacterCardsSheet(state, actions,
            onDismiss = { showCharacterCards = false },
            onConnect = {
                showCharacterCards = false
                if (state.paired) actions.reconnect()
                else { pairPrefill = ""; showPairing = true }
            })
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
private fun AppHeader(state: UiState, onMenu: () -> Unit, onSettings: () -> Unit) {
    Row(Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 12.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically) {
        if (state.chatAvailable) IconButton(onMenu, Modifier.size(48.dp)) {
            Icon(Icons.Default.Menu, contentDescription = "打开对话历史")
        } else AuraMark(Modifier.padding(8.dp).size(32.dp))
        val title = state.conversations.firstOrNull { it.id == state.selectedConversationId }
            ?.title?.trim()?.takeIf { it.isNotEmpty() } ?: if (state.chatAvailable) "新对话" else "鲸伴"
        Text(title, style = MaterialTheme.typography.titleMedium,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f).padding(horizontal = 8.dp)
                .testTag("conversation-title").semantics { heading() })
        if (state.paired) Box(Modifier.size(6.dp).clip(RoundedCornerShape(3.dp))
            .background(if (state.connection == ConnectionState.CONNECTED) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = .5f))
            .semantics { contentDescription = if (state.connection == ConnectionState.CONNECTED) "电脑在线" else "电脑离线" })
        IconButton(onSettings, Modifier.size(48.dp).testTag("app-settings")) {
            Icon(Icons.Default.Settings, contentDescription = "设置", modifier = Modifier.size(22.dp))
        }
    }
}

@Composable
private fun Welcome(modifier: Modifier, onPair: () -> Unit, onConfigureApi: () -> Unit) {
    Column(modifier.widthIn(max = 440.dp).fillMaxWidth().verticalScroll(rememberScrollState())
        .padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        AuraMark(Modifier.size(64.dp))
        Spacer(Modifier.height(20.dp))
        Text("把陪伴，带在身边。", style = MaterialTheme.typography.headlineSmall,
            modifier = Modifier.semantics { heading() })
        Spacer(Modifier.height(24.dp))
        Button(onConfigureApi, Modifier.heightIn(min = 48.dp)) { Text("配置模型") }
        TextButton(onPair, Modifier.heightIn(min = 48.dp)) { Text("连接电脑") }
    }
}

@Composable
private fun EmptyConversation(modifier: Modifier) {
    Column(modifier.fillMaxWidth().padding(28.dp), horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        AuraMark(Modifier.size(48.dp))
        Spacer(Modifier.height(16.dp))
        Text("今天想聊什么？", style = MaterialTheme.typography.titleLarge)
    }
}

@Composable
internal fun ConversationMessages(messages: List<UiMessage>, modifier: Modifier, onRetry: (String) -> Unit,
                                 onOpenImage: (String) -> Unit, phoneAvailable: Boolean,
                                 interaction: UiAgentInteraction? = null, interactionBusy: Boolean = false,
                                 onAnswer: (String, UiAgentAnswer, (Boolean) -> Unit) -> Unit = { _, _, answer -> answer(false) },
                                 artifacts: List<UiAgentArtifact> = emptyList(), artifactDownloading: String? = null,
                                 onSaveArtifact: (String) -> Unit = {}) {
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    var followLatest by remember { mutableStateOf(true) }
    val bottomTolerance = with(LocalDensity.current) { 24.dp.roundToPx() }
    val scrollIntent = remember(listState, bottomTolerance) { object : NestedScrollConnection {
        override fun onPreScroll(available: Offset, source: NestedScrollSource): Offset {
            if (source == NestedScrollSource.UserInput && available.y > 0f) followLatest = false
            return Offset.Zero
        }
        override fun onPostScroll(consumed: Offset, available: Offset, source: NestedScrollSource): Offset {
            if (consumed.y < 0f) {
                val info = listState.layoutInfo
                val last = info.visibleItemsInfo.lastOrNull()
                followLatest = last != null && isNearConversationBottom(last.index, info.totalItemsCount,
                    last.offset + last.size, info.viewportEndOffset, bottomTolerance)
            }
            return Offset.Zero
        }
    } }
    // A trailing anchor reaches the newest text even when the last reply is taller
    // than the screen. Scrolling to the reply itself would reset it to its beginning.
    val tailIndex = messages.size + artifacts.size + if (interaction != null) 1 else 0
    LaunchedEffect(messages.lastOrNull()?.id, messages.lastOrNull()?.content, messages.lastOrNull()?.status, interaction?.id, artifacts) {
        withFrameNanos { }
        if (followLatest && !listState.isScrollInProgress) {
            listState.scrollToItem(tailIndex)
        }
    }
    Box(modifier.widthIn(max = 800.dp).fillMaxWidth()) {
    LazyColumn(state = listState, modifier = Modifier.fillMaxSize().nestedScroll(scrollIntent).testTag("conversation-messages"),
        contentPadding = PaddingValues(horizontal = 24.dp, vertical = 24.dp),
        verticalArrangement = Arrangement.spacedBy(28.dp)) {
        items(messages, key = { it.id }) { MessageRow(it, onRetry, onOpenImage, phoneAvailable) }
        items(artifacts, key = { "artifact:${it.reference}" }) { AgentArtifactCard(it, artifactDownloading, onSaveArtifact) }
        interaction?.let { pending -> item(key = "agent-question:${pending.id}") {
            AgentInteractionCard(pending, interactionBusy, onAnswer)
        } }
        item(key = "conversation-tail") { Spacer(Modifier.fillMaxWidth().height(1.dp).testTag("conversation-tail")) }
    }
    if (!followLatest && listState.canScrollForward) {
        FilledTonalIconButton(onClick = { followLatest = true; scope.launch { listState.scrollToItem(tailIndex) } },
            modifier = Modifier.align(Alignment.BottomEnd).padding(12.dp).testTag("return-to-latest")) {
            Icon(Icons.Default.KeyboardArrowDown, contentDescription = "回到最新消息")
        }
    }
    }
}

internal fun isNearConversationBottom(lastVisibleIndex: Int, totalItems: Int, lastVisibleEnd: Int,
    viewportEnd: Int, tolerancePixels: Int): Boolean = totalItems > 0 && lastVisibleIndex == totalItems - 1 &&
    lastVisibleEnd - viewportEnd <= tolerancePixels

@Composable
private fun MessageComposer(draft: String, onDraft: (String) -> Unit, state: UiState,
                            attachment: UiPendingAttachment?, preparingImage: Boolean,
                            desktopAssistance: Boolean, canSend: Boolean,
                            onDesktopAssistance: (Boolean) -> Unit, onStop: () -> Unit,
                            onAttach: () -> Unit, onRemoveImage: () -> Unit, onSend: () -> Unit) {
    val modeLineHeight = with(LocalDensity.current) { MaterialTheme.typography.labelLarge.lineHeight.toDp() }
    val modeVisualHeight = maxOf(34.dp, modeLineHeight + 8.dp)
    val modeTouchHeight = maxOf(48.dp, modeVisualHeight)
    Column(Modifier.fillMaxWidth().navigationBarsPadding().imePadding().testTag("message-composer")
        .padding(start = 12.dp, end = 12.dp, top = 4.dp, bottom = 6.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Row(Modifier.widthIn(max = 268.dp).fillMaxWidth().height(modeTouchHeight).testTag("chat-mode-switch"),
            verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.Center) {
            listOf(false to "手机聊天", true to "电脑 Agent").forEach { (desktop, label) ->
                val selected = desktopAssistance == desktop
                Box(Modifier.weight(1f).height(modeTouchHeight)
                    .testTag(if (desktop) "desktop-agent-mode" else "phone-chat-mode")
                    .clip(RoundedCornerShape(20.dp))
                    .selectable(selected = selected, enabled = !state.desktopTaskBusy && !state.directChatBusy,
                        role = Role.Tab, onClick = { onDesktopAssistance(desktop) }),
                    contentAlignment = Alignment.Center) {
                    Surface(shape = RoundedCornerShape(18.dp),
                        color = if (selected) MaterialTheme.colorScheme.primaryContainer.copy(alpha = .55f) else Color.Transparent,
                        modifier = Modifier.fillMaxWidth().height(modeVisualHeight)) {
                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text(label, style = MaterialTheme.typography.labelLarge, maxLines = 1, overflow = TextOverflow.Ellipsis,
                                color = if (selected) MaterialTheme.colorScheme.onPrimaryContainer
                                    else MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
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
                IconButton(onAttach, Modifier.size(48.dp), enabled = !preparingImage && !state.desktopTaskBusy && !state.desktopTaskRunning) {
                    Icon(Icons.Default.Add, contentDescription = "添加图片")
                }
                BasicTextField(value = draft, onValueChange = onDraft,
                    textStyle = MaterialTheme.typography.bodyLarge.copy(color = MaterialTheme.colorScheme.onSurface),
                    cursorBrush = SolidColor(MaterialTheme.colorScheme.primary), maxLines = 6,
                    modifier = Modifier.weight(1f).heightIn(min = 48.dp).padding(vertical = 12.dp)
                        .semantics { contentDescription = "消息内容" },
                    decorationBox = { inner ->
                        if (draft.isEmpty()) Text(if (desktopAssistance && state.desktopTaskRunning && state.canSteerDesktop) "补充当前任务…" else if (desktopAssistance && !state.desktopTaskAvailable)
                            if (state.connection == ConnectionState.CONNECTED) "电脑 Agent 暂不可用" else "电脑离线，连接后可执行任务"
                            else if (desktopAssistance) "告诉电脑 Agent 下一步…" else if (attachment != null) "问问 鲸伴 这张图片…" else "发消息给 鲸伴",
                            style = MaterialTheme.typography.bodyLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        inner()
                    })
                val phoneStreaming = !desktopAssistance && state.directChatBusy &&
                    state.directChatConversationId == state.selectedConversationId
                val showStop = (desktopAssistance && state.desktopTaskRunning) || phoneStreaming
                if (showStop && canSend) IconButton(onStop, Modifier.size(48.dp)) {
                    Icon(StopSquareIcon, contentDescription = "停止电脑任务", modifier = Modifier.size(21.dp))
                }
                FilledIconButton(if (showStop && !canSend) onStop else onSend, enabled = showStop || canSend,
                    modifier = Modifier.size(48.dp).semantics {
                        contentDescription = if (phoneStreaming) "停止回复" else if (showStop && !canSend) "停止电脑任务" else if (showStop) "补充指令" else if (state.desktopTaskBusy) "正在提交电脑任务" else if (state.directChatBusy) "正在回复，请稍候" else "发送消息"
                    }) {
                    if (showStop && !canSend) Icon(StopSquareIcon, contentDescription = null, modifier = Modifier.size(21.dp))
                    else if (state.directChatBusy || state.desktopTaskBusy) CircularProgressIndicator(Modifier.size(21.dp), strokeWidth = 2.dp)
                    else Icon(Icons.AutoMirrored.Filled.Send, contentDescription = null, modifier = Modifier.size(21.dp))
                }
            }
        }
        if (draft.length > 8000) Text("${draft.length}/8000 字，请缩短后发送",
            color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelMedium,
            modifier = Modifier.widthIn(max = 800.dp).fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp)
                .testTag("draft-over-limit"))
    }
}

@Composable
private fun HistoryDrawer(state: UiState, actions: UiActions, onSelect: (String) -> Unit,
                          onNew: () -> Unit, onSettings: () -> Unit) {
    var query by rememberSaveable { mutableStateOf("") }
    val filtered = remember(state.conversations, query) {
        state.conversations.filter { it.title.contains(query, true) || it.preview.contains(query, true) }
    }
    ModalDrawerSheet(Modifier.widthIn(max = 340.dp).fillMaxHeight().testTag("history-drawer"),
        drawerShape = RoundedCornerShape(topEnd = 24.dp, bottomEnd = 24.dp),
        drawerContainerColor = glassSheetColor(), drawerContentColor = MaterialTheme.colorScheme.onSurface) {
        Row(Modifier.fillMaxWidth().padding(start = 20.dp, end = 8.dp, top = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            AuraMark(Modifier.size(30.dp))
            Text("鲸伴", modifier = Modifier.padding(start = 10.dp).weight(1f),
                style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Medium)
            IconButton(onSettings, Modifier.size(48.dp).testTag("drawer-settings")) {
                Icon(Icons.Default.Settings, contentDescription = "设置", modifier = Modifier.size(22.dp))
            }
        }
        OutlinedButton(onNew, Modifier.fillMaxWidth().padding(horizontal = 16.dp).heightIn(min = 48.dp),
            shape = RoundedCornerShape(14.dp), border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant.copy(alpha = .55f))) {
            Icon(Icons.Default.Add, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("新建对话")
        }
        OutlinedTextField(query, { query = it }, placeholder = { Text("搜索对话") }, singleLine = true,
            leadingIcon = { Icon(Icons.Default.Search, null, Modifier.size(20.dp)) },
            modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp)
                .semantics { contentDescription = "搜索对话" }, shape = RoundedCornerShape(14.dp))
        Text("最近对话", style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        if (filtered.isEmpty()) Text(if (query.isBlank()) "还没有对话" else "没有找到相关对话",
            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(24.dp))
        LazyColumn(Modifier.weight(1f), contentPadding = PaddingValues(12.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp)) {
            items(filtered, key = { it.id }) { conversation ->
                Surface(onClick = { onSelect(conversation.id) }, shape = RoundedCornerShape(12.dp),
                    color = if (conversation.id == state.selectedConversationId) MaterialTheme.colorScheme.primaryContainer.copy(alpha = .50f)
                        else Color.Transparent,
                    modifier = Modifier.fillMaxWidth().semantics { selected = conversation.id == state.selectedConversationId }) {
                    Column(Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                        Text(conversation.title.ifBlank { "新对话" }, style = MaterialTheme.typography.bodyLarge,
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                        if (conversation.updatedAt.isNotBlank()) Text(conversation.updatedAt,
                            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

@Preview(name = "鲸伴 · Welcome", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun WelcomePreview() { AuraApp(UiState(theme = ThemeMode.LIGHT), PreviewActions) }

@Preview(name = "鲸伴 · Dark", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun DarkPreview() { AuraApp(previewChatState().copy(theme = ThemeMode.DARK), PreviewActions) }

@Preview(name = "鲸伴 · Conversation", widthDp = 393, heightDp = 852, showBackground = true)
@Composable
private fun ChatPreview() { AuraApp(previewChatState(), PreviewActions) }

@Preview(name = "鲸伴 · Phone API", widthDp = 393, heightDp = 852, showBackground = true)
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
