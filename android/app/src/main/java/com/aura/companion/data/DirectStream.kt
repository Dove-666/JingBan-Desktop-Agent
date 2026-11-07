package com.aura.companion.data

import org.json.JSONObject
import java.io.BufferedReader

/** Public response text only. Provider reasoning and transport metadata never enter app state. */
internal data class DirectChatUpdate(val text: String)

internal fun redactKnownText(value: String, secrets: Collection<String>): String =
    secrets.filter { it.isNotBlank() }.distinct().sortedByDescending { it.length }
        .fold(value) { text, secret -> text.replace(secret, "[已隐藏密钥]") }

internal fun redactStreamingText(value: String, secrets: Collection<String>, final: Boolean = false): String {
    var safe = redactKnownText(value, secrets)
    if (!final) {
        var suffix = 0
        for (secret in secrets.filter { it.isNotBlank() }) {
            var match = minOf(secret.length - 1, safe.length)
            while (match > suffix && !safe.endsWith(secret.substring(0, match))) match--
            suffix = maxOf(suffix, match)
        }
        if (suffix > 0) safe = safe.dropLast(suffix)
    }
    if (safe.lastOrNull()?.isHighSurrogate() == true) safe = safe.dropLast(1)
    return safe
}

/**
 * Reads the SSE protocol, rather than treating socket reads as JSON boundaries.
 * A dropped socket is never a successful completion, even after visible text.
 */
internal fun readDirectEvents(reader: BufferedReader, apiKey: String, onUpdate: (DirectChatUpdate) -> Unit,
    beforeRead: () -> Unit = {}): String = readDirectEvents(reader, listOf(apiKey), onUpdate, beforeRead)

internal fun readDirectEvents(reader: BufferedReader, secrets: List<String>, onUpdate: (DirectChatUpdate) -> Unit,
    beforeRead: () -> Unit = {}): String {
    val text = DirectVisibleText(secrets, onUpdate)
    val event = StringBuilder()
    var consumed = 0
    var done = false
    var finish: String? = null
    fun dispatch() {
        if (event.isEmpty()) return
        val data = event.toString().trimEnd('\n'); event.setLength(0)
        if (data.trim() == "[DONE]") { done = true; return }
        val chunk = try { JSONObject(data) } catch (_: Exception) {
            throw DirectApiFailure("模型流式回复格式不完整；已保留收到的文字，未自动重试")
        }
        if (chunk.has("error")) throw DirectApiFailure("模型 API 中断了回复；已保留收到的文字，未自动重试")
        val choices = chunk.optJSONArray("choices") ?: return
        for (index in 0 until choices.length()) {
            val choice = choices.optJSONObject(index) ?: continue
            if (choice.optInt("index", 0) != 0) continue
            val delta = choice.optJSONObject("delta")
            if (delta?.opt("content") is String) text.append(delta.getString("content"))
            // reasoning_content, reasoning, tool_calls and other fields are not display text.
            if (!choice.isNull("finish_reason")) finish = choice.optString("finish_reason")
        }
    }
    while (!done) {
        beforeRead()
        val line = reader.readBoundedEventLine() ?: break
        consumed += line.length + 1
        if (consumed > 512 * 1024 || line.length > 128 * 1024) throw DirectApiFailure("模型回复过大；已保留收到的文字，未自动重试")
        when {
            line.isEmpty() -> dispatch()
            line.startsWith("data:") -> event.append(line.substring(5).removePrefix(" ")).append('\n')
            // SSE comments, event names and ids are intentionally ignored.
        }
    }
    if (!done && event.isNotEmpty()) dispatch()
    if (!done || finish != "stop") throw DirectApiFailure(when (finish) {
        "length" -> "模型回复达到服务商长度限制；已保留收到的文字，未自动继续或重试"
        "content_filter" -> "模型 API 停止了回复；已保留收到的文字，未自动重试"
        else -> "模型连接在回复完成前中断；已保留收到的文字，未自动重试"
    })
    return text.finish()
}

/** readLine() can allocate without a bound before its caller can check size. */
private fun BufferedReader.readBoundedEventLine(): String? {
    val line = StringBuilder()
    while (true) {
        val value = read()
        if (value < 0) return line.toString().takeIf { line.isNotEmpty() }
        val char = value.toChar()
        if (char == '\n') return line.toString()
        if (char == '\r') {
            mark(1); if (read() != '\n'.code) reset()
            return line.toString()
        }
        if (line.length >= 128 * 1024) throw DirectApiFailure("模型流式数据过大；已保留收到的文字，未自动重试")
        line.append(char)
    }
}

/**
 * Some desktop personas request {reply, emotion} JSON. Decode the reply string
 * incrementally without exposing that envelope (or partially escaped text).
 */
internal class DirectVisibleText(secrets: List<String>, private val onUpdate: (DirectChatUpdate) -> Unit) {
    constructor(apiKey: String, onUpdate: (DirectChatUpdate) -> Unit) : this(listOf(apiKey), onUpdate)
    private val secrets = secrets.filter { it.isNotBlank() }.distinct()
    private val raw = StringBuilder()
    private var previous = ""

    fun append(delta: String) {
        if (raw.length + delta.length > 128 * 1024) throw DirectApiFailure("模型回复过大，未自动重试")
        raw.append(delta)
        publish(visible(final = false), final = false)
    }

    fun finish(): String {
        val result = visible(final = true)
        if (result.isBlank() || result.length > 24000) throw DirectApiFailure("模型回复为空或超过 24000 字，未自动重试")
        return publish(result, final = true)
    }

    private fun publish(value: String, final: Boolean): String {
        // Withhold every known credential prefix across transport chunks.
        val safe = redactStreamingText(value, secrets, final)
        if (safe.length > 24000) throw DirectApiFailure("模型回复超过 24000 字；已保留收到的文字，未自动重试")
        if (safe != previous) { previous = safe; onUpdate(DirectChatUpdate(safe)) }
        return safe
    }

    private fun visible(final: Boolean): String {
        var candidate = raw.toString().trimStart()
        if (candidate.startsWith("`")) {
            if (!candidate.contains('\n') && !final) return ""
            if (candidate.startsWith("```json", ignoreCase = true)) candidate = candidate.substring(7).trimStart()
            else if (candidate.startsWith("```\n") || candidate.startsWith("```\r\n")) candidate = candidate.substring(3).trimStart()
            else return candidate
        }
        if (candidate.isEmpty()) return ""
        if (!candidate.startsWith("{")) return if (final) candidate.trim() else candidate
        if (final) {
            val json = candidate.removeSuffix("```").trimEnd()
            val objectValue = runCatching { JSONObject(json) }.getOrNull()
                ?: throw DirectApiFailure("模型回复格式不完整；已保留收到的文字，未自动重试")
            if (objectValue.has("reply")) {
                val reply = objectValue.opt("reply")
                if (reply !is String) throw DirectApiFailure("模型回复格式不完整；已保留收到的文字，未自动重试")
                return reply.trim()
            }
            if (objectValue.has("emotion") || objectValue.has("reasoning_content"))
                throw DirectApiFailure("模型回复缺少可显示内容，未自动重试")
            // A user's requested JSON document is ordinary answer content.
            // Hold it until structurally complete, then retain its code fence.
            return raw.toString().trim()
        }
        // Scan top-level JSON keys. Nested objects may contain another reply
        // key, which must never be mistaken for the public response.
        var pos = 1
        while (pos < candidate.length) {
            while (pos < candidate.length && (candidate[pos].isWhitespace() || candidate[pos] == ',')) pos++
            if (pos >= candidate.length || candidate[pos] != '"') return ""
            val key = decodeString(candidate, pos) ?: return ""
            if (!key.closed) return ""
            pos = key.end
            while (pos < candidate.length && candidate[pos].isWhitespace()) pos++
            if (pos >= candidate.length || candidate[pos++] != ':') return ""
            while (pos < candidate.length && candidate[pos].isWhitespace()) pos++
            if (pos >= candidate.length) return ""
            if (key.text == "reply") return if (candidate[pos] == '"') decodeString(candidate, pos)?.text.orEmpty() else ""
            // Skip a complete arbitrary value, preserving string escapes and
            // object/array depth until the next top-level comma.
            var depth = 0
            while (pos < candidate.length) {
                when (candidate[pos]) {
                    '"' -> { val value = decodeString(candidate, pos) ?: return ""; if (!value.closed) return ""; pos = value.end; continue }
                    '{', '[' -> depth++
                    '}', ']' -> if (depth > 0) depth-- else return ""
                    ',' -> if (depth == 0) { pos++; break }
                }
                pos++
            }
        }
        return ""
    }

    private data class Decoded(val text: String, val end: Int, val closed: Boolean)
    private fun decodeString(value: String, start: Int): Decoded? {
        val out = StringBuilder(); var at = start + 1
        while (at < value.length) {
            val c = value[at++]
            if (c == '"') return Decoded(out.toString(), at, true)
            if (c != '\\') { if (c < ' ') return null; out.append(c); continue }
            if (at >= value.length) break
            when (val escaped = value[at++]) {
                '"', '\\', '/' -> out.append(escaped)
                'b' -> out.append('\b')
                'f' -> out.append('\u000c')
                'n' -> out.append('\n')
                'r' -> out.append('\r')
                't' -> out.append('\t')
                'u' -> {
                    if (at + 4 > value.length) return Decoded(out.toString(), value.length, false)
                    val code = value.substring(at, at + 4).toIntOrNull(16) ?: return null
                    out.append(code.toChar()); at += 4
                }
                else -> return null
            }
        }
        return Decoded(out.toString(), at, false)
    }
}
