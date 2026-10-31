package com.aura.companion

import android.app.Application
import android.content.Intent
import android.net.Uri
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aura.companion.data.AppRepository
import com.aura.companion.data.AppState
import com.aura.companion.transport.IrohRpcClient
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

private data class LocalUi(
    val pairing: Boolean = false,
    val pairingError: String? = null,
    val notice: String? = null,
    val theme: ThemeMode = ThemeMode.SYSTEM,
    val petScale: Int = 100,
    val petVisible: Boolean = true,
    val pendingPairInfo: String? = null,
)

/** Owns only foreground work. Records and unacknowledged requests live in SQLite. */
class AuraViewModel(application: Application) : AndroidViewModel(application), UiActions {
    private val repository = AppRepository(application, IrohRpcClient(application))
    private val preferences = application.getSharedPreferences("appearance", 0)
    private val local = MutableStateFlow(LocalUi(theme = runCatching {
        ThemeMode.valueOf(preferences.getString("theme", "SYSTEM") ?: "SYSTEM")
    }.getOrDefault(ThemeMode.SYSTEM)))
    private var active = false
    private var foregroundJob: Job? = null
    private var idleCloseJob: Job? = null
    private var autoConnect = preferences.getBoolean("auto_connect", true)

    val state: StateFlow<UiState> = combine(repository.state, local) { data, ui -> mapState(data, ui) }
        .stateIn(viewModelScope, SharingStarted.Eagerly, UiState(theme = local.value.theme))

    private fun mapState(data: AppState, ui: LocalUi): UiState {
        val outbox = data.outbox.filter { it.status != "discarded" }
        val images = data.images.associateBy { it.id }
        val messages = data.messages.map { message ->
            val queued = outbox.firstOrNull { it.messageId == message.id || it.requestId == message.id }
            val status = when (message.localState ?: message.requestStatus ?: queued?.status) {
                "queued" -> MessageStatus.QUEUED
                "sending" -> MessageStatus.SENDING
                "pending" -> MessageStatus.PENDING
                "failed", "cancelled", "interrupted" -> MessageStatus.FAILED
                "unknown", "needs_attention" -> MessageStatus.UNCERTAIN
                else -> MessageStatus.SENT
            }
            UiMessage(message.id, when (message.role) {
                "assistant" -> MessageRole.ASSISTANT
                "user" -> MessageRole.USER
                else -> MessageRole.SYSTEM
            }, message.content, status,
                detail = queued?.error.orEmpty(),
                attachmentUri = message.attachmentId?.let { images[it]?.localPath },
                retryable = queued?.status in setOf("queued", "unknown", "failed"))
        }
        val connection = ConnectionState.valueOf(data.connectionState.name)
        val detail = when (connection) {
            ConnectionState.CONNECTED -> when (data.connectionType) {
                "direct" -> "已直连电脑"
                "relay" -> "通过加密中继连接"
                else -> "已加密连接"
            }
            ConnectionState.CONNECTING -> "正在连接电脑"
            ConnectionState.UNAUTHORIZED -> "需要重新配对"
            ConnectionState.ERROR -> "连接暂不可用 · 内容已保存在本机"
            ConnectionState.DISCONNECTED -> if (data.device != null) "离线可查看历史，消息将在连接后发送" else "连接你的电脑，继续同一段对话"
        }
        return UiState(
            paired = data.device != null,
            desktopName = data.device?.name ?: "我的电脑",
            connection = connection,
            connectionDetail = detail,
            sync = SyncState.valueOf(data.syncState.name),
            queuedCount = outbox.count { it.status in setOf("queued", "sending", "unknown", "pending", "needs_attention") },
            queue = outbox.mapNotNull { item ->
                val status = runCatching { QueueStatus.valueOf(item.status.uppercase(Locale.ROOT)) }.getOrNull()
                    ?: return@mapNotNull null
                UiQueueItem(item.requestId, item.text, status, item.error.orEmpty(),
                    data.conversations.firstOrNull { it.id == item.conversationId }?.title.orEmpty(),
                    item.attachmentId?.let { images[it]?.filename })
            },
            conversations = data.conversations.map { UiConversation(it.id, it.title,
                updatedAt = historyDate(it.updatedAt), localOnly = it.localOnly) },
            selectedConversationId = data.selectedConversationId,
            messages = messages,
            images = data.images.map { UiImage(it.id, it.filename,
                sizeLabel = if (it.size < 1024 * 1024) "${it.size / 1024} KB" else String.format(Locale.ROOT, "%.1f MB", it.size / 1048576.0),
                status = when (it.uploadState) { "queued" -> "等待发送"; "sent", "uploaded" -> "已发送到电脑"; else -> "电脑收件箱" },
                localUri = it.localPath,
                canDelete = connection == ConnectionState.CONNECTED) },
            pairing = ui.pairing, pairingError = ui.pairingError,
            notice = ui.notice ?: data.error, theme = ui.theme,
            petScale = ui.petScale, petVisible = ui.petVisible, pendingPairInfo = ui.pendingPairInfo,
        )
    }

    fun setForeground(value: Boolean) {
        active = value
        idleCloseJob?.cancel()
        foregroundJob?.cancel()
        if (!value) {
            idleCloseJob = viewModelScope.launch {
                delay(3000)
                if (!active) repository.disconnect()
            }
            return
        }
        foregroundJob = viewModelScope.launch {
            var retryDelay = 2000L
            while (isActive && active) {
                val data = repository.state.value
                if (autoConnect && data.device != null) {
                    when (data.connectionState) {
                        com.aura.companion.data.ConnectionState.CONNECTED -> {
                            repository.syncNow()
                            retryDelay = 2000
                        }
                        com.aura.companion.data.ConnectionState.DISCONNECTED,
                        com.aura.companion.data.ConnectionState.ERROR -> {
                            repository.reconnect()
                            if (repository.state.value.connectionState != com.aura.companion.data.ConnectionState.CONNECTED) {
                                delay(retryDelay)
                                retryDelay = (retryDelay * 2).coerceAtMost(30_000)
                            }
                        }
                        else -> Unit
                    }
                }
                delay(2000)
            }
        }
    }

    override fun pair(payload: String) {
        if (local.value.pairing) return
        viewModelScope.launch {
            local.update { it.copy(pairing = true, pairingError = null, notice = null) }
            repository.pair(payload, "${Build.MANUFACTURER} ${Build.MODEL}".take(80))
            val data = repository.state.value
            val succeeded = data.connectionState == com.aura.companion.data.ConnectionState.CONNECTED && data.error == null
            if (succeeded) {
                autoConnect = true
                preferences.edit().putBoolean("auto_connect", true).apply()
            }
            local.update { it.copy(pairing = false, pairingError = if (succeeded) null
                else data.error ?: "连接未完成，请检查配对信息和电脑状态") }
        }
    }

    override fun reconnect() {
        autoConnect = true
        preferences.edit().putBoolean("auto_connect", true).apply()
        viewModelScope.launch { repository.reconnect() }
    }

    override fun disconnect() {
        autoConnect = false
        preferences.edit().putBoolean("auto_connect", false).apply()
        viewModelScope.launch { repository.disconnect() }
    }

    override fun syncNow() { viewModelScope.launch { repository.syncNow() } }
    override fun selectConversation(id: String) { viewModelScope.launch { repository.selectConversation(id) } }
    override fun newConversation() { viewModelScope.launch { repository.newConversation() } }
    override fun sendMessage(text: String) {
        if (repository.state.value.device == null) {
            local.update { it.copy(notice = "先连接电脑，再开始对话") }
            return
        }
        viewModelScope.launch {
            val id = repository.state.value.selectedConversationId ?: repository.newConversation()
            repository.sendMessage(id, text)
        }
    }

    override fun retryMessage(id: String) {
        val operation = repository.state.value.outbox.firstOrNull { it.messageId == id || it.requestId == id }
        if (operation == null) {
            local.update { it.copy(notice = "此消息已在电脑保存，请连接后同步最新状态") }
            return
        }
        viewModelScope.launch { repository.retryOutbox(operation.requestId) }
    }

    override fun discardOutbox(requestId: String) { viewModelScope.launch { repository.discardOutbox(requestId) } }

    /** MainActivity delegates this action to the system photo picker. */
    override fun pickImage() = Unit

    fun attachImage(uri: Uri) {
        viewModelScope.launch {
            try {
                val prepared = withContext(Dispatchers.IO) { ImagePreparer.prepare(getApplication(), uri) }
                repository.sendImage(prepared.bytes, prepared.filename, prepared.mimeType)
            } catch (_: CancellationException) { throw CancellationException() }
            catch (_: Exception) { local.update { it.copy(notice = "无法读取这张图片，请换一张后重试") } }
        }
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
        foregroundJob?.cancel()
        idleCloseJob?.cancel()
        CoroutineScope(Dispatchers.IO).launch { repository.close() }
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
