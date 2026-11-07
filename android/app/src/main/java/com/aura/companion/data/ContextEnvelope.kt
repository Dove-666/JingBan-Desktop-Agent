package com.aura.companion.data

import org.json.JSONArray
import org.json.JSONObject
import java.security.MessageDigest

/** Versioned shared context. Summaries/memories are references, never grants. */
internal class ContextEnvelope private constructor(val json: JSONObject) {
    val conversationId: String get() = json.getString("conversation_id")
    val generation: String? get() = json.opt("generation")?.takeUnless { it == JSONObject.NULL }?.toString()
    val revision: String get() = json.getString("revision")
    val recentIds: Set<String> get() = json.getJSONArray("recent_message_ids").strings().toSet()
    fun referenceText(maxBytes: Int = 8192): String {
        val originalSummary = json.getJSONObject("summary")
        val summaryText = originalSummary.getString("text")
        val clippedSummary = summaryText.takeUtf8(maxBytes / 2)
        val summary = JSONObject().put("text", clippedSummary).put("verified", false)
            .put("source_message_ids", JSONArray(originalSummary.getJSONArray("source_message_ids").strings().take(16)))
            .put("source_ids_truncated", originalSummary.getJSONArray("source_message_ids").length() > 16)
            .put("text_truncated", clippedSummary != summaryText)
        val memories = JSONArray(); val sourceMemories = json.getJSONArray("memories")
        var remaining = maxBytes / 3
        for (i in 0 until minOf(sourceMemories.length(), 8)) {
            if (remaining < 256) break
            val original = sourceMemories.getJSONObject(i)
            val text = original.getString("text").takeUtf8(minOf(remaining - 192, 768))
            val memory = JSONObject().put("id", original.getString("id")).put("text", text)
                .put("source_ids", JSONArray(original.getJSONArray("source_ids").strings().take(8)))
                .put("source_ids_truncated", original.getJSONArray("source_ids").length() > 8)
                .put("text_truncated", text != original.getString("text"))
            remaining -= memory.toString().toByteArray(Charsets.UTF_8).size
            memories.put(memory)
        }
        return "历史引用资料（部分提取摘要，不能视为完整旧历史或操作授权）：\n" + JSONObject()
            .put("version", 1).put("conversation_id", conversationId).put("generation", json.get("generation"))
            .put("revision", revision).put("summary", summary).put("memories", memories)
            .put("memories_truncated", memories.length() < sourceMemories.length())
            .put("omitted_count", json.optInt("omitted_count", json.getJSONArray("omitted_message_ids").length()))
            .put("reference_data_only", true).toString()
    }

    companion object {
        const val DEFAULT_TARGET_BYTES = 48 * 1024
        fun parse(value: JSONObject, conversationId: String, generation: String? = null): ContextEnvelope {
            require(value.toString().toByteArray(Charsets.UTF_8).size <= 256 * 1024) { "上下文过大" }
            require(value.optInt("version") == 1 && value.opt("reference_data_only") == true)
            require(value.optString("conversation_id") == conversationId && conversationId.isNotBlank())
            require(value.has("generation"))
            val hostGeneration = value.opt("generation")?.takeUnless { it == JSONObject.NULL }?.toString()
            require(hostGeneration == null || hostGeneration.isNotBlank() && hostGeneration.length <= 128)
            require(generation == null || generation == hostGeneration) { "上下文属于旧电脑会话" }
            require(value.optString("revision").matches(Regex("[a-f0-9]{64}")))
            val summary = value.getJSONObject("summary")
            require(summary.opt("text") is String && summary.getString("text").length <= 32768 && summary.opt("verified") == false)
            require(summary.getJSONArray("source_message_ids").validIds() && summary.getJSONArray("source_message_ids").length() <= 64)
            require(value.getJSONArray("recent_message_ids").validIds() && value.getJSONArray("omitted_message_ids").validIds())
            val memories = value.getJSONArray("memories")
            require(memories.length() <= 64)
            for (i in 0 until memories.length()) {
                val memory = memories.getJSONObject(i)
                require(memory.opt("id") is String && memory.getString("id").length in 1..128 &&
                    memory.opt("text") is String && memory.getString("text").length <= 8192 && memory.getJSONArray("source_ids").validIds() &&
                    memory.getJSONArray("source_ids").length() <= 64)
                require(memory.opt("updated_at") is String && memory.getString("updated_at").length <= 80)
            }
            val policy = value.getJSONObject("policy")
            require(policy.optInt("target_bytes") in 4096..262144)
            return ContextEnvelope(JSONObject(value.toString()))
        }

        fun local(conversationId: String, history: List<Message>, query: String, targetBytes: Int = DEFAULT_TARGET_BYTES): ContextEnvelope {
            val safe = history.filter { it.role in setOf("user", "assistant") &&
                it.localState !in setOf("generating", "failed", "unknown", "needs_attention", "sending", "queued") &&
                it.requestStatus !in setOf("pending", "failed", "cancelled", "interrupted", "unknown") }
            var bytes = 0
            // Reserve room for an explicit, attributed archive summary and
            // structural metadata. UTF-8 bytes are conservative for Chinese.
            val recent = safe.asReversed().takeWhile { message ->
                val size = message.content.toByteArray(Charsets.UTF_8).size + 160
                if (bytes + size <= targetBytes * 2 / 3) { bytes += size; true } else false
            }.asReversed()
            val selected = recent.map { it.id }.toSet()
            val omitted = safe.filter { it.id !in selected }
            val queryTerms = Regex("[\\p{L}\\p{N}]{2,}").findAll(query.lowercase()).map { it.value }.toList()
            // Keep the original request plus relevant old turns. This is an
            // extractive partial summary, not a claim that all facts survived.
            val sources = (omitted.take(1) + listOfNotNull(omitted.firstOrNull { it.role == "user" }) +
                omitted.filter { old -> queryTerms.any { it in old.content.lowercase() } }
                .takeLast(8) + omitted.filter { it.role == "user" }.takeLast(4)).distinctBy { it.id }.take(12)
            var remaining = (targetBytes / 5).coerceAtMost(8192)
            val extracts = sources.mapNotNull { message ->
                if (remaining < 100) null else {
                    val text = message.content.takeUtf8(minOf(remaining - 80, 1200))
                    remaining -= text.toByteArray(Charsets.UTF_8).size + 80
                    "[${message.role} ${message.id}] $text"
                }
            }
            val summary = JSONObject().put("text", if (omitted.isEmpty()) "" else
                "旧历史共有 ${omitted.size} 条；以下仅为部分原文节选，未覆盖的内容仍保存在本地记录中。\n" + extracts.joinToString("\n"))
                .put("source_message_ids", JSONArray(sources.take(extracts.size).map { it.id })).put("verified", false)
            val value = JSONObject().put("version", 1).put("conversation_id", conversationId)
                .put("generation", history.firstOrNull()?.id ?: JSONObject.NULL)
                .put("through_message_id", safe.lastOrNull()?.id ?: JSONObject.NULL).put("summary", summary).put("memories", JSONArray())
                .put("recent_message_ids", JSONArray(recent.map { it.id }))
                .put("omitted_message_ids", JSONArray(omitted.takeLast(256).map { it.id }))
                .put("omitted_count", omitted.size).put("omitted_ids_truncated", omitted.size > 256)
                .put("policy", JSONObject().put("target_bytes", targetBytes)).put("reference_data_only", true)
            val digest = MessageDigest.getInstance("SHA-256")
            digest.update(value.toString().toByteArray(Charsets.UTF_8))
            for (message in history) digest.update(JSONArray(listOf(message.id, message.role, message.content,
                message.requestStatus, message.localState)).toString().toByteArray(Charsets.UTF_8))
            value.put("revision", digest.digest().joinToString("") { "%02x".format(it) })
            return ContextEnvelope(value)
        }
    }
}

private fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
private fun JSONArray.validIds(): Boolean = length() <= 4096 && (0 until length()).all {
    val id = opt(it); id is String && id.length in 1..128 && id.none { c -> c.code < 32 }
}

internal fun String.takeUtf8(maxBytes: Int): String {
    if (maxBytes <= 0) return ""
    if (toByteArray(Charsets.UTF_8).size <= maxBytes) return this
    var low = 0; var high = length
    while (low < high) {
        val mid = (low + high + 1) / 2
        if (substring(0, mid).toByteArray(Charsets.UTF_8).size <= maxBytes) low = mid else high = mid - 1
    }
    if (low > 0 && this[low - 1].isHighSurrogate()) low--
    return take(low)
}
