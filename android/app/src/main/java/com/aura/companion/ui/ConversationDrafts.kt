package com.aura.companion.ui

import android.os.Bundle
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.Saver
import androidx.lifecycle.ViewModel
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.drop
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.UUID

internal data class DraftSnapshot(val text: String = "", val revision: Long = 0, val identity: String = "")
internal data class DraftSubmission(val serial: Long, val snapshot: DraftSnapshot, val identity: String = UUID.randomUUID().toString())
private data class SubmissionOwner(var conversationId: String?, var snapshot: DraftSnapshot)

/** UI edits are immediate. Disk transactions run in order, with at most one queued edit batch. */
internal class ConversationDrafts(private val store: DraftStore? = null,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    uiDispatcher: CoroutineDispatcher = Dispatchers.Main.immediate) : ViewModel() {
    private val drafts = mutableStateMapOf<String?, DraftSnapshot>()
    private val submissions = mutableMapOf<DraftSubmission, SubmissionOwner>()
    private var scopes: Map<String, DraftScope> = emptyMap()
    private val dirty = mutableSetOf<String?>()
    private val pendingClears = mutableMapOf<String?, DraftSnapshot>()
    private val discarded = mutableSetOf<String?>()
    private val clearAncestors = mutableMapOf<String?, Set<String>>()
    private val clearTargets = mutableMapOf<String?, DraftSnapshot>()
    private var committed = PersistedDrafts()
    private var inFlightEdits: Map<String?, DraftSnapshot> = emptyMap()
    private val recoveries = mutableMapOf<String?, Bundle>()
    private var revision = 0L
    private var serial = 0L
    private val persistenceScope = CoroutineScope(SupervisorJob() + uiDispatcher)
    private val transaction = Mutex()
    private var changeObserver: Job? = null
    private var retired = false
    private var flushQueued = false
    private var operations by mutableStateOf(0)
    var loaded by mutableStateOf(store == null)
        private set
    var persistenceError by mutableStateOf<String?>(null)
        private set
    var clearingSubmittedDraft by mutableStateOf(false)
        private set
    val durable get() = store != null
    val saving get() = !loaded || operations > 0
    var preserveFirstConversation = false
        private set

    init {
        if (store != null) {
            enqueue { refresh(); loaded = true }
            changeObserver = persistenceScope.launch { store.changes.drop(1).collect {
                enqueue { refresh(fromNotification = true) }
            } }
        }
    }

    private fun enqueue(action: suspend () -> Unit) {
        operations++
        persistenceScope.launch {
            try { transaction.withLock { action() } }
            finally { operations--; closeWhenDrained() }
        }
    }

    private fun closeWhenDrained() {
        if (retired && operations == 0 && submissions.isEmpty()) persistenceScope.cancel()
    }

    private fun apply(value: PersistedDrafts) {
        drafts.clear(); drafts.putAll(value.entries); scopes = value.scopes; revision = value.revision
    }

    private fun invalidScope(id: String?, current: Map<String, DraftScope>, previous: Map<String, DraftScope>): Boolean =
        id != null && current[id]?.let { it.deleted ||
            (previous[id]?.generation != null && previous[id]?.generation != it.generation) } == true

    private fun mergeCommitted(value: PersistedDrafts) {
        committed = value
        dirty.removeAll { invalidScope(it, value.scopes, scopes) }
        val pending = dirty.associateWith { drafts[it] ?: DraftSnapshot() }
        apply(value.copy(revision = maxOf(value.revision, revision), entries = value.entries + pending,
            scopes = scopes + value.scopes))
    }

    private suspend fun refresh(fromNotification: Boolean = false) {
        val target = store ?: return
        try {
            val useCached = fromNotification && persistenceError == null
            val value = withContext(ioDispatcher) {
                if (useCached) target.committed ?: target.load() else target.load()
            }
            mergeCommitted(value)
            if (recoveries.isNotEmpty()) recoveries.toMap().forEach { (id, saved) -> restoreRecovery(id, saved) }
            else if (pendingClears.isNotEmpty()) persistEdits()
            else if (dirty.isEmpty()) persistenceError = null
        } catch (_: Exception) {
            if (persistenceError == null) persistenceError = "无法读取本机草稿，请检查存储空间；当前文字不会发送。"
        }
    }

    private fun clearAcceptedSnapshots(value: PersistedDrafts, clears: Map<String?, DraftSnapshot>, targets: Map<String?, DraftSnapshot>,
        ancestors: Map<String?, Set<String>>): PersistedDrafts {
        var next = value
        for ((id, snapshot) in clears) {
            val signature = stamp(next.entries[id] ?: DraftSnapshot())
            if (signature == stamp(snapshot) || signature in ancestors[id].orEmpty()) next = next.copy(revision = next.revision + 1,
                entries = next.entries + (id to targets.getValue(id).copy(revision = next.revision + 1)))
        }
        return next
    }

    private suspend fun persistEdits(): Boolean {
        val target = store ?: return true
        val edits = dirty.associateWith { drafts[it] ?: DraftSnapshot() }
        val clears = pendingClears.toMap()
        if (edits.isEmpty() && clears.isEmpty()) return persistenceError == null
        val expectedScopes = scopes
        inFlightEdits = edits
        val targets = clears.mapValues { (id, _) -> clearTargets.getOrPut(id) { DraftSnapshot(identity = UUID.randomUUID().toString()) } }
        val ancestors = clearAncestors.toMap()
        try {
            val committed = withContext(ioDispatcher) {
                target.update { disk ->
                    val cleared = clearAcceptedSnapshots(disk, clears, targets, ancestors)
                    var nextRevision = maxOf(cleared.revision, edits.values.maxOfOrNull { it.revision } ?: 0)
                    val entries = cleared.entries.toMutableMap()
                    for ((id, snapshot) in edits) {
                        if (!invalidScope(id, cleared.scopes, expectedScopes) && clears[id] != snapshot) {
                            entries[id] = snapshot.copy(revision = ++nextRevision)
                        }
                    }
                    cleared.copy(revision = nextRevision, entries = entries)
                }
            }
            for ((id, snapshot) in edits) {
                if (drafts[id] == snapshot) {
                    dirty.remove(id)
                }
                submissions.values.filter { it.conversationId == id && it.snapshot == snapshot }.forEach {
                    committed.entries[id]?.let { saved -> it.snapshot = saved }
                }
            }
            clears.forEach { (id, snapshot) ->
                if (pendingClears[id] == snapshot) {
                    pendingClears.remove(id)
                    clearAncestors.remove(id); clearTargets.remove(id); discarded.remove(id)
                }
            }
            clearingSubmittedDraft = pendingClears.isNotEmpty()
            mergeCommitted(committed)
            persistenceError = null
            return true
        } catch (_: Exception) {
            persistenceError = if (clears.keys.any { it in discarded }) {
                "草稿清除尚未保存，请勿发送当前文字，并检查存储空间。"
            } else if (clears.isNotEmpty()) {
                "消息已提交，但草稿清除尚未保存；请勿重复发送，并检查存储空间。"
            } else {
                "草稿尚未保存，请保留当前文字并检查存储空间；当前文字不会发送。"
            }
            return false
        } finally { inFlightEdits = emptyMap() }
    }

    /** Conversation changes and Activity stop request a barrier without blocking the UI thread. */
    fun requestFlush() {
        if (store == null || flushQueued) return
        flushQueued = true
        enqueue {
            flushQueued = false
            persistEdits()
        }
    }

    suspend fun flush(): Boolean = transaction.withLock {
        persistEdits() && dirty.isEmpty() && pendingClears.isEmpty()
    }

    private fun mutate(failureMessage: String = "草稿尚未保存，请保留当前文字并检查存储空间；当前文字不会发送。",
        onFailure: () -> Unit = {},
        change: (PersistedDrafts) -> PersistedDrafts) {
        if (store == null) { apply(change(PersistedDrafts(revision, drafts.toMap(), scopes))); return }
        enqueue {
            if (!persistEdits()) { onFailure(); return@enqueue }
            try {
                val committed = withContext(ioDispatcher) { store.update(change) }
                mergeCommitted(committed); persistenceError = null
            } catch (_: Exception) { persistenceError = failureMessage; onFailure() }
        }
    }

    fun read(conversationId: String?): DraftSnapshot = drafts[conversationId] ?: DraftSnapshot()

    fun edit(conversationId: String?, text: String) {
        if (read(conversationId).text == text) return
        revision = maxOf(revision, store?.committed?.revision ?: 0) + 1
        drafts[conversationId] = DraftSnapshot(text, revision, UUID.randomUUID().toString())
        if (store != null) { dirty += conversationId; requestFlush() }
    }

    fun preserveFirstDraft(conversationId: String?) { preserveFirstConversation = conversationId == null }

    fun cancelFirstMigration() {
        preserveFirstConversation = false
        submissions.entries.removeAll { it.value.conversationId == null }
    }

    fun migrateFirstDraft(conversationId: String) {
        mutate { value ->
            val source = value.entries[null]; val destination = value.entries[conversationId]
            val entries = value.entries.toMutableMap()
            if (source != null && (destination == null || source.revision > destination.revision)) entries[conversationId] = source
            entries[null] = DraftSnapshot("", value.revision + 1)
            value.copy(revision = value.revision + 1, entries = entries)
        }
        submissions.values.forEach { if (it.conversationId == null) it.conversationId = conversationId }
        preserveFirstConversation = false
    }

    fun beginSubmission(conversationId: String?): DraftSubmission {
        preserveFirstDraft(conversationId)
        val ticket = DraftSubmission(++serial, read(conversationId))
        submissions[ticket] = SubmissionOwner(conversationId, ticket.snapshot)
        return ticket
    }

    fun isCurrent(ticket: DraftSubmission): Boolean = submissions[ticket]?.let {
        read(it.conversationId) == it.snapshot
    } == true

    fun acknowledge(ticket: DraftSubmission, accepted: Boolean) {
        val owner = submissions.remove(ticket) ?: return
        if (!accepted) { closeWhenDrained(); return }
        if (store == null) {
            if (read(owner.conversationId) == owner.snapshot) drafts[owner.conversationId] = DraftSnapshot("", ++revision)
            closeWhenDrained()
        } else {
            pendingClears[owner.conversationId] = owner.snapshot
            clearTargets[owner.conversationId] = DraftSnapshot(identity = UUID.randomUUID().toString())
            clearingSubmittedDraft = true
            requestFlush()
        }
    }

    fun discard(conversationId: String?) {
        submissions.entries.removeAll { it.value.conversationId == conversationId }
        val snapshot = read(conversationId)
        if (!durable) { drafts[conversationId] = DraftSnapshot("", ++revision); return }
        val disk = store?.committed ?: committed
        clearAncestors[conversationId] = setOfNotNull(stamp(disk.entries[conversationId] ?: DraftSnapshot()),
            inFlightEdits[conversationId]?.let(::stamp))
        discarded += conversationId
        pendingClears[conversationId] = snapshot
        clearTargets[conversationId] = DraftSnapshot(identity = UUID.randomUUID().toString())
        clearingSubmittedDraft = true
        requestFlush()
    }

    private fun stamp(snapshot: DraftSnapshot): String =
        snapshot.identity.takeIf { it.isNotEmpty() }?.let { "id:$it" } ?: "revision:${snapshot.revision}"

    /** Android saved state bridges a pending disk write. Its ancestry is checked in the disk transaction. */
    fun saveRecovery(conversationId: String?): Bundle? {
        recoveries[conversationId]?.let { return Bundle(it) }
        if (!durable || !loaded) return null
        val current = read(conversationId)
        val clear = pendingClears[conversationId]
        val discard = clear?.takeIf { conversationId in discarded }
        if (conversationId !in dirty && clear == null) return null
        val disk = store?.committed ?: committed
        return Bundle().apply {
            putString("text", current.text)
            putString("identity", current.identity)
            putLong("revision", current.revision)
            putStringArrayList("ancestors", arrayListOf(stamp(disk.entries[conversationId] ?: DraftSnapshot())).apply {
                inFlightEdits[conversationId]?.let { add(stamp(it)) }
                addAll(clearAncestors[conversationId].orEmpty())
                clearTargets[conversationId]?.let { add(stamp(it)) }
            })
            putString("generation", conversationId?.let { scopes[it]?.generation })
            putBoolean("scopeKnown", conversationId in scopes)
            putBoolean("pendingEdit", conversationId in dirty)
            clear?.let { putString("clear", stamp(it)) }
            discard?.let { putString("discard", stamp(it)) }
        }
    }

    fun restoreRecovery(conversationId: String?, saved: Bundle) {
        if (!durable) return
        // A retained ViewModel already owns newer UI state; saved state is only for a fresh owner.
        if (conversationId in dirty || pendingClears.containsKey(conversationId)) return
        recoveries[conversationId] = Bundle(saved)
        enqueue {
            if (conversationId in dirty) { recoveries.remove(conversationId); return@enqueue }
            try {
                val restored = withContext(ioDispatcher) {
                    requireNotNull(store).update { disk ->
                        val scope = conversationId?.let { disk.scopes[it] }
                        val scopeChanged = scope?.deleted == true || (saved.getBoolean("scopeKnown") &&
                            saved.getString("generation") != scope?.generation)
                        if (scopeChanged) disk else {
                            val snapshot = disk.entries[conversationId] ?: DraftSnapshot()
                            val signature = stamp(snapshot)
                            val ancestors = saved.getStringArrayList("ancestors").orEmpty()
                            val pending = DraftSnapshot(saved.getString("text").orEmpty(), saved.getLong("revision"),
                                saved.getString("identity").orEmpty())
                            val discard = saved.getString("discard")
                            val clear = saved.getString("clear")
                            val next = when {
                                discard != null && (signature in ancestors || signature == discard) ->
                                    if (saved.getBoolean("pendingEdit") && stamp(pending) != discard) pending else DraftSnapshot()
                                clear != null && signature == clear -> if (saved.getBoolean("pendingEdit") && stamp(pending) != clear) pending else DraftSnapshot()
                                saved.getBoolean("pendingEdit") && discard == null && stamp(pending) != clear && signature in ancestors -> pending
                                else -> null
                            }
                            if (next == null) disk else disk.copy(revision = disk.revision + 1,
                                entries = disk.entries + (conversationId to next.copy(revision = disk.revision + 1)))
                        }
                    }
                }
                recoveries.remove(conversationId)
                mergeCommitted(restored); persistenceError = null
            } catch (_: Exception) {
                persistenceError = "草稿恢复尚未完成，请检查存储空间；当前文字不会发送。"
            }
        }
    }

    fun restoreCurrent(conversationId: String?, text: String) {
        if (!durable && !drafts.containsKey(conversationId)) drafts[conversationId] = DraftSnapshot(text, ++revision)
    }

    override fun onCleared() {
        // Stop watching, but drain accepted edits and any outstanding admission receipts.
        // A late receipt must still clear only its owned revision after the screen closes.
        retired = true
        changeObserver?.cancel()
        requestFlush()
        closeWhenDrained()
        super.onCleared()
    }
}
/** Only the currently visible draft is persisted across process death. */
internal class CurrentDraftCursor(val drafts: ConversationDrafts, var conversationId: String?) {
    companion object {
        fun saver(drafts: ConversationDrafts) = Saver<CurrentDraftCursor, Bundle>(
            save = { cursor -> Bundle().apply {
                putString("conversation", cursor.conversationId)
                if (!drafts.durable) putString("text", drafts.read(cursor.conversationId).text)
                else drafts.saveRecovery(cursor.conversationId)?.let { putBundle("pending", it) }
            } },
            restore = { saved ->
                val conversationId = saved.getString("conversation")
                drafts.restoreCurrent(conversationId, saved.getString("text").orEmpty())
                saved.getBundle("pending")?.let { drafts.restoreRecovery(conversationId, it) }
                CurrentDraftCursor(drafts, conversationId)
            },
        )
    }
}
