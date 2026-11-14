package com.aura.companion.data

import org.json.JSONObject

/** Ephemeral desktop-owned request. Never persisted or placed in the outbox. */
data class AgentFileResource(val path: String, val access: String, val directory: Boolean)
data class AgentInteraction(val id: String, val kind: String, val question: String,
    val conversationId: String, val options: List<String> = emptyList(),
    val resource: AgentFileResource? = null, val authorityId: String)

internal fun parseAgentInteraction(value: JSONObject?, authorityId: String?): AgentInteraction? {
    if (value == null || authorityId == null) return null
    val id = value.opt("id") as? String ?: return null
    val kind = value.opt("kind") as? String ?: return null
    val question = value.opt("question") as? String ?: return null
    val conversation = value.opt("conversation_id") as? String ?: return null
    if (!id.matches(Regex("[A-Za-z0-9_-]{1,100}")) || kind !in setOf("question", "file_access") ||
        question.codePointCount(0, question.length) !in 1..16000 || !AppRepository.canonicalUuid(conversation)) return null
    val options = mutableListOf<String>()
    var resource: AgentFileResource? = null
    if (kind == "file_access") {
        val raw = value.optJSONObject("resource") ?: return null
        val path = raw.opt("path") as? String ?: return null
        val access = raw.opt("access") as? String ?: return null
        val directory = raw.opt("directory") as? Boolean ?: return null
        if (path.length !in 1..4096 || path.any { it.code < 32 } || access !in setOf("read", "write")) return null
        resource = AgentFileResource(path, access, directory)
    } else if (value.has("options")) {
        val array = value.optJSONArray("options") ?: return null
        if (array.length() > 6) return null
        for (i in 0 until array.length()) {
            val item = array.opt(i) as? String ?: return null
            if (item.codePointCount(0, item.length) !in 1..240) return null
            options += item
        }
    }
    return AgentInteraction(id, kind, question, conversation, options, resource, authorityId)
}

internal fun validatedAgentAnswer(interaction: AgentInteraction, answer: JSONObject): JSONObject? {
    val keys = answer.keys().asSequence().toSet()
    if (interaction.kind == "file_access") {
        if (keys != setOf("allowed")) return null
        val allowed = answer.opt("allowed") as? Boolean ?: return null
        return JSONObject().put("allowed", allowed)
    }
    if (keys != setOf("text")) return null
    val text = (answer.opt("text") as? String)?.trim() ?: return null
    if (text.codePointCount(0, text.length) !in 1..8000 || text.any { it.code < 32 && it != '\n' && it != '\t' }) return null
    return JSONObject().put("text", text)
}
