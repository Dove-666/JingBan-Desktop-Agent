package com.aura.companion.transport

import org.json.JSONObject
import org.json.JSONTokener
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.CodingErrorAction
import java.util.UUID

/** The desktop's bounded UTF-8 JSON protocol; no identity comes from JSON. */
object RpcProtocol {
    const val MAX_FRAME_BYTES = 8 * 1024 * 1024
    const val TIMEOUT_MILLIS = 30_000L
    const val MAX_CONCURRENT_REQUESTS = 4
    val ALPN: ByteArray get() = "aura-remote/1".toByteArray(Charsets.UTF_8)
    private val methodPattern = Regex("[a-z][a-z0-9_]*(?:\\.[a-z][a-z0-9_]*)*")
    private val earlyErrors = setOf("frame_too_large", "invalid_frame", "invalid_request", "busy", "disabled")

    fun request(method: String, params: JSONObject, requestId: String): JSONObject {
        if (method.length > 64 || !methodPattern.matches(method) || !canonicalUuid(requestId)) {
            throw RpcTransportException("invalid_request", "请求格式无效。")
        }
        // Snapshot mutable params before another UI/repository coroutine changes them.
        return JSONObject().put("id", requestId).put("method", method)
            .put("params", JSONObject(params.toString()))
    }

    fun canonicalUuid(value: String): Boolean = try {
        UUID.fromString(value).toString() == value
    } catch (_: IllegalArgumentException) { false }

    fun frameHeader(size: Int): ByteArray {
        requireSize(size)
        return ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(size).array()
    }

    /** Validate unsigned wire length before asking the native stream for a buffer. */
    fun frameLength(header: ByteArray): Int {
        if (header.size != 4) throw RpcTransportException("invalid_frame", "电脑返回的消息不完整。")
        val length = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xffff_ffffL
        if (length == 0L || length > MAX_FRAME_BYTES.toLong()) {
            throw RpcTransportException("frame_too_large", "消息超过 8 MiB，请减少同步页大小或图片体积。")
        }
        return length.toInt()
    }

    fun encode(value: JSONObject): ByteArray {
        val bytes = value.toString().toByteArray(Charsets.UTF_8)
        requireSize(bytes.size)
        return bytes
    }

    fun decode(bytes: ByteArray): JSONObject {
        requireSize(bytes.size)
        try {
            val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString()
            val reader = JSONTokener(text)
            val value = reader.nextValue()
            if (value !is JSONObject || reader.nextClean() != '\u0000') {
                throw RpcTransportException("invalid_frame", "电脑返回的消息格式无效。")
            }
            return value
        } catch (error: RpcTransportException) {
            throw error
        } catch (_: Exception) {
            throw RpcTransportException("invalid_frame", "电脑返回的消息格式无效。")
        }
    }

    fun validateResponse(response: JSONObject, requestId: String): JSONObject {
        val ok = response.opt("ok")
        if (ok !is Boolean) throw RpcTransportException("invalid_frame", "电脑返回的消息格式无效。")
        if (response.opt("id") != requestId) {
            // Header/size rejection happens before the server has read an id.
            val earlyCode = response.optJSONObject("error")?.optString("code", "")
            if (ok || !response.isNull("id") || earlyCode !in earlyErrors) {
                throw RpcTransportException("invalid_frame", "电脑返回的消息标识不匹配。")
            }
        }
        if ((ok && response.optJSONObject("result") == null)
            || (!ok && response.optJSONObject("error") == null)) {
            throw RpcTransportException("invalid_frame", "电脑返回的消息格式无效。")
        }
        return response
    }

    private fun requireSize(size: Int) {
        if (size <= 0 || size > MAX_FRAME_BYTES) {
            throw RpcTransportException("frame_too_large", "消息超过 8 MiB，请减少同步页大小或图片体积。")
        }
    }
}

/** Static safe text only: no SDK exception strings, payloads, tickets or secrets. */
class RpcTransportException(val code: String, message: String) : Exception(message)
