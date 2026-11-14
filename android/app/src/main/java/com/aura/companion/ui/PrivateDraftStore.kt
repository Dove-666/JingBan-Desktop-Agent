package com.aura.companion.ui

import android.util.AtomicFile
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.util.concurrent.ConcurrentHashMap

internal data class DraftScope(val generation: String?, val deleted: Boolean = false)
internal data class PersistedDrafts(val revision: Long = 0,
    val entries: Map<String?, DraftSnapshot> = emptyMap(), val scopes: Map<String, DraftScope> = emptyMap())

internal interface DraftStore {
    val changes: StateFlow<Long>
    val committed: PersistedDrafts? get() = null
    fun load(): PersistedDrafts
    fun update(change: (PersistedDrafts) -> PersistedDrafts): PersistedDrafts
    fun refreshScopes() = Unit
}

/** Private app storage only. No credentials, API settings, network or draft logging. */
internal class PrivateDraftStore(private val file: File,
    private val scopeLookup: (Set<String>) -> Map<String, DraftScope> = { emptyMap() }) : DraftStore {
    private val storeId = file.canonicalPath
    private val changed = notifications.getOrPut(storeId) { MutableStateFlow(0L) }
    override val changes: StateFlow<Long> = changed
    override val committed get() = commits[storeId]

    // Activity/ViewModel overlap can leave an old callback alive. Always read
    // the committed file inside the same process-wide transaction lock.
    override fun load(): PersistedDrafts = update { it }
    override fun update(change: (PersistedDrafts) -> PersistedDrafts): PersistedDrafts = synchronized(lock) {
        val original = read()
        val next = reconcile(change(reconcile(original)))
        if (next != original) write(next)
        else commits[storeId] = next
        next
    }
    override fun refreshScopes() { update { it } }

    private fun reconcile(value: PersistedDrafts): PersistedDrafts {
        val ids = value.entries.keys.filterNotNull().toSet() + value.scopes.keys
        if (ids.isEmpty()) return value
        val scopes = scopeLookup(ids)
        var revision = value.revision
        val entries = value.entries.toMutableMap()
        val generations = value.scopes.toMutableMap()
        for ((id, current) in scopes) {
            val old = generations[id]
            if (current != old) {
                if (current.deleted || (old?.generation != null && old.generation != current.generation)) {
                    entries[id] = DraftSnapshot("", ++revision)
                }
                generations[id] = current
            } else if (current.deleted && entries[id]?.text?.isNotEmpty() == true) {
                entries[id] = DraftSnapshot("", ++revision)
            }
        }
        return PersistedDrafts(revision, entries, generations)
    }

    private fun read(): PersistedDrafts {
        val atomic = AtomicFile(file)
        if (!file.exists() && !File(file.path + ".bak").exists()) return PersistedDrafts()
        val encoded = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(atomic.readFully())).toString()
        val value = JSONObject(encoded)
        require(value.getInt("version") == 1)
        val revision = value.getLong("revision"); require(revision >= 0)
        val entries = linkedMapOf<String?, DraftSnapshot>()
        val saved = value.getJSONArray("drafts")
        for (index in 0 until saved.length()) {
            val row = saved.getJSONObject(index)
            val cid = if (row.isNull("conversation")) null else row.getString("conversation")
            require(cid == null || cid.isNotBlank() && cid.length <= 200)
            val version = row.getLong("revision"); require(version in 0..revision && !entries.containsKey(cid))
            entries[cid] = DraftSnapshot(row.getString("text"), version, row.optString("identity", ""))
        }
        val scopes = linkedMapOf<String, DraftScope>()
        val savedScopes = value.getJSONArray("scopes")
        for (index in 0 until savedScopes.length()) {
            val row = savedScopes.getJSONObject(index); val cid = row.getString("conversation")
            require(cid.isNotBlank() && cid.length <= 200 && !scopes.containsKey(cid))
            scopes[cid] = DraftScope(if (row.isNull("generation")) null else row.getString("generation"), row.getBoolean("deleted"))
        }
        return PersistedDrafts(revision, entries, scopes)
    }

    private fun write(value: PersistedDrafts) {
        require(value.revision >= 0 && value.entries.values.all { it.revision in 0..value.revision })
        require(file.parentFile!!.isDirectory || file.parentFile!!.mkdirs())
        val entries = JSONArray()
        value.entries.forEach { (cid, draft) -> entries.put(JSONObject()
            .put("conversation", cid ?: JSONObject.NULL).put("text", draft.text).put("revision", draft.revision)
            .put("identity", draft.identity)) }
        val scopes = JSONArray()
        value.scopes.forEach { (cid, scope) -> scopes.put(JSONObject().put("conversation", cid)
            .put("generation", scope.generation ?: JSONObject.NULL).put("deleted", scope.deleted)) }
        val bytes = JSONObject().put("version", 1).put("revision", value.revision)
            .put("drafts", entries).put("scopes", scopes).toString().toByteArray(Charsets.UTF_8)
        val atomic = AtomicFile(file)
        val stream = atomic.startWrite()
        try {
            stream.write(bytes)
            atomic.finishWrite(stream)
        } catch (failure: Exception) {
            atomic.failWrite(stream)
            throw failure
        }
        // AtomicFile reports some rename/fsync failures only via platform logs.
        // Never notify the UI that a draft was saved unless its bytes committed.
        if (!atomic.readFully().contentEquals(bytes)) throw IOException("Private draft commit was not verified")
        commits[storeId] = value
        changed.value++
    }

    companion object {
        private val lock = Any()
        private val notifications = ConcurrentHashMap<String, MutableStateFlow<Long>>()
        private val commits = ConcurrentHashMap<String, PersistedDrafts>()
    }
}
