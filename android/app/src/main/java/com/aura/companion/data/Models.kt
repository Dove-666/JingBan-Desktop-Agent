package com.aura.companion.data

enum class ConnectionState { DISCONNECTED, CONNECTING, CONNECTED, UNAUTHORIZED, ERROR }
enum class SyncState { IDLE, SYNCING, READY, ERROR }

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
    val selectedConversationId: String? = null,
    val lastSyncedAt: Long? = null,
)
