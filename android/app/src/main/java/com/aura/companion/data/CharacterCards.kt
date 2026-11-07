package com.aura.companion.data

import org.json.JSONArray
import org.json.JSONObject

/** Public shelf metadata only. Card prompts are never part of application/UI state. */
data class CharacterCardSummary(val id: String, val label: String, val name: String, val digest: String)

private val CHARACTER_ID = Regex("[0-9a-f]{32}")
private val CHARACTER_DIGEST = Regex("[0-9a-f]{64}")
internal const val MAX_CHARACTER_ENTRIES = 10_000
internal const val MAX_CHARACTER_CACHE_BYTES = 16 * 1024 * 1024
private const val INVALID_CHARACTER = "角色卡同步数据无效，请刷新后重试"

internal fun validCharacterId(value: String): Boolean = value == "default" || CHARACTER_ID.matches(value)

private fun text(value: JSONObject, field: String, limit: Int, secrets: List<String>, identity: Boolean = false): String {
    val raw = value.opt(field)
    require(raw is String && raw.isNotBlank() && raw.length <= limit &&
        raw.none { it.code < 32 || it.code == 127 }) { INVALID_CHARACTER }
    if (identity) require(secrets.none { it.isNotBlank() && raw.contains(it) }) { INVALID_CHARACTER }
    return if (identity) raw else redactKnownText(raw, secrets)
}

internal fun characterDigest(value: JSONObject, field: String, secrets: List<String> = emptyList()): String =
    text(value, field, 64, secrets, identity = true).also { require(CHARACTER_DIGEST.matches(it)) { INVALID_CHARACTER } }

private fun strictInt(value: JSONObject, field: String, maximum: Int): Int {
    val raw = value.opt(field)
    require(raw is Int || raw is Long) { INVALID_CHARACTER }
    return (raw as Number).toLong().also { require(it in 0..maximum.toLong()) { INVALID_CHARACTER } }.toInt()
}

internal fun parseCharacterSummary(value: JSONObject, secrets: List<String> = emptyList()): CharacterCardSummary {
    val id = text(value, "id", 32, secrets, identity = true)
    require(CHARACTER_ID.matches(id)) { INVALID_CHARACTER }
    return CharacterCardSummary(id, text(value, "label", 256, secrets), text(value, "name", 256, secrets),
        characterDigest(value, "digest", secrets))
}

internal class CharacterSelection(val id: String?, val name: String, val digest: String, val persona: SyncedPersona) {
    override fun toString() = "CharacterSelection([private])"
}

internal fun parseCharacterSelection(value: JSONObject, authority: String, secrets: List<String>): CharacterSelection {
    require(value.has("id")) { INVALID_CHARACTER }
    val id = if (value.isNull("id")) null else text(value, "id", 32, secrets, identity = true)
    require(id == null || validCharacterId(id)) { INVALID_CHARACTER }
    val name = text(value, "name", 256, secrets)
    val digest = characterDigest(value, "digest", secrets)
    val persona = SyncedPersona.fromRpc(value.getJSONObject("persona"), authority, secrets)
    require(persona.name == name) { INVALID_CHARACTER }
    require(CHARACTER_DIGEST.matches(persona.revision)) { INVALID_CHARACTER }
    return CharacterSelection(id, name, digest, persona)
}

internal class CharacterPage(val revision: String, val total: Int, val items: List<CharacterCardSummary>,
    val nextOffset: Int?, val active: CharacterSelection) {
    override fun toString() = "CharacterPage([private])"
}

internal fun parseCharacterPage(value: JSONObject, authority: String, offset: Int, secrets: List<String>): CharacterPage {
    require(value.opt("version") == 1 && offset in 0..MAX_CHARACTER_ENTRIES) { INVALID_CHARACTER }
    val revision = characterDigest(value, "revision", secrets)
    val total = strictInt(value, "total", MAX_CHARACTER_ENTRIES)
    val values = value.getJSONArray("items")
    require(value.has("next_offset")) { INVALID_CHARACTER }
    require(values.length() <= 100 && offset <= total && offset + values.length() <= total) { INVALID_CHARACTER }
    val entries = (0 until values.length()).map { parseCharacterSummary(values.getJSONObject(it), secrets) }
    require(entries.map { it.id }.distinct().size == entries.size) { INVALID_CHARACTER }
    val next = if (value.isNull("next_offset")) null else strictInt(value, "next_offset", MAX_CHARACTER_ENTRIES)
    if (next != null) require(entries.isNotEmpty() && next == offset + entries.size && next < total) { INVALID_CHARACTER }
    else require(offset + entries.size == total) { INVALID_CHARACTER }
    return CharacterPage(revision, total, entries, next,
        parseCharacterSelection(value.getJSONObject("active"), authority, secrets))
}

internal class CharacterCardSnapshot(val summary: CharacterCardSummary, val persona: SyncedPersona) {
    override fun toString() = "CharacterCardSnapshot([private])"
}

internal fun parseCharacterCard(value: JSONObject, authority: String, requestedId: String,
                               expectedDigest: String?, secrets: List<String>): CharacterCardSnapshot {
    require(value.opt("version") == 1 && validCharacterId(requestedId)) { INVALID_CHARACTER }
    val id = text(value, "id", 32, secrets, identity = true)
    require(id == requestedId) { INVALID_CHARACTER }
    val digest = characterDigest(value, "digest", secrets)
    require(expectedDigest == null || digest == expectedDigest) { "角色卡已变化，请刷新后重新选择" }
    val summary = CharacterCardSummary(id, text(value, "label", 256, secrets), text(value, "name", 256, secrets), digest)
    val persona = SyncedPersona.fromRpc(value.getJSONObject("persona"), authority, secrets)
    require(persona.name == summary.name && CHARACTER_DIGEST.matches(persona.revision)) { INVALID_CHARACTER }
    return CharacterCardSnapshot(summary, persona)
}

/** Stored only as authenticated ciphertext, independently from the API credential. */
internal class CharacterCatalogCache(val authorityId: String, val revision: String,
    val items: List<CharacterCardSummary>, val active: CharacterSelection) {
    override fun toString() = "CharacterCatalogCache([private])"
    fun encoded(): ByteArray = JSONObject().put("version", 1).put("authority_id", authorityId)
        .put("revision", revision).put("items", JSONArray(items.map {
            JSONObject().put("id", it.id).put("label", it.label).put("name", it.name).put("digest", it.digest)
        })).put("active", JSONObject().put("id", active.id ?: JSONObject.NULL).put("name", active.name)
            .put("digest", active.digest).put("persona", JSONObject().put("version", 1)
                .put("revision", active.persona.revision).put("name", active.persona.name)
                .put("system_prompt", active.persona.systemPrompt)))
        .toString().toByteArray(Charsets.UTF_8).also { require(it.size <= MAX_CHARACTER_CACHE_BYTES) { INVALID_CHARACTER } }

    companion object {
        fun decode(bytes: ByteArray): CharacterCatalogCache {
            require(bytes.size <= MAX_CHARACTER_CACHE_BYTES) { INVALID_CHARACTER }
            val value = JSONObject(bytes.toString(Charsets.UTF_8))
            require(value.opt("version") == 1) { INVALID_CHARACTER }
            val authority = text(value, "authority_id", 2048, emptyList(), identity = true)
            val revision = characterDigest(value, "revision")
            val array = value.getJSONArray("items")
            require(array.length() <= MAX_CHARACTER_ENTRIES) { INVALID_CHARACTER }
            val entries = (0 until array.length()).map { parseCharacterSummary(array.getJSONObject(it)) }
            require(entries.map { it.id }.distinct().size == entries.size) { INVALID_CHARACTER }
            val active = parseCharacterSelection(value.getJSONObject("active"), authority, emptyList())
            require(active.id == null || active.id == "default" || entries.any {
                it.id == active.id && it.digest == active.digest
            }) { INVALID_CHARACTER }
            return CharacterCatalogCache(authority, revision, entries, active)
        }
    }
}
