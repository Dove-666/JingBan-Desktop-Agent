package com.aura.companion.ui

/** UI-only models. The application maps its persisted records into these values. */
enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, UNAUTHORIZED, ERROR }
enum class SyncState { IDLE, SYNCING, READY, ERROR }
enum class ThemeMode { SYSTEM, LIGHT, DARK }
enum class MessageRole { USER, ASSISTANT, SYSTEM }
enum class MessageStatus { SENT, QUEUED, SENDING, PENDING, FAILED, UNCERTAIN }
enum class QueueStatus { QUEUED, SENDING, UNKNOWN, PENDING, FAILED, NEEDS_ATTENTION }

data class UiConversation(
    val id: String,
    val title: String,
    val preview: String = "",
    val updatedAt: String = "",
    val localOnly: Boolean = false,
)

data class UiMessage(
    val id: String,
    val role: MessageRole,
    val content: String,
    val status: MessageStatus = MessageStatus.SENT,
    val detail: String = "",
    val attachmentUri: String? = null,
    val retryable: Boolean = false,
)

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
)

data class UiState(
    val paired: Boolean = false,
    val desktopName: String = "我的电脑",
    val connection: ConnectionState = ConnectionState.DISCONNECTED,
    val connectionDetail: String = "",
    val sync: SyncState = SyncState.IDLE,
    val queuedCount: Int = 0,
    val conversations: List<UiConversation> = emptyList(),
    val selectedConversationId: String? = null,
    val messages: List<UiMessage> = emptyList(),
    val images: List<UiImage> = emptyList(),
    val queue: List<UiQueueItem> = emptyList(),
    val pairing: Boolean = false,
    val pairingError: String? = null,
    val pendingPairInfo: String? = null,
    val notice: String? = null,
    val theme: ThemeMode = ThemeMode.SYSTEM,
    val petScale: Int = 100,
    val petVisible: Boolean = true,
)

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
    fun retryMessage(id: String)
    fun discardOutbox(requestId: String)
    fun pickImage()
    fun petCommand(action: String, value: PetValue)
    fun refreshImages()
    fun openImage(id: String)
    fun deleteImage(id: String)
    fun dismissNotice()
    fun setTheme(mode: ThemeMode)
}
