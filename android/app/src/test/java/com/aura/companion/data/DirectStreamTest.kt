package com.aura.companion.data

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.StringReader
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class DirectStreamTest {
    private val secret = "SYNTHETIC_KEY_DO_NOT_SHOW"
    private fun event(text: String?, finish: String? = null, reasoning: String? = null): String = "data: " + JSONObject()
        .put("choices", JSONArray().put(JSONObject().put("index", 0).put("delta", JSONObject().apply {
            text?.let { put("content", it) }; reasoning?.let { put("reasoning_content", it) }
        }).put("finish_reason", finish ?: JSONObject.NULL))).toString() + "\n\n"

    @Test fun jsonReplyStreamsEscapesUnicodeAndOnlyPublicReplyWithoutCrossChunkSecret() {
        val updates = mutableListOf<String>()
        val visible = DirectVisibleText(secret) { updates += it.text }
        val raw = "```json\n" + JSONObject().put("emotion", "PRIVATE_METADATA").put("reply", "你好\n鲸鱼 🐳 \\\" $secret 好的").toString() + "\n```"
        raw.forEach { visible.append(it.toString()) }
        val reply = visible.finish()
        assertEquals("你好\n鲸鱼 🐳 \\\" [已隐藏密钥] 好的", reply)
        assertTrue(updates.size > 6)
        assertTrue(updates.all { !it.contains("PRIVATE_METADATA") && !it.contains(secret) && !it.contains("\"reply\"") })
        assertFalse(updates.any { it.lastOrNull()?.isHighSurrogate() == true })
        assertTrue(updates.filter { it.isNotBlank() }.all { reply.startsWith(it) })
    }

    @Test fun sseUsesDataEventsIgnoresReasoningAndRejectsMissingCompletionOrLength() {
        val updates = mutableListOf<String>()
        val sse = ": ping\n\n" + event(null, reasoning = "PRIVATE_THOUGHT") + event("first ") + event("second", "stop") +
            "data: {\"choices\":[],\"usage\":{\"total_tokens\":3}}\n\ndata: [DONE]\n\n"
        assertEquals("first second", readDirectEvents(StringReader(sse).buffered(), secret, { updates += it.text }))
        assertEquals(listOf("first ", "first second"), updates)
        for (broken in listOf(event("partial"), event("partial", "length") + "data: [DONE]\n\n")) {
            val partial = mutableListOf<String>()
            try { readDirectEvents(StringReader(broken).buffered(), secret, { partial += it.text }); fail("Incomplete must fail") }
            catch (_: DirectApiFailure) { assertEquals("partial", partial.last()) }
        }
    }

    @Test fun ordinaryJsonAnswerStaysValidWhileIncompletePersonaEnvelopeCannotLeakMetadata() {
        val ordinary = mutableListOf<String>()
        val visible = DirectVisibleText(secret) { ordinary += it.text }
        visible.append("```json\n{\"count\":")
        assertTrue(ordinary.isEmpty())
        visible.append("2}\n```")
        assertEquals("```json\n{\"count\":2}\n```", visible.finish())
        val metadata = mutableListOf<String>()
        val malformed = DirectVisibleText(secret) { metadata += it.text }
        malformed.append("{\"emotion\":\"PRIVATE\",\"reply\":\"visible")
        try { malformed.finish(); fail("Incomplete JSON must not finalize") } catch (_: DirectApiFailure) { }
        assertEquals(listOf("visible"), metadata)
    }

    @Test fun httpEmitsBeforeResponseFinishesAndCancelsBlockedSocketWithoutSecondRequest() = runBlocking {
        val server = ServerSocket(0); val worker = Executors.newSingleThreadExecutor()
        val allowFinish = CountDownLatch(1); val sawText = CompletableDeferred<String>(); val requests = AtomicInteger()
        val serverWork = worker.submit {
            server.accept().use { socket ->
                requests.incrementAndGet()
                val input = socket.getInputStream().bufferedReader(); var length = 0
                while (true) { val line = input.readLine(); if (line.isEmpty()) break
                    if (line.startsWith("Content-Length:", true)) length = line.substringAfter(':').trim().toInt() }
                val body = CharArray(length); var n = 0
                while (n < length) n += input.read(body, n, length - n)
                val payload = JSONObject(String(body))
                assertTrue(payload.getBoolean("stream")); assertFalse(payload.has("max_tokens")); assertFalse(payload.has("max_completion_tokens"))
                val output = socket.getOutputStream()
                output.write("HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n".toByteArray())
                output.write(event("already visible").toByteArray()); output.flush()
                allowFinish.await(4, TimeUnit.SECONDS)
            }
        }
        try {
            val config = DirectApiConfig("desktop", "rev", "http://127.0.0.1:${server.localPort}/v1", "test-model", secret,
                "prompt", 10, "default", 0)
            val request = launch { HttpDirectChatGateway().stream(config, JSONArray()) { sawText.complete(it.text) } }
            assertEquals("already visible", withTimeout(3000) { sawText.await() })
            assertTrue(request.isActive)
            withTimeout(1500) { request.cancelAndJoin() }
            allowFinish.countDown(); serverWork.get(4, TimeUnit.SECONDS)
            assertEquals(1, requests.get())
        } finally { allowFinish.countDown(); server.close(); worker.shutdownNow() }
    }

    @Test fun nonStreamingJsonRequiresExplicitStopAndNeverInfersCompletionFromMissingReason() = runBlocking {
        for (finish in listOf(null, JSONObject.NULL, "length", "content_filter", "stop")) {
            val server = ServerSocket(0); val worker = Executors.newSingleThreadExecutor()
            val requests = AtomicInteger()
            val choice = JSONObject().put("message", JSONObject().put("content", "public fixture answer"))
            if (finish != null) choice.put("finish_reason", finish)
            val response = JSONObject().put("choices", JSONArray().put(choice)).toString().toByteArray()
            val serverWork = worker.submit {
                server.accept().use { socket ->
                    socket.soTimeout = 3000
                    requests.incrementAndGet()
                    val input = socket.getInputStream().bufferedReader(); var length = 0
                    while (true) {
                        val line = input.readLine(); if (line.isEmpty()) break
                        if (line.startsWith("Content-Length:", true)) length = line.substringAfter(':').trim().toInt()
                    }
                    var remaining = length
                    while (remaining > 0) {
                        val n = input.read(CharArray(remaining)); check(n > 0); remaining -= n
                    }
                    socket.getOutputStream().apply {
                        write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${response.size}\r\n" +
                            "Connection: close\r\n\r\n").toByteArray())
                        write(response); flush()
                    }
                }
            }
            try {
                val config = DirectApiConfig("desktop", "rev", "http://127.0.0.1:${server.localPort}/v1", "fixture-model", secret,
                    "prompt", 10, "default", 0)
                try {
                    val reply = HttpDirectChatGateway().complete(config, JSONArray())
                    assertEquals("Only explicit stop may complete", "stop", finish)
                    assertEquals("public fixture answer", reply)
                } catch (failure: DirectApiFailure) {
                    assertNotEquals("An explicit stop response should succeed", "stop", finish)
                }
                serverWork.get(4, TimeUnit.SECONDS)
                assertEquals(1, requests.get())
            } finally { server.close(); worker.shutdownNow() }
        }
    }
}
