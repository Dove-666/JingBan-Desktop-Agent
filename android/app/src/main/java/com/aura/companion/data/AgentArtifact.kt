package com.aura.companion.data

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Public immutable delivery reference. Never a desktop filesystem path. */
data class AgentArtifact(val reference: String, val name: String, val mime: String,
    val bytes: Long, val sha256: String, val verificationScope: String)
data class DownloadedArtifact(val artifact: AgentArtifact, val file: File)

internal const val MAX_ARTIFACT_BYTES = 16L * 1024 * 1024
internal fun parseAgentArtifact(raw: JSONObject?): AgentArtifact? {
    if (raw == null) return null
    val reference = raw.opt("reference") as? String ?: return null
    val name = raw.opt("name") as? String ?: return null
    val mime = raw.opt("mime") as? String ?: return null
    val bytes = raw.opt("bytes")
    val sha = raw.opt("sha256") as? String ?: return null
    val scope = raw.opt("verification_scope") as? String ?: return null
    if (!reference.matches(Regex("artifact_[0-9a-f]{64}")) || name.length !in 1..240 ||
        name in setOf(".", "..") || name.any { it.code < 32 || it == '/' || it == '\\' } ||
        !mime.matches(Regex("[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")) || mime.length > 120 ||
        (bytes !is Int && bytes !is Long) || (bytes as Number).toLong() !in 0..MAX_ARTIFACT_BYTES ||
        !sha.matches(Regex("[0-9a-f]{64}")) || scope.length > 160 || scope.any { it.code < 32 }) return null
    return AgentArtifact(reference, name, mime, bytes.toLong(), sha, scope)
}

internal fun parseAgentArtifacts(raw: JSONArray?): List<AgentArtifact> =
    (0 until minOf(raw?.length() ?: 0, 100)).mapNotNull { parseAgentArtifact(raw?.optJSONObject(it)) }
        .distinctBy { it.reference }
