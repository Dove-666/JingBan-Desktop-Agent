package com.aura.companion.ui

/** UI-only models. The application maps its persisted records into these values. */
enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, UNAUTHORIZED, ERROR }
enum class SyncState { IDLE, SYNCING, READY, ERROR }
enum class ThemeMode { SYSTEM, LIGHT, DARK }
enum class MessageRole { USER, ASSISTANT, SYSTEM }
enum class MessageStatus { SENT, QUEUED, SENDING, PENDING, FAILED, UNCERTAIN }
enum class QueueStatus { QUEUED, SENDING, UNKNOWN, PENDING, FAILED, NEEDS_ATTENTION }
enum class ApiConfigSyncState { WAITING, SYNCING, READY, ERROR }
enum class ApiConfigSource { NONE, DESKTOP, LOCAL }

/** Safe presentation summary only. Credentials never belong in UI state. */
data class UiDirectApi(
    val available: Boolean = false,
    val baseUrl: String = "",
    val model: String = "",
    val sync: ApiConfigSyncState = ApiConfigSyncState.WAITING,
    val lastSyncedAt: String = "",
    val message: String = "",
    val source: ApiConfigSource = ApiConfigSource.NONE,
    val keySaved: Boolean = false,
    val timeoutSeconds: Int = 30,
    val reasoningEffort: String = "default",
    val reasoningProfile: String = "auto",
    val reasoningBudgetTokens: Int = 0,
    val reasoningMetadata: String? = null,
)

/** Ephemeral form submission; never store this object in saved UI state. */
class UiApiConfigInput(val baseUrl: String, val model: String, val apiKey: String,
                       val timeoutSeconds: Int, val reasoningEffort: String,
                       val reasoningProfile: String = "auto", val reasoningBudgetTokens: Int = 0,
                       val reasoningMetadata: String? = null) {
    override fun toString() = "UiApiConfigInput([private])"
}

data class UiConversation(
    val id: String,
    val title: String,
    val preview: String = "",
    val updatedAt: String = "",
    val localOnly: Boolean = false,
)

/** Character shelf labels only. Role prompts and credentials stay out of UI state. */
data class UiCharacterCard(val id: String, val label: String, val name: String)

/** Public application/tool observations only; no model reasoning or tool arguments. */
data class UiExecutionStep(
    val id: String,
    val kind: String,
    val label: String,
    val status: String,
    val elapsedMs: Long,
)

data class UiExecution(
    val state: String,
    val startedAt: String,
    val durationMs: Long,
    val events: List<UiExecutionStep>,
    val omittedCount: Int = 0,
)

data class UiMessage(
    val id: String,
    val role: MessageRole,
    val content: String,
    val status: MessageStatus = MessageStatus.SENT,
    val detail: String = "",
    val attachmentUri: String? = null,
    val retryable: Boolean = false,
    val attachmentId: String? = null,
    val retryOnPhone: Boolean = false,
    val desktopAgent: Boolean = false,
    val execution: UiExecution? = null,
)

data class UiPendingAttachment(
    val id: String,
    val filename: String,
    val localUri: String,
    val conversationId: String,
    val preparing: Boolean = false,
)

data class UiAgentFileResource(val path: String, val access: String, val directory: Boolean)
data class UiAgentInteraction(val id: String, val kind: String, val question: String,
    val conversationId: String, val options: List<String> = emptyList(), val resource: UiAgentFileResource? = null)
data class UiAgentArtifact(val reference: String, val name: String, val bytes: Long)
sealed interface UiAgentAnswer {
    data class Text(val text: String) : UiAgentAnswer
    data class Permission(val allowed: Boolean) : UiAgentAnswer
}

data class UiImage(
    val id: String,
    val filename: String,
    val sizeLabel: String = "",
    val status: String = "",
    val localUri: String? = null,
    val canDelete: Boolean = true,
)

data class UiQueueItem(
    val requestId: String,
    val content: String,
    val status: QueueStatus,
    val detail: String = "",
    val conversationTitle: String = "",
    val attachmentName: String? = null,
    val isHistorySync: Boolean = false,
    val canSendOnPhone: Boolean = false,
)

data class UiState(
    val paired: Boolean = false,
    val desktopName: String = "我的电脑",
    val connection: ConnectionState = ConnectionState.DISCONNECTED,
    val connectionDetail: String = "",
    val sync: SyncState = SyncState.IDLE,
    val queuedCount: Int = 0,
    val attentionCount: Int = 0,
    val conversations: List<UiConversation> = emptyList(),
    val selectedConversationId: String? = null,
    val messages: List<UiMessage> = emptyList(),
    val images: List<UiImage> = emptyList(),
    val queue: List<UiQueueItem> = emptyList(),
    val pendingAttachment: UiPendingAttachment? = null,
    val preparingImage: Boolean = false,
    val pairing: Boolean = false,
    val pairingError: String? = null,
    val pendingPairInfo: String? = null,
    val notice: String? = null,
    val theme: ThemeMode = ThemeMode.LIGHT,
    val petScale: Int = 100,
    val petVisible: Boolean = true,
    val directApi: UiDirectApi = UiDirectApi(),
    val directChatBusy: Boolean = false,
    val directChatConversationId: String? = null,
    val historyPendingCount: Int = 0,
    val apiConfigSaving: Boolean = false,
    val apiConfigError: String? = null,
    val apiConfigSaveGeneration: Long = 0,
    val desktopTaskAvailable: Boolean = false,
    val desktopTaskMessage: String = "",
    val desktopTaskBusy: Boolean = false,
    val desktopAgentMode: Boolean = false,
    val desktopTaskRunning: Boolean = false,
    val desktopTaskActivity: String = "",
    val desktopTaskError: String = "",
    val agentInteraction: UiAgentInteraction? = null,
    val agentInteractionBusy: Boolean = false,
    val canSteerDesktop: Boolean = false,
    val agentArtifacts: List<UiAgentArtifact> = emptyList(),
    val artifactDownloading: String? = null,
    val taskNotificationsEnabled: Boolean = false,
    val characterCards: List<UiCharacterCard> = emptyList(),
    val activeCharacterId: String? = null,
    val activeCharacterName: String = "鲸鱼娘",
    val characterSyncBusy: Boolean = false,
    val characterSyncError: String? = null,
    val characterLibrarySupported: Boolean = false,
) {
    val chatAvailable: Boolean get() = paired || directApi.available || conversations.isNotEmpty()
    val queueSummary: String get() = when {
        queuedCount > 0 && attentionCount > 0 -> "$queuedCount 条待发送 · $attentionCount 条需处理"
        attentionCount > 0 -> "$attentionCount 条消息需处理"
        queuedCount > 0 -> "$queuedCount 条消息待发送"
        else -> ""
    }
}

sealed interface PetValue {
    data class Text(val value: String) : PetValue
    data class Number(val value: Int) : PetValue
    data class Toggle(val value: Boolean) : PetValue
}

interface UiActions {
    fun pair(payload: String)
    fun consumePairInfo()
    fun reconnect()
    fun disconnect()
    fun syncNow()
    fun selectConversation(id: String)
    fun newConversation()
    fun sendMessage(text: String)
    fun sendPhoneMessage(text: String, onAccepted: (Boolean) -> Unit) { sendMessage(text); onAccepted(false) }
    fun retryMessage(id: String)
    fun discardOutbox(requestId: String)
    fun pickImage()
    fun removePendingImage()
    fun petCommand(action: String, value: PetValue)
    fun refreshImages()
    fun openImage(id: String)
    fun deleteImage(id: String)
    fun dismissNotice()
    fun setTheme(mode: ThemeMode)
    fun saveDirectApiConfig(input: UiApiConfigInput)
    fun sendQueuedOnPhone(requestId: String)
    fun sendDesktopMessage(text: String, onAccepted: (Boolean) -> Unit)
    fun requestDesktopScreenshot()
    fun setDesktopAgentMode(enabled: Boolean) {}
    fun cancelDesktopTask() {}
    fun cancelDirectChat() {}
    fun answerAgentInteraction(id: String, answer: UiAgentAnswer, onAccepted: (Boolean) -> Unit) { onAccepted(false) }
    fun steerDesktopTask(text: String, onAccepted: (Boolean) -> Unit) { onAccepted(false) }
    fun saveArtifact(reference: String) {}
    fun setTaskNotifications(enabled: Boolean) {}
    fun refreshCharacters() {}
    fun selectCharacter(id: String) {}
}
