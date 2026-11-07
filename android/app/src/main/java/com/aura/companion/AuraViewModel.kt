package com.aura.companion

import android.app.Application
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aura.companion.data.AppRepository
import com.aura.companion.data.AppState
import com.aura.companion.data.DownloadedArtifact
import com.aura.companion.ui.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import java.io.File
import java.util.Locale
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import org.json.JSONObject

private data class LocalUi(
    val pairing: Boolean = false,
    val pairingError: String? = null,
    val notice: String? = null,
    val theme: ThemeMode = ThemeMode.LIGHT,
    val petScale: Int = 100,
    val petVisible: Boolean = true,
    val pendingPairInfo: String? = null,
    val preparingImage: Boolean = false,
    val apiConfigSaving: Boolean = false,
    val apiConfigError: String? = null,
    val apiConfigSaveGeneration: Long = 0,
    val desktopTaskBusy: Boolean = false,
    val draftAgentMode: Boolean = false,
    val agentInteractionBusy: Boolean = false,
    val artifactDownloading: String? = null,
    val taskNotificationsEnabled: Boolean = false,
)

/** UI state stays in this model; an opt-in task session may outlive the Activity. */
class AuraViewModel internal constructor(application: Application, private val repository: AppRepository,
    private val session: CompanionSession? = null) :
    AndroidViewModel(application), UiActions {
    constructor(application: Application) : this(application, CompanionSession.get(application).repository,
        CompanionSession.get(application))

    private var pendingArtifact: DownloadedArtifact? = null
    internal val conversationDraftStore = PrivateDraftStore(File(application.filesDir, "conversation-drafts.json")) { ids ->
        ids.associateWith { id -> DraftScope(repository.database.conversationGeneration(id), repository.database.isConversationDeleted(id)) }
    }
    private val preferences = application.getSharedPreferences("appearance", 0)
    private val local = MutableStateFlow(LocalUi(theme = runCatching {
        ThemeMode.valueOf(preferences.getString("theme", "LIGHT") ?: "LIGHT")
    }.getOrDefault(ThemeMode.LIGHT), taskNotificationsEnabled = preferences.getBoolean("task_notifications", false)))
    private var autoConnect = preferences.getBoolean("auto_connect", true)
    private var latestConnectionIntent = Any()
    private var pickerConversationId: String? = null
    private val foregroundSync = if (session == null) ForegroundSyncLoop(viewModelScope, repository.state, { autoConnect },
        reconnect = { repository.reconnect(silent = true) }, poll = { repository.poll() }, disconnect = { repository.disconnect() }) else null

    val state: StateFlow<UiState> = combine(repository.state, local) { data, ui -> mapState(data, ui) }
        .stateIn(viewModelScope, SharingStarted.Eagerly, UiState(theme = local.value.theme))

    init {
        viewModelScope.launch(Dispatchers.IO) {
            repository.state.collect {
                try { conversationDraftStore.refreshScopes() }
                catch (_: Exception) { local.update { it.copy(notice = "无法保存本机草稿，请检查手机存储空间。") } }
            }
        }
    }

    private fun mapState(data: AppState, ui: LocalUi): UiState {
        val outbox = data.outbox.filter { !it.hidden && it.status != "discarded" }
        val images = data.images.associateBy { it.id }
        val messages = conversationPresentation(data)
        val connection = ConnectionState.valueOf(data.connectionState.name)
        val detail = when (connection) {
            ConnectionState.CONNECTED -> when (data.connectionType) {
                "direct" -> "已直连电脑"
                "relay" -> "通过加密中继连接"
                else -> "已加密连接"
            }
            ConnectionState.CONNECTING -> "正在连接电脑"
            ConnectionState.UNAUTHORIZED -> "需要重新配对"
            ConnectionState.ERROR -> if (data.directApi.available) "电脑暂时离线 · 手机仍可直接对话" else "电脑连接暂不可用 · 可配置手机模型直接聊天"
            ConnectionState.DISCONNECTED -> when {
                data.directApi.available -> "电脑暂时离线 · 记录将在连接后同步"
                data.device != null -> "电脑当前离线 · 可配置手机模型直接聊天"
                else -> "配置手机模型即可聊天，也可以配对电脑同步记录"
            }
        }
        return UiState(
            paired = data.device != null,
            desktopName = data.device?.name ?: "我的电脑",
            connection = connection,
            connectionDetail = detail,
            sync = SyncState.valueOf(data.syncState.name),
            queuedCount = outbox.count { it.method != "chat.import" && it.status in setOf("queued", "sending", "unknown", "pending") },
            attentionCount = outbox.count { it.status in setOf("needs_attention", "failed") },
            queue = outbox.mapNotNull { item ->
                val status = runCatching { QueueStatus.valueOf(item.status.uppercase(Locale.ROOT)) }.getOrNull()
                    ?: return@mapNotNull null
                UiQueueItem(item.requestId, item.text, status, item.error.orEmpty(),
                    data.conversations.firstOrNull { it.id == item.conversationId }?.title.orEmpty(),
                    item.attachmentId?.let { images[it]?.filename }, isHistorySync = item.method == "chat.import",
                    canSendOnPhone = item.canSendOnPhone)
            },
            conversations = data.conversations.map { UiConversation(it.id, it.title,
                updatedAt = historyDate(it.updatedAt), localOnly = it.localOnly) },
            selectedConversationId = data.selectedConversationId,
            messages = messages,
            pendingAttachment = data.selectedConversationId?.let { id -> data.pendingImages[id] }?.let {
                UiPendingAttachment(it.attachmentId, it.filename, it.localPath.orEmpty(), it.conversationId,
                    preparing = ui.preparingImage)
            },
            preparingImage = ui.preparingImage,
            images = data.images.map { UiImage(it.id, it.filename,
                sizeLabel = if (it.size < 1024 * 1024) "${it.size / 1024} KB" else String.format(Locale.ROOT, "%.1f MB", it.size / 1048576.0),
                status = when (it.uploadState) { "staged" -> "待发送"; "queued" -> "等待发送"; "sent", "uploaded" -> "已发送到电脑"; else -> "电脑收件箱" },
                localUri = it.localPath,
                canDelete = connection == ConnectionState.CONNECTED) },
            pairing = ui.pairing, pairingError = ui.pairingError,
            notice = ui.notice ?: data.error, theme = ui.theme,
            petScale = ui.petScale, petVisible = ui.petVisible, pendingPairInfo = ui.pendingPairInfo,
            directApi = UiDirectApi(available = data.directApi.available, baseUrl = data.directApi.baseUrl,
                model = data.directApi.model, sync = ApiConfigSyncState.valueOf(data.directApi.sync.name),
                lastSyncedAt = data.directApi.lastSyncedAt?.let { historyDate(Instant.ofEpochMilli(it).toString()) }.orEmpty(),
                message = data.directApi.message, source = ApiConfigSource.valueOf(data.directApi.source.name),
                keySaved = data.directApi.keySaved, timeoutSeconds = data.directApi.timeoutSeconds,
                reasoningEffort = data.directApi.reasoningEffort, reasoningProfile = data.directApi.reasoningProfile,
                reasoningBudgetTokens = data.directApi.reasoningBudgetTokens, reasoningMetadata = data.directApi.reasoningMetadata),
            directChatBusy = data.directChatBusy, historyPendingCount = data.historyPendingCount,
            directChatConversationId = data.directChatConversationId,
            apiConfigSaving = ui.apiConfigSaving, apiConfigError = ui.apiConfigError,
            apiConfigSaveGeneration = ui.apiConfigSaveGeneration,
            desktopTaskAvailable = data.desktopTaskAvailable,
            desktopTaskMessage = data.desktopTaskMessage,
            desktopTaskBusy = ui.desktopTaskBusy,
            desktopAgentMode = data.selectedConversationId?.let { it in data.desktopAgentConversations } ?: ui.draftAgentMode,
            desktopTaskRunning = data.desktopAgentBusy && data.desktopAgentConversationId == data.selectedConversationId,
            desktopTaskActivity = data.messages.lastOrNull { it.executionMode == "agent" && it.requestStatus == "pending" }?.agentActivity.orEmpty(),
            desktopTaskError = data.messages.lastOrNull { it.executionMode == "agent" }?.agentError.orEmpty(),
            agentInteraction = data.agentInteraction?.takeIf { it.conversationId == data.selectedConversationId }?.let {
                UiAgentInteraction(it.id, it.kind, it.question, it.conversationId, it.options,
                    it.resource?.let { resource -> UiAgentFileResource(resource.path, resource.access, resource.directory) })
            },
            agentInteractionBusy = ui.agentInteractionBusy,
            canSteerDesktop = data.desktopSteeringAvailable && data.desktopAgentBusy &&
                data.desktopAgentConversationId == data.selectedConversationId,
            agentArtifacts = data.agentArtifacts.map { UiAgentArtifact(it.reference, it.name, it.bytes) },
            artifactDownloading = ui.artifactDownloading,
            taskNotificationsEnabled = ui.taskNotificationsEnabled,
            characterCards = data.characterCards.map { UiCharacterCard(it.id, it.label, it.name) },
            activeCharacterId = data.activeCharacterId,
            activeCharacterName = data.activeCharacterName,
            characterSyncBusy = data.characterSyncBusy,
            characterSyncError = data.characterSyncError,
            characterLibrarySupported = data.characterLibrarySupported,
        )
    }

    fun setForeground(value: Boolean) {
        if (value && local.value.taskNotificationsEnabled && !TaskNotifications.allowed(getApplication()))
            setTaskNotifications(false)
        session?.setForeground(value) ?: foregroundSync?.setForeground(value)
    }

    override fun setTaskNotifications(enabled: Boolean) {
        val allowed = enabled && TaskNotifications.allowed(getApplication())
        preferences.edit().putBoolean("task_notifications", allowed).apply()
        local.update { it.copy(taskNotificationsEnabled = allowed,
            notice = if (enabled && !allowed) "请在系统设置中允许鲸伴发送通知。" else it.notice) }
        session?.settingsChanged()
    }

    override fun refreshCharacters() {
        viewModelScope.launch { repository.refreshCharacters() }
    }

    override fun selectCharacter(id: String) {
        viewModelScope.launch { repository.selectCharacter(id) }
    }

    override fun pair(payload: String) {
        if (local.value.pairing) return
        val intent = Any().also { latestConnectionIntent = it }
        viewModelScope.launch {
            local.update { it.copy(pairing = true, pairingError = null, notice = null) }
            repository.pair(payload, "${Build.MANUFACTURER} ${Build.MODEL}".take(80))
            val data = repository.state.value
            val succeeded = data.connectionState == com.aura.companion.data.ConnectionState.CONNECTED && data.error == null
            if (succeeded && latestConnectionIntent === intent) {
                autoConnect = true
                preferences.edit().putBoolean("auto_connect", true).apply()
            }
            local.update { it.copy(pairing = false, pairingError = if (succeeded) null
                else data.error ?: "连接未完成，请检查配对信息和电脑状态") }
        }
    }

    override fun reconnect() {
        latestConnectionIntent = Any()
        autoConnect = true
        preferences.edit().putBoolean("auto_connect", true).apply()
        session?.settingsChanged()
        viewModelScope.launch { repository.reconnect() }
    }

    override fun disconnect() {
        latestConnectionIntent = Any()
        autoConnect = false
        preferences.edit().putBoolean("auto_connect", false).apply()
        session?.settingsChanged()
        viewModelScope.launch { repository.disconnect() }
    }

    override fun syncNow() { viewModelScope.launch { repository.syncNow() } }
    override fun selectConversation(id: String) { viewModelScope.launch { repository.selectConversation(id) } }
    override fun newConversation() { viewModelScope.launch { repository.newConversation() } }
    override fun sendMessage(text: String) { sendPhoneMessage(text) {} }

    override fun sendPhoneMessage(text: String, onAccepted: (Boolean) -> Unit) {
        if (!repository.state.value.directApi.available) {
            local.update { it.copy(notice = "请先配置手机模型，或连接电脑同步 API 配置。") }
            onAccepted(false)
            return
        }
        viewModelScope.launch {
            var accepted = false
            try {
                val id = repository.state.value.selectedConversationId ?: repository.newConversation()
                accepted = repository.sendMessage(id, text)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { local.update { it.copy(notice = "消息未提交，草稿已保留。") } }
            finally { onAccepted(accepted) }
        }
    }

    override fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit) {
        if (local.value.desktopTaskBusy) { onAccepted(false); return }
        if (!repository.state.value.desktopTaskAvailable || repository.state.value.directChatBusy) {
            local.update { it.copy(notice = repository.state.value.desktopTaskMessage.ifBlank { "请先连接电脑，再发送电脑任务。" }) }
            onAccepted(false)
            return
        }
        local.update { it.copy(desktopTaskBusy = true) }
        viewModelScope.launch {
            var accepted = false
            try {
                val id = repository.state.value.selectedConversationId ?: repository.newConversation()
                repository.setDesktopAgentMode(id, true)
                accepted = repository.sendDesktopMessage(id, text)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { local.update { it.copy(notice = "电脑任务未提交，请检查连接后重试。") } }
            finally {
                local.update { it.copy(desktopTaskBusy = false) }
                onAccepted(accepted)
            }
        }
    }

    override fun setDesktopAgentMode(enabled: Boolean) {
        val id = repository.state.value.selectedConversationId
        local.update { it.copy(draftAgentMode = enabled) }
        viewModelScope.launch {
            val target = id ?: repository.newConversation()
            repository.setDesktopAgentMode(target, enabled)
        }
    }

    override fun cancelDesktopTask() {
        val id = repository.state.value.selectedConversationId ?: return
        viewModelScope.launch { repository.cancelDesktopTask(id) }
    }

    override fun cancelDirectChat() {
        val id = repository.state.value.selectedConversationId ?: return
        viewModelScope.launch(Dispatchers.IO) { repository.cancelDirectChat(id) }
    }

    override fun answerAgentInteraction(id: String, answer: UiAgentAnswer, onAccepted: (Boolean) -> Unit) {
        val interaction = repository.state.value.agentInteraction
        if (local.value.agentInteractionBusy || interaction?.id != id ||
            interaction.conversationId != repository.state.value.selectedConversationId) { onAccepted(false); return }
        val payload = when (answer) {
            is UiAgentAnswer.Text -> JSONObject().put("text", answer.text)
            is UiAgentAnswer.Permission -> JSONObject().put("allowed", answer.allowed)
        }
        local.update { it.copy(agentInteractionBusy = true) }
        viewModelScope.launch {
            var accepted = false
            try { accepted = repository.answerAgentInteraction(interaction.conversationId, id, payload) }
            finally { local.update { it.copy(agentInteractionBusy = false) }; onAccepted(accepted) }
        }
    }

    override fun steerDesktopTask(text: String, onAccepted: (Boolean) -> Unit) {
        val data = repository.state.value
        val id = data.selectedConversationId
        if (id == null || local.value.desktopTaskBusy || !data.desktopAgentBusy || data.desktopAgentConversationId != id) {
            onAccepted(false); return
        }
        local.update { it.copy(desktopTaskBusy = true) }
        viewModelScope.launch {
            var accepted = false
            try { accepted = repository.steerDesktopTask(id, text) }
            finally { local.update { it.copy(desktopTaskBusy = false) }; onAccepted(accepted) }
        }
    }

    fun prepareArtifact(reference: String, onReady: (String, String) -> Unit) {
        val cid = repository.state.value.selectedConversationId ?: return
        if (local.value.artifactDownloading != null || pendingArtifact != null) return
        local.update { it.copy(artifactDownloading = reference) }
        viewModelScope.launch {
            try {
                val downloaded = repository.downloadArtifact(cid, reference)
                if (downloaded != null) {
                    pendingArtifact = downloaded
                    onReady(downloaded.artifact.name, downloaded.artifact.mime)
                }
            } finally { local.update { it.copy(artifactDownloading = null) } }
        }
    }

    /** The system document picker owns the destination and overwrite confirmation. */
    fun finishArtifactSave(uri: Uri?) {
        val downloaded = pendingArtifact ?: return
        pendingArtifact = null
        viewModelScope.launch {
            try {
                if (uri != null) withContext(Dispatchers.IO) {
                    requireNotNull(getApplication<Application>().contentResolver.openOutputStream(uri, "w")).use { output ->
                        downloaded.file.inputStream().use { it.copyTo(output) }
                    }
                    local.update { it.copy(notice = "文件已保存") }
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { local.update { it.copy(notice = "文件未能保存，请重新选择保存位置") } }
            finally { downloaded.file.delete() }
        }
    }

    override fun requestDesktopScreenshot() {
        if (local.value.desktopTaskBusy) return
        local.update { it.copy(desktopTaskBusy = true) }
        viewModelScope.launch {
            try {
                repository.requestDesktopScreenshot()?.let { openImage(it) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { local.update { it.copy(notice = "暂时无法查看电脑桌面，请检查电脑连接和屏幕感知授权。") } }
            finally { local.update { it.copy(desktopTaskBusy = false) } }
        }
    }

    override fun retryMessage(id: String) {
        val operation = repository.state.value.outbox.firstOrNull { it.messageId == id || it.requestId == id }
        if (operation == null) {
            local.update { it.copy(notice = "此消息已在电脑保存，请连接后同步最新状态") }
            return
        }
        viewModelScope.launch {
            if (operation.canSendOnPhone && repository.state.value.directApi.available) {
                repository.sendQueuedOnPhone(operation.requestId)
            } else repository.retryOutbox(operation.requestId)
        }
    }

    override fun discardOutbox(requestId: String) { viewModelScope.launch { repository.discardOutbox(requestId) } }

    override fun saveDirectApiConfig(input: UiApiConfigInput) {
        if (local.value.apiConfigSaving) return
        local.update { it.copy(apiConfigSaving = true, apiConfigError = null) }
        viewModelScope.launch {
            try {
                val saved = repository.saveDirectApiConfig(input.baseUrl, input.model, input.apiKey,
                    input.timeoutSeconds, input.reasoningEffort, input.reasoningProfile,
                    input.reasoningBudgetTokens, input.reasoningMetadata)
                val error = if (saved) null else repository.state.value.error ?: "模型配置未保存，请检查后重试。"
                repository.clearError()
                local.update { it.copy(apiConfigError = error, notice = if (saved) "已保存，手机可直接对话" else it.notice,
                    apiConfigSaveGeneration = if (saved) it.apiConfigSaveGeneration + 1 else it.apiConfigSaveGeneration) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) {
                local.update { it.copy(apiConfigError = "模型配置未保存，请检查后重试。") }
            } finally { local.update { it.copy(apiConfigSaving = false) } }
        }
    }

    override fun sendQueuedOnPhone(requestId: String) {
        viewModelScope.launch { repository.sendQueuedOnPhone(requestId) }
    }

    /** MainActivity delegates this action to the system photo picker. */
    override fun pickImage() = Unit

    fun beginImageSelection(launchPicker: () -> Unit) {
        if (local.value.preparingImage) return
        local.update { it.copy(preparingImage = true) }
        viewModelScope.launch {
            try {
                if (repository.state.value.device == null && !repository.state.value.directApi.available) {
                    local.update { it.copy(preparingImage = false, notice = "请先配置手机模型，或连接电脑同步 API 配置。") }
                    return@launch
                }
                pickerConversationId = repository.state.value.selectedConversationId ?: repository.newConversation()
                launchPicker()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) {
                pickerConversationId = null
                local.update { it.copy(preparingImage = false, notice = "暂时无法打开图片选择器，请稍后重试。") }
            }
        }
    }

    fun attachImage(uri: Uri?) {
        val conversation = pickerConversationId
        pickerConversationId = null
        if (uri == null) {
            local.update { it.copy(preparingImage = false) }
            return
        }
        if (conversation == null) {
            local.update { it.copy(preparingImage = false, notice = "图片选择已中断，请重新选择图片。") }
            return
        }
        viewModelScope.launch {
            try {
                val prepared = withContext(Dispatchers.IO) { ImagePreparer.prepare(getApplication(), uri) }
                repository.stageImage(conversation, prepared.bytes, prepared.filename, prepared.mimeType)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { local.update { it.copy(notice = "无法读取这张图片，请换一张后重试") } }
            finally { local.update { it.copy(preparingImage = false) } }
        }
    }

    override fun removePendingImage() {
        if (local.value.preparingImage) return
        val conversation = repository.state.value.selectedConversationId ?: return
        viewModelScope.launch { repository.removePendingImage(conversation) }
    }

    override fun petCommand(action: String, value: PetValue) {
        val raw: Any = when (value) { is PetValue.Text -> value.value; is PetValue.Number -> value.value; is PetValue.Toggle -> value.value }
        viewModelScope.launch {
            repository.petCommand(action, raw)
            if (repository.state.value.error == null) local.update { it.copy(
                petScale = if (action == "scale" && raw is Int) raw else it.petScale,
                petVisible = if (action == "visibility" && raw is Boolean) raw else it.petVisible) }
        }
    }

    override fun refreshImages() { viewModelScope.launch { repository.refreshImages() } }
    override fun openImage(id: String) {
        viewModelScope.launch {
            val file = repository.downloadImage(id)
            if (file == null) {
                local.update { it.copy(notice = "图片暂时无法打开，请连接电脑后再试") }
                return@launch
            }
            val app = getApplication<Application>()
            app.startActivity(Intent(app, ImagePreviewActivity::class.java)
                .putExtra("image_path", file.absolutePath).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    override fun deleteImage(id: String) { viewModelScope.launch { repository.deleteImage(id) } }
    override fun dismissNotice() { repository.clearError(); local.update { it.copy(notice = null) } }
    override fun setTheme(mode: ThemeMode) {
        preferences.edit().putString("theme", mode.name).apply()
        local.update { it.copy(theme = mode) }
    }

    fun acceptPairInfo(value: String?) {
        if (value != null && value.length in 1..32768) local.update { it.copy(pendingPairInfo = value, pairingError = null) }
    }
    override fun consumePairInfo() { local.update { it.copy(pendingPairInfo = null) } }

    override fun onCleared() {
        foregroundSync?.close()
        if (session == null) CoroutineScope(Dispatchers.IO).launch { repository.close() }
        super.onCleared()
    }

    private fun historyDate(value: String): String {
        if (value.isBlank()) return ""
        return runCatching {
            val zone = ZoneId.systemDefault()
            val moment = runCatching { Instant.parse(value).atZone(zone) }
                .getOrElse { LocalDateTime.parse(value).atZone(zone) }
            val today = LocalDate.now(zone)
            when (moment.toLocalDate()) {
                today -> "今天 ${moment.format(DateTimeFormatter.ofPattern("HH:mm"))}"
                today.minusDays(1) -> "昨天"
                else -> moment.format(DateTimeFormatter.ofPattern(if (moment.year == today.year) "M月d日" else "yyyy年M月d日"))
            }
        }.getOrElse { value.take(10) }
    }
}
