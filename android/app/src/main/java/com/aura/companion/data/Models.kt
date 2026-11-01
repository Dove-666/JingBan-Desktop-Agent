package com.aura.companion.data

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, UNAUTHORIZED, ERROR }
enum class SyncState { IDLE, SYNCING, READY, ERROR }
enum class ApiConfigSyncState { WAITING, SYNCING, READY, ERROR }
enum class ApiConfigSource { NONE, DESKTOP, LOCAL }

/** Public summary only. API credentials and the system prompt never enter UI state. */
data class ApiConnectionInfo(
    val available: Boolean = false,
    val baseUrl: String = "",
    val model: String = "",
    val sync: ApiConfigSyncState = ApiConfigSyncState.WAITING,
    val lastSyncedAt: Long? = null,
    val message: String = "请配置手机模型，或连接电脑自动同步 API 配置",
    val source: ApiConfigSource = ApiConfigSource.NONE,
    val keySaved: Boolean = false,
    val timeoutSeconds: Int = 30,
    val reasoningEffort: String = "default",
)

/** Editable public fields only. The key is supplied separately for a single save. */
data class ApiConfigDraft(val baseUrl: String = "", val model: String = "", val timeoutSeconds: Int = 30,
    val reasoningEffort: String = "default", val keySaved: Boolean = false, val source: ApiConfigSource = ApiConfigSource.NONE)

data class DeviceInfo(val name: String, val endpointId: String, val ticket: String) {
    val id: String get() = endpointId
}

data class Conversation(
    val id: String,
    val title: String,
    val createdAt: String,
    val updatedAt: String,
    val messageCount: Int = 0,
    val localOnly: Boolean = false,
)

data class Message(
    val id: String,
    val conversationId: String,
    val role: String,
    val content: String,
    val createdAt: String,
    val source: String = "user",
    val requestStatus: String? = null,
    val replyTo: String? = null,
    val localState: String? = null,
    val attachmentId: String? = null,
    val imageError: String? = null,
    val localError: String? = null,
    val agentErrorCode: String? = null,
    val agentError: String? = null,
    val agentActivity: String? = null,
    val executionMode: String? = null,
    val execution: ExecutionProcess? = null,
)

/** status: queued, sending, unknown, pending, sent, failed, needs_attention, discarded. */
data class OutboxItem(
    val requestId: String,
    val method: String,
    val conversationId: String?,
    val text: String,
    val status: String,
    val createdAt: String,
    val error: String? = null,
    val messageId: String? = null,
    val attachmentId: String? = null,
    val hidden: Boolean = false,
    val canSendOnPhone: Boolean = false,
)

data class ImageItem(
    val id: String,
    val filename: String,
    val mimeType: String,
    val size: Long,
    val width: Int,
    val height: Int,
    val createdAt: String,
    val localPath: String? = null,
    val uploadState: String = "received",
)

/** One local composer attachment per conversation; choosing it never sends a request. */
data class PendingImage(
    val conversationId: String,
    val attachmentId: String,
    val filename: String,
    val mimeType: String,
    val sizeBytes: Long,
    val localPath: String?,
)

data class AppState(
    val device: DeviceInfo? = null,
    val connectionState: ConnectionState = ConnectionState.DISCONNECTED,
    val connectionType: String = "unknown",
    val syncState: SyncState = SyncState.IDLE,
    val error: String? = null,
    val conversations: List<Conversation> = emptyList(),
    /** Only the selected conversation's latest 500 messages are held in memory. */
    val messages: List<Message> = emptyList(),
    val outbox: List<OutboxItem> = emptyList(),
    val images: List<ImageItem> = emptyList(),
    val pendingImages: Map<String, PendingImage> = emptyMap(),
    val selectedConversationId: String? = null,
    val lastSyncedAt: Long? = null,
    val directApi: ApiConnectionInfo = ApiConnectionInfo(),
    val directChatBusy: Boolean = false,
    val historyPendingCount: Int = 0,
    val desktopTaskAvailable: Boolean = false,
    val desktopTaskMessage: String = "请先连接电脑后使用电脑 Agent",
    val desktopAgentBusy: Boolean = false,
    val desktopAgentConversationId: String? = null,
    val desktopAgentConversations: Set<String> = emptySet(),
)
