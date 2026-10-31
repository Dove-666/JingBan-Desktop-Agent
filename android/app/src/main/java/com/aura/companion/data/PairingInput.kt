package com.aura.companion.data

import android.util.Base64
import android.util.JsonReader
import android.util.JsonToken
import org.json.JSONObject
import java.io.StringReader
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction

/** Local validation errors must never close or replace an existing RPC session. */
internal class PairingInputException(message: String) : IllegalArgumentException(message)

/** Only static actionable errors leave this parser; no input or parser cause does. */
internal object PairingInput {
    const val EMPTY = "请在电脑「远程连接」点击「复制配对信息」，将整段内容粘贴到这里。"
    const val SHORT_CODE = "这只是短配对码，缺少电脑连接地址。请在电脑「远程连接」点击「复制配对信息」，将复制的整段内容粘贴到这里。"
    const val TOO_LONG = "配对信息过长。请只粘贴电脑端「复制配对信息」复制的内容。"
    const val FORMAT = "配对信息格式不完整。请在电脑「远程连接」重新点击「复制配对信息」，完整复制后粘贴。"
    const val LINK = "配对链接已损坏或不完整。请在电脑重新点击「复制配对信息」，粘贴完整内容。"
    const val FIELDS = "配对信息字段格式无效。请在电脑重新生成并复制完整配对信息。"
    const val MISSING_TICKET = "配对信息缺少电脑连接地址。请点击电脑端「复制配对信息」，粘贴整段内容，不能只填短配对码。"
    const val MISSING_CODE = "这份信息缺少临时配对码。请在电脑生成新的配对信息，再点击「复制配对信息」后整段粘贴。"
    const val VERSION = "配对信息的版本不兼容。请更新手机 App 和电脑端，再重新生成配对信息。"
    const val EXPIRED = "配对信息已过期。请在电脑「远程连接」重新生成配对信息，并在 5 分钟内完成配对。"
    const val CONFLICT = "配对信息内容不一致。请在电脑重新生成，并完整复制同一份配对信息。"

    private const val PREFIX = "aura://pair/"
    private const val MAX_LENGTH = 32768
    private val fields = listOf("ticket", "code", "endpoint_id", "version", "expires_at")
    private val stringFields = setOf("ticket", "code", "endpoint_id", "uri")

    fun parse(value: String, nowMillis: Long = System.currentTimeMillis()): JSONObject {
        if (value.length > MAX_LENGTH) fail(TOO_LONG)
        val text = value.trim().removePrefix("\uFEFF").trim()
        if (text.isEmpty()) fail(EMPTY)
        val outer = when {
            text.startsWith("aura://") -> readUri(text)
            text.startsWith("{") -> readJson(text, FORMAT)
            text.matches(Regex("[A-Za-z0-9_-]{1,128}")) -> fail(SHORT_CODE)
            else -> fail(FORMAT)
        }
        val info = outer.optJSONObject("pairing") ?: outer
        if (info !== outer && info.has("pairing")) fail(FIELDS)
        val fromUri = if (info.has("uri")) readUri(info.getString("uri")) else JSONObject()
        if (fromUri.has("uri") || fromUri.has("pairing")) fail(FIELDS)
        val normalized = JSONObject()
        for (source in listOf(outer, fromUri, info)) {
            if (source.has("version") && source.get("version") != 1L) fail(VERSION)
            for (key in fields) if (source.has(key)) {
                val candidate = source.get(key)
                if (normalized.has(key)) {
                    val previous = normalized.get(key)
                    val same = if (previous is Number && candidate is Number) previous.toDouble() == candidate.toDouble()
                        else previous == candidate
                    if (!same) fail(CONFLICT)
                }
                normalized.put(key, candidate)
            }
        }
        if (!normalized.has("ticket") || normalized.optString("ticket").isBlank()) fail(MISSING_TICKET)
        if (!normalized.has("code") || normalized.optString("code").isBlank()) fail(MISSING_CODE)
        val ticket = normalized.getString("ticket")
        val code = normalized.getString("code")
        if (ticket.length > 16384 || ticket.any { it.isWhitespace() || it.code < 32 || it.code == 127 } ||
            code.length > 128 || !code.matches(Regex("[A-Za-z0-9_-]+"))) fail(FIELDS)
        if (normalized.has("endpoint_id")) {
            val endpoint = normalized.getString("endpoint_id")
            if (endpoint.isEmpty() || endpoint.length > 200 || endpoint.any { it.isWhitespace() || it.code < 32 || it.code == 127 }) fail(FIELDS)
        }
        if (normalized.has("expires_at")) {
            val expiry = (normalized.get("expires_at") as? Number)?.toDouble() ?: fail(FIELDS)
            if (!expiry.isFinite()) fail(FIELDS)
            if (expiry <= nowMillis / 1000.0) fail(EXPIRED)
        }
        if (!normalized.has("version")) normalized.put("version", 1L)
        return normalized
    }

    private fun readUri(value: String): JSONObject {
        if (!value.startsWith(PREFIX)) fail(LINK)
        val encoded = value.removePrefix(PREFIX)
        if (!encoded.matches(Regex("[A-Za-z0-9_-]+={0,2}")) || encoded.length % 4 == 1 ||
            ('=' in encoded && encoded.length % 4 != 0)) fail(LINK)
        val decoded = try {
            val bytes = Base64.decode(encoded, Base64.URL_SAFE or Base64.NO_WRAP)
            val canonical = Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
            if (canonical != encoded.trimEnd('=')) fail(LINK)
            Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
        } catch (_: Exception) { fail(LINK) }
        return readJson(decoded, LINK)
    }

    /** Strict JSON also rejects trailing input, duplicate keys and coerced types. */
    private fun readJson(value: String, formatError: String): JSONObject = try {
        JsonReader(StringReader(value)).use { reader ->
            reader.isLenient = false
            val result = readObject(reader, 0)
            if (reader.peek() != JsonToken.END_DOCUMENT) fail(formatError)
            result
        }
    } catch (invalid: PairingInputException) { throw invalid }
    catch (_: Exception) { fail(formatError) }

    private fun readObject(reader: JsonReader, depth: Int): JSONObject {
        if (reader.peek() != JsonToken.BEGIN_OBJECT || depth > 1) fail(FIELDS)
        reader.beginObject()
        val result = JSONObject()
        val seen = mutableSetOf<String>()
        while (reader.hasNext()) {
            val name = reader.nextName()
            if (!seen.add(name)) fail(FIELDS)
            when {
                name == "pairing" -> {
                    if (depth != 0) fail(FIELDS)
                    result.put(name, readObject(reader, depth + 1))
                }
                name in stringFields -> {
                    if (reader.peek() != JsonToken.STRING) fail(FIELDS)
                    result.put(name, reader.nextString())
                }
                name == "version" -> {
                    if (reader.peek() != JsonToken.NUMBER) fail(VERSION)
                    result.put(name, reader.nextString().toLongOrNull() ?: fail(VERSION))
                }
                name == "expires_at" -> {
                    if (reader.peek() != JsonToken.NUMBER) fail(FIELDS)
                    val number = reader.nextString().toDoubleOrNull() ?: fail(FIELDS)
                    if (!number.isFinite()) fail(FIELDS)
                    result.put(name, number)
                }
                else -> reader.skipValue()
            }
        }
        reader.endObject()
        return result
    }

    private fun fail(message: String): Nothing = throw PairingInputException(message)
}
