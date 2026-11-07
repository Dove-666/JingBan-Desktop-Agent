package com.aura.companion.data

import org.json.JSONArray
import org.json.JSONObject

/** A cache can never transplant memory to another peer or conversation generation. */
internal class ConversationContextCache(private val database: CompanionDatabase) {
    private fun key(authority: String, cid: String) = "context_envelope:$authority:$cid"

    fun read(authority: String, cid: String, generation: String?): ContextEnvelope? {
        if (database.device()?.endpointId != authority || database.isConversationDeleted(cid) ||
            database.getSetting("authorization_blocked") == "1") return null
        val encoded = database.getSetting(key(authority, cid)) ?: return null
        return try {
            val envelope = ContextEnvelope.parse(JSONObject(encoded), cid, generation)
            require(envelope.generation == generation && generation == database.conversationGeneration(cid)) { "上下文代次已变化" }
            envelope
        } catch (_: Exception) { database.setSetting(key(authority, cid), null); null }
    }

    fun replace(authority: String, cid: String, generation: String?, value: JSONObject, secrets: List<String> = emptyList(),
        expectedEpoch: String = database.contextEpoch(cid)): ContextEnvelope {
        require(database.device()?.endpointId == authority && !database.isConversationDeleted(cid) &&
            database.getSetting("authorization_blocked") != "1")
        var encoded = value.toString()
        // JSON-string encoding matters when a synthetic or user-configured
        // secret contains backslashes or quotes.
        for (secret in secrets.filter { it.isNotBlank() }) encoded = encoded.replace(JSONObject.quote(secret).drop(1).dropLast(1), "[已隐藏密钥]")
        val safe = JSONObject(encoded)
        // A fresh revision is a full replacement. Disabled/unavailable memory
        // must never inherit older cached active entries.
        if (safe.optString("memory_status") != "available") safe.put("memories", JSONArray())
        val envelope = ContextEnvelope.parse(safe, cid, generation)
        require(envelope.generation == generation)
        database.cacheContextEnvelope(authority, cid, generation, expectedEpoch, envelope.json.toString())
        return envelope
    }

    fun clear(cid: String? = null) = database.clearContextEnvelopes(cid)
}
