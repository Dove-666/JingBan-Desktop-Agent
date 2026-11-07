package com.aura.companion

import com.aura.companion.data.AppState
import com.aura.companion.data.Message

internal data class TaskCompletion(val requestId: String, val conversationId: String, val outcome: String)

/** Track observed requests, never infer success from a lost connection or a historical reply. */
internal class TaskCompletionTracker {
    private data class Request(val conversation: String, val message: String)
    private val active = linkedMapOf<String, Request>()
    private val finished = linkedMapOf<String, String>()
    private var authority: String? = null
    val pendingCount: Int get() = active.size
    val conversationIds: Set<String> get() = active.values.map { it.conversation }.toSet()
    val messageIds: Set<String> get() = active.values.map { it.message }.toSet()
    val requestIds: Set<String> get() = active.keys.toSet()

    fun clear() { active.clear(); finished.clear(); authority = null }

    fun observe(state: AppState, messages: List<Message> = state.messages): List<TaskCompletion> {
        if (authority != state.device?.id) { active.clear(); finished.clear(); authority = state.device?.id }
        val byId = messages.associateBy { it.id }
        val operations = state.outbox.filter { it.method == "agent.send" }.associateBy { it.requestId }
        for (operation in operations.values) {
            val cid = operation.conversationId ?: continue
            val id = operation.messageId ?: operation.requestId
            val message = byId[id]
            if (finished[operation.requestId] == "needs_user" && message?.execution?.state == "running") {
                finished.remove(operation.requestId); finished.remove(id)
            }
            if (operation.requestId in finished || id in finished) continue
            if (terminal(message) == null && (operation.status in setOf("queued", "sending", "unknown", "pending") ||
                message?.requestStatus in setOf("queued", "pending") || message?.execution?.state == "running")) {
                active[operation.requestId] = Request(cid, id)
            } else if (operation.requestId in active) active[operation.requestId] = Request(cid, id)
        }
        // A task started on the computer also qualifies once its running record is observed.
        for (message in messages) {
            if (finished[message.id] == "needs_user" && message.execution?.state == "running") finished.remove(message.id)
            if (message.role == "user" && message.executionMode == "agent" &&
                message.id !in finished && terminal(message) == null &&
                (message.requestStatus in setOf("queued", "pending") || message.execution?.state == "running") &&
                // A database pull can overtake a previously captured queue-state
                // emission. Wait for its canonical ID before adopting it twice.
                operations.values.none { it.conversationId == message.conversationId && it.messageId == null &&
                    it.status in setOf("queued", "sending", "unknown", "pending") } &&
                active.values.none { it.message == message.id }) {
                active[message.id] = Request(message.conversationId, message.id)
            }
        }
        val completed = mutableListOf<TaskCompletion>()
        for ((id, request) in active.toMap()) {
            val operation = operations[id]
            val message = byId[request.message]
            val outcome = when {
                operation?.status == "discarded" -> "discarded"
                terminal(message) != null -> terminal(message)
                operation?.status in setOf("failed", "needs_attention") -> "failed"
                else -> null
            }
            if (outcome != null) {
                active.remove(id)
                finished[id] = outcome
                finished[request.message] = outcome
                while (finished.size > 512) finished.remove(finished.keys.first())
                if (outcome != "discarded") completed += TaskCompletion(id, request.conversation, outcome)
            } else if (state.conversations.none { it.id == request.conversation } &&
                state.outbox.none { it.requestId == id }) active.remove(id)
        }
        return completed
    }

    private fun terminal(message: Message?): String? = when {
        message?.execution?.state in setOf("completed", "failed", "cancelled", "interrupted", "needs_user") -> message!!.execution!!.state
        message?.requestStatus == "complete" -> "completed"
        message?.requestStatus in setOf("failed", "cancelled", "stopped") -> message!!.requestStatus
        else -> null
    }
}
