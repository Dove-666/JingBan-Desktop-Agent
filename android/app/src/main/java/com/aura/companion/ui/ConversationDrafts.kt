package com.aura.companion.ui

import android.os.Bundle
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.Saver
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.launch
import java.util.UUID

internal data class DraftSnapshot(val text: String = "", val revision: Long = 0)
internal data class DraftSubmission(val serial: Long, val snapshot: DraftSnapshot, val identity: String = UUID.randomUUID().toString())

/** Activity-scoped drafts survive navigation and recreation without putting every draft in a Bundle. */
internal class ConversationDrafts(private val store: DraftStore? = null) : ViewModel() {
    private val drafts = mutableStateMapOf<String?, DraftSnapshot>()
    private val submissions = mutableMapOf<DraftSubmission, String?>()
    private var scopes: Map<String, DraftScope> = emptyMap()
    private val dirty = mutableSetOf<String?>()
    private val pendingClears = mutableMapOf<String?, DraftSnapshot>()
    private var revision = 0L
    private var serial = 0L
    var persistenceError by mutableStateOf<String?>(null)
        private set
    val durable get() = store != null
    var preserveFirstConversation = false
        private set

    init {
        refresh()
        if (store != null) viewModelScope.launch { store.changes.collect { refresh(fromNotification = true) } }
    }

    private fun apply(value: PersistedDrafts) {
        drafts.clear(); drafts.putAll(value.entries); scopes = value.scopes; revision = value.revision
    }

    private fun refresh(fromNotification: Boolean = false) {
        if (store == null) return
        try {
            val value = if (pendingClears.isNotEmpty()) store.update(::clearAcceptedSnapshots)
                else if (fromNotification && persistenceError == null) store.committed ?: store.load() else store.load()
            pendingClears.clear()
            // Explicit deletion/reset wins over an unsaved edit of that scope;
            // independent unsaved drafts still retain their complete text.
            dirty.removeAll { id -> id != null && value.scopes[id]?.let { current ->
                current.deleted || (scopes[id]?.generation != null && scopes[id]?.generation != current.generation)
            } == true }
            val pending = dirty.associateWith { drafts[it] ?: DraftSnapshot() }
            apply(value.copy(revision = maxOf(value.revision, revision), entries = value.entries + pending,
                scopes = scopes + value.scopes))
            if (dirty.isEmpty()) persistenceError = null
        } catch (_: Exception) {
            if (persistenceError == null) persistenceError = "无法读取本机草稿，请检查存储空间；当前文字不会发送。"
        }
    }

    private fun clearAcceptedSnapshots(value: PersistedDrafts): PersistedDrafts {
        var next = value
        for ((id, snapshot) in pendingClears.toMap()) {
            if ((next.entries[id] ?: DraftSnapshot()) == snapshot) next = next.copy(revision = next.revision + 1,
                entries = next.entries + (id to DraftSnapshot("", next.revision + 1)))
        }
        return next
    }

    private fun mutate(keepOnFailure: Boolean = false,
        failureMessage: String = "草稿尚未保存，请保留当前文字并检查存储空间；当前文字不会发送。",
        change: (PersistedDrafts) -> PersistedDrafts): Boolean {
        val memory = PersistedDrafts(revision, drafts.toMap(), scopes)
        if (store == null) { apply(change(memory)); return true }
        try {
            val committed = store.update { disk ->
                val cleared = clearAcceptedSnapshots(disk)
                val merged = if (dirty.isEmpty()) cleared else cleared.copy(revision = maxOf(cleared.revision, revision),
                    entries = cleared.entries + dirty.associateWith { drafts[it] ?: DraftSnapshot() })
                change(merged)
            }
            pendingClears.clear(); dirty.clear(); apply(committed); persistenceError = null
            return true
        } catch (_: Exception) {
            if (!keepOnFailure) {
                val next = change(memory)
                dirty += next.entries.keys.filter { next.entries[it] != memory.entries[it] }
                apply(next)
            }
            persistenceError = failureMessage
            return false
        }
    }

    fun read(conversationId: String?): DraftSnapshot = drafts[conversationId] ?: DraftSnapshot()

    fun edit(conversationId: String?, text: String) {
        if (read(conversationId).text != text) mutate { value ->
            value.copy(revision = value.revision + 1,
                entries = value.entries + (conversationId to DraftSnapshot(text, value.revision + 1)))
        }
    }

    fun preserveFirstDraft(conversationId: String?) {
        preserveFirstConversation = conversationId == null
    }

    fun cancelFirstMigration() {
        preserveFirstConversation = false
        // An unresolved first request must never clear a manually selected conversation.
        submissions.entries.removeAll { it.value == null }
    }

    fun migrateFirstDraft(conversationId: String) {
        mutate { value ->
            val source = value.entries[null]; val destination = value.entries[conversationId]
            val entries = value.entries.toMutableMap()
            if (source != null && (destination == null || source.revision > destination.revision)) entries[conversationId] = source
            entries[null] = DraftSnapshot("", value.revision + 1)
            value.copy(revision = value.revision + 1, entries = entries)
        }
        submissions.entries.forEach { if (it.value == null) it.setValue(conversationId) }
        preserveFirstConversation = false
    }

    fun beginSubmission(conversationId: String?): DraftSubmission {
        preserveFirstDraft(conversationId)
        val ticket = DraftSubmission(++serial, read(conversationId))
        submissions[ticket] = conversationId
        return ticket
    }

    fun acknowledge(ticket: DraftSubmission, accepted: Boolean) {
        if (!submissions.containsKey(ticket)) return
        val conversationId = submissions.remove(ticket)
        if (accepted && !mutate(keepOnFailure = true,
            failureMessage = "消息已提交，但草稿清除尚未保存；请勿重复发送，并检查存储空间。") { value ->
            if ((value.entries[conversationId] ?: DraftSnapshot()) != ticket.snapshot) value
            else value.copy(revision = value.revision + 1,
                entries = value.entries + (conversationId to DraftSnapshot("", value.revision + 1)))
        }) pendingClears[conversationId] = ticket.snapshot
    }

    fun discard(conversationId: String?) {
        submissions.entries.removeAll { it.value == conversationId }
        val snapshot = read(conversationId)
        if (!mutate(keepOnFailure = true,
            failureMessage = "草稿清除尚未保存，请勿发送当前文字，并检查存储空间。") { value -> value.copy(revision = value.revision + 1,
            entries = value.entries + (conversationId to DraftSnapshot("", value.revision + 1))) }) pendingClears[conversationId] = snapshot
    }

    fun restoreCurrent(conversationId: String?, text: String) {
        // A retained ViewModel may already contain a newer edit or an acknowledged empty draft.
        if (!durable && !drafts.containsKey(conversationId)) drafts[conversationId] = DraftSnapshot(text, ++revision)
    }
}

/** Only the currently visible draft is persisted across process death. */
internal class CurrentDraftCursor(val drafts: ConversationDrafts, var conversationId: String?) {
    companion object {
        fun saver(drafts: ConversationDrafts) = Saver<CurrentDraftCursor, Bundle>(
            save = { cursor -> Bundle().apply {
                putString("conversation", cursor.conversationId)
                if (!drafts.durable) putString("text", drafts.read(cursor.conversationId).text)
            } },
            restore = { saved ->
                val conversationId = saved.getString("conversation")
                drafts.restoreCurrent(conversationId, saved.getString("text").orEmpty())
                CurrentDraftCursor(drafts, conversationId)
            },
        )
    }
}
