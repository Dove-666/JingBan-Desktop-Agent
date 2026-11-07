package com.aura.companion

import com.aura.companion.data.CompanionDatabase
import com.aura.companion.data.Message
import com.aura.companion.data.OutboxItem
import com.aura.companion.data.parseExecutionProcess

/** Read public task phases only. Never load chat bodies, image data, or the history window. */
internal fun readTaskStatuses(database: CompanionDatabase, trackedMessageIds: Set<String>, outbox: List<OutboxItem>,
    trackedRequestIds: Set<String> = emptySet()): List<Message> {
    val rows = linkedMapOf<String, Message>()
    val columns = "id,conversation_id,role,request_status,execution_mode,agent_trace"
    fun query(condition: String, args: Array<String>?) {
        database.readableDatabase.rawQuery("SELECT $columns FROM messages WHERE role='user' AND execution_mode='agent' AND ($condition)", args)
            .use { cursor ->
                while (cursor.moveToNext()) {
                    fun text(index: Int): String? = if (cursor.isNull(index)) null else cursor.getString(index)
                    val id = cursor.getString(0)
                    rows[id] = Message(id, cursor.getString(1), cursor.getString(2), "", "",
                        requestStatus = text(3), executionMode = text(4), execution = parseExecutionProcess(text(5)))
                }
            }
    }
    query("request_status IN ('pending','queued')", null)
    val ids = trackedMessageIds + outbox.filter { it.method == "agent.send" &&
        (it.requestId in trackedRequestIds || it.status in setOf("queued", "sending", "unknown", "pending")) }
        .mapNotNull { it.messageId }
    // Older Android SQLite builds have a 999-variable limit.
    for (chunk in ids.chunked(400)) query("id IN (${chunk.joinToString(",") { "?" }})", chunk.toTypedArray())
    return rows.values.toList()
}
