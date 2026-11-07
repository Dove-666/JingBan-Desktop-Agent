package com.aura.companion.data

import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.net.InetAddress
import java.net.ServerSocket
import java.net.SocketTimeoutException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class ModelReasoningTest {
    private val gateway = "https://fixture.example/v1"
    private fun rpc(model: String = "fixture-model", effort: String = "default") = JSONObject()
        .put("version", 1).put("revision", "fixture-revision").put("base_url", gateway).put("model", model)
        .put("api_key", "PRIVATE_SYNTHETIC_KEY").put("system_prompt", "fixture system prompt")
        .put("timeout_seconds", 10).put("reasoning_effort", effort)

    @Test fun freshKnownModelsChooseTheirOwnHighestSupportedEffort() {
        assertEquals("max", ModelReasoning.defaultSelection(gateway, "deepseek-flash").getString("value"))
        assertEquals("xhigh", ModelReasoning.defaultSelection(gateway, "gpt-5.4").getString("value"))
        assertEquals("high", ModelReasoning.defaultSelection(gateway, "gpt-5").getString("value"))
        assertEquals("default", ModelReasoning.defaultSelection(gateway, "private-alias").getString("value"))
        val toggle = ModelReasoning.defaultSelection(gateway, "private-alias", "thinking")
        assertEquals("enabled", toggle.getString("value"))
    }

    @Test fun highestDefaultRespectsUnsortedExactMetadataAndNeverGuessesPrivateTokens() {
        val metadata = JSONObject().put("version", 1).put("base_url", gateway).put("model", "alias")
            .put("profile", "openai").put("values", JSONArray(listOf("high", "none", "low")))
        assertEquals("high", ModelReasoning.defaultSelection(gateway, "alias", metadata = metadata).getString("value"))
        assertEquals("default", ModelReasoning.defaultSelection(gateway, "other-alias", metadata = metadata).getString("value"))
        metadata.put("values", JSONArray(listOf("Turbo-Private")))
        assertEquals("default", ModelReasoning.defaultSelection(gateway, "alias", metadata = metadata).getString("value"))
    }

    @Test fun highestIsOnlyAnUnsetEditorDefaultAndSavedChoicesRetainTheirWire() {
        for (value in listOf("default", "none", "low")) {
            val edit = ModelReasoning.forEditing(gateway, "deepseek-flash", value, "auto")
            assertEquals(value, edit.getString("value"))
        }
        val fresh = ModelReasoning.forEditing(gateway, "deepseek-flash", "default", "auto", useHighestDefault = true)
        assertEquals("max", fresh.getString("value"))
        val wire = ModelReasoning.body(gateway, "deepseek-flash", fresh.getString("value"))
        assertEquals("max", wire.getString("reasoning_effort"))
        assertEquals("enabled", wire.getJSONObject("thinking").getString("type"))
    }

    @Test fun catalogueUsesDistinctModelOptionsAndOnlyExactDatedFamilies() {
        assertTrue(ModelReasoning.profileChoices().any { it.first == "auto" })
        assertFalse(ModelReasoning.profileChoices().any { it.first == "legacy" })
        val gpt = ModelReasoning.resolve(gateway, "gpt-6.1-sol")
        assertFalse(gpt.values.contains("none"))
        assertFalse(gpt.values.contains("minimal"))
        assertTrue(gpt.values.contains("max"))
        assertFalse(gpt.allowCustom)
        assertEquals(gpt.values, ModelReasoning.resolve(gateway, "gpt-6.1-sol-2026-10-01").values)
        assertEquals(listOf("default"), ModelReasoning.resolve(gateway, "gpt-6.1-sol-image").values)
        assertTrue(ModelReasoning.resolve(gateway, "gpt-5.4").values.contains("none"))
        assertFalse(ModelReasoning.resolve(gateway, "gpt-5.4").values.contains("max"))
    }

    @Test fun platformTakesPrecedenceOverVendorAndOffReallyDisablesThinking() {
        assertEquals(listOf("default", "none", "low", "high", "max"), ModelReasoning.resolve(gateway, "deepseek-flash").values)
        val direct = ModelReasoning.body(gateway, "deepseek-flash", "high")
        assertEquals("enabled", direct.getJSONObject("thinking").getString("type"))
        assertEquals("high", direct.getString("reasoning_effort"))
        val off = ModelReasoning.body(gateway, "deepseek-flash", "none")
        assertEquals("disabled", off.getJSONObject("thinking").getString("type"))
        assertFalse(off.has("reasoning_effort"))
        assertEquals(listOf("default"), ModelReasoning.resolve("https://openrouter.ai/api/v1", "deepseek-flash").values)
        val declared = JSONObject().put("version", 1).put("base_url", "https://openrouter.ai/api/v1")
            .put("model", "deepseek-flash").put("profile", "openrouter").put("values", JSONArray(listOf("high")))
        val router = ModelReasoning.body("https://openrouter.ai/api/v1", "deepseek-flash", "high", metadata = declared)
        assertEquals("high", router.getJSONObject("reasoning").getString("effort"))
        assertFalse(router.has("thinking")); assertFalse(router.has("reasoning_effort"))
        assertEquals("unknown", ModelReasoning.resolve("https://openrouter.ai.evil.example/v1", "ordinary-alias").source)
    }

    @Test fun unknownIsDefaultOnlyAndCustomValuesAreExplicitAndBounded() {
        assertEquals(listOf("default"), ModelReasoning.resolve(gateway, "unpublished-alias").values)
        assertEquals(0, ModelReasoning.body(gateway, "unpublished-alias").length())
        assertThrows(IllegalArgumentException::class.java) { ModelReasoning.body(gateway, "unpublished-alias", "high") }
        assertEquals("ultra", ModelReasoning.body(gateway, "unpublished-alias", "ultra", "custom").getString("reasoning_effort"))
        assertEquals("UltraMode", ModelReasoning.body(gateway, "unpublished-alias", "UltraMode", "custom").getString("reasoning_effort"))
        for (value in listOf("high\r\nAuthorization", "a".repeat(65), "", "../high")) {
            assertThrows(IllegalArgumentException::class.java) { ModelReasoning.body(gateway, "alias", value, "custom") }
        }
    }

    @Test fun togglesAndBudgetsUseTheirOwnFieldsAndDoNotLeakIntoEffort() {
        assertEquals(listOf("default", "none", "enabled"), ModelReasoning.resolve(gateway, "alias", "thinking").values)
        assertEquals("enabled", ModelReasoning.body(gateway, "alias", "enabled", "thinking").getJSONObject("thinking").getString("type"))
        val qwen = ModelReasoning.body(gateway, "alias", "enabled", "qwen_budget", 16384)
        assertTrue(qwen.getBoolean("enable_thinking")); assertEquals(16384, qwen.getInt("thinking_budget"))
        assertFalse(qwen.has("reasoning_effort"))
        assertEquals(8192, ModelReasoning.body(gateway, "alias", "enabled", "qwen_budget").getInt("thinking_budget"))
        assertFalse(ModelReasoning.body(gateway, "alias", "none", "qwen_budget").has("thinking_budget"))
        assertThrows(IllegalArgumentException::class.java) { ModelReasoning.body(gateway, "alias", "enabled", "claude_budget", 1) }
        assertThrows(IllegalArgumentException::class.java) { ModelReasoning.body(gateway, "alias", "high", "openai", 1024) }
        val adaptive = ModelReasoning.body(gateway, "alias", "high", "claude_adaptive")
        assertEquals("adaptive", adaptive.getJSONObject("thinking").getString("type"))
        assertEquals("high", adaptive.getJSONObject("output_config").getString("effort"))
    }

    @Test fun onlyBoundedExactApiModelDeclarationsCanChangeChoices() {
        val declaration = JSONObject().put("version", 1).put("base_url", "https://FIXTURE.example:443/v1/")
            .put("model", "private-alias").put("profile", "openai").put("values", JSONArray(listOf("high", "ultra")))
        val cap = ModelReasoning.resolve(gateway, "private-alias", metadata = declaration)
        assertEquals(listOf("default", "high", "ultra"), cap.values)
        assertEquals("metadata", cap.source); assertFalse(cap.allowCustom)
        assertEquals(listOf("default"), ModelReasoning.resolve(gateway, "other-alias", metadata = declaration).values)
        assertEquals(listOf("default"), ModelReasoning.resolve("https://other.example/v1", "private-alias", metadata = declaration).values)
        val injection = JSONObject(declaration.toString()).put("extra_body", JSONObject().put("tools", JSONArray()))
        assertEquals("unknown", ModelReasoning.resolve(gateway, "private-alias", metadata = injection).source)
        val invalidToggle = JSONObject(declaration.toString()).put("profile", "thinking")
        assertEquals("unknown", ModelReasoning.resolve(gateway, "private-alias", metadata = invalidToggle).source)
        val encodedRoot = JSONObject(declaration.toString()).put("base_url", "https://fixture.example/v1%2Fprivate")
        assertEquals("unknown", ModelReasoning.resolve("https://fixture.example/v1/private", "private-alias", metadata = encodedRoot).source)
    }

    @Test fun olderConfigMigratesWithoutChangingItsWireAndNewConfigRoundTrips() {
        val previous = DirectApiConfig.fromRpc(rpc(effort = "high"), "desktop", false)
        assertEquals("legacy", previous.reasoningProfile)
        assertEquals("high", ModelReasoning.body(previous.baseUrl, previous.model, previous.reasoningEffort, previous.reasoningProfile)
            .getString("reasoning_effort"))
        val newRpc = rpc().put("reasoning", ModelReasoning.selection("qwen_budget", "enabled", 16000))
        val original = DirectApiConfig.fromRpc(newRpc, "desktop", false)
        val loaded = DirectApiConfig.decode(original.encoded())
        assertEquals("qwen_budget", loaded.reasoningProfile); assertEquals("enabled", loaded.reasoningEffort)
        assertEquals(16000, loaded.reasoningBudgetTokens)
        assertEquals(16000, ModelReasoning.body(loaded.baseUrl, loaded.model, loaded.reasoningEffort,
            loaded.reasoningProfile, loaded.reasoningBudgetTokens).getInt("thinking_budget"))
        val manual = DirectApiConfig("phone", "r", gateway, "manual-alias", "PRIVATE_PHONE_KEY", "local role", 10,
            "ultra", 1, ApiConfigSource.LOCAL, reasoningProfile = "custom")
        val updated = manual.withDesktopCache(loaded).withPersona(SyncedPersona("desktop", "role-r", "Role", "new role"))
        assertEquals("custom", updated.reasoningProfile); assertEquals("ultra", updated.reasoningEffort)
        assertEquals("qwen_budget", updated.desktopCache!!.reasoningProfile)
        assertEquals("PRIVATE_PHONE_KEY", updated.apiKey); assertEquals("new role", updated.systemPrompt)
        assertFalse(updated.publicInfo().toString().contains("PRIVATE_PHONE_KEY"))
    }

    @Test fun openingAnOldConfigEditsInAutomaticModeWithoutMutatingItsOriginalWire() {
        val old = DirectApiConfig.fromRpc(rpc(effort = "high"), "desktop", false)
        val original = old.encoded()
        val draft = ModelReasoning.forEditing(old.baseUrl, old.model, old.reasoningEffort, old.reasoningProfile)
        assertEquals("auto", draft.getString("profile")); assertEquals("default", draft.getString("value"))
        assertEquals(listOf("default"), ModelReasoning.resolve(old.baseUrl, old.model, draft.getString("profile")).values)
        assertEquals("legacy", old.reasoningProfile)
        assertEquals("high", ModelReasoning.body(old.baseUrl, old.model, old.reasoningEffort, old.reasoningProfile).getString("reasoning_effort"))
        assertArrayEquals(original, old.encoded())
        val applicable = ModelReasoning.forEditing(gateway, "deepseek-flash", "high", "legacy")
        assertEquals("high", applicable.getString("value")); assertEquals("auto", applicable.getString("profile"))
    }

    @Test fun privateKeysCannotBeReflectedThroughEffortMetadataOrDesktopBackup() {
        val customLeak = rpc().put("api_key", "UltraMode")
            .put("reasoning", ModelReasoning.selection("custom", "UltraMode"))
        val failure = assertThrows(IllegalArgumentException::class.java) { DirectApiConfig.fromRpc(customLeak, "desktop", false) }
        assertFalse(failure.message.orEmpty().contains("UltraMode"))
        val metadata = JSONObject().put("version", 1).put("base_url", gateway).put("model", "fixture-model")
            .put("profile", "openai").put("values", JSONArray(listOf("high")))
        val metadataLeak = rpc().put("api_key", "high")
            .put("reasoning", ModelReasoning.selection("auto", "default", metadata = metadata))
        assertThrows(IllegalArgumentException::class.java) { DirectApiConfig.fromRpc(metadataLeak, "desktop", false) }
        val desktop = DirectApiConfig("desktop", "r", gateway, "fixture-model", "UltraMode", "role", 10, "default", 0)
        val manual = DirectApiConfig("phone", "r", gateway, "phone-model", "PHONE_SYNTHETIC_KEY", "role", 10,
            "UltraMode", 0, ApiConfigSource.LOCAL, desktop, reasoningProfile = "custom")
        assertThrows(IllegalArgumentException::class.java) { manual.requireSafePublicSettings() }
        val summary = manual.publicInfo()
        assertFalse(summary.available); assertNull(summary.reasoningMetadata)
        assertEquals("default", summary.reasoningEffort)
        assertFalse(summary.toString().contains("UltraMode"))
        assertFalse(summary.toString().contains("PHONE_SYNTHETIC_KEY"))
    }

    @Test fun aPrivateReasoningValueIsRejectedBeforeAnHttpConnectionIsOpened(): Unit = runBlocking {
        ServerSocket(0, 1, InetAddress.getByName("127.0.0.1")).use { server ->
            server.soTimeout = 250
            val unsafe = DirectApiConfig("phone", "r", "http://127.0.0.1:${server.localPort}/v1", "alias", "UltraMode",
                "role", 1, "UltraMode", 0, reasoningProfile = "custom")
            try { HttpDirectChatGateway().complete(unsafe, JSONArray()); fail("Private configuration cannot be submitted") }
            catch (failure: DirectApiFailure) { assertFalse(failure.safeMessage.contains("UltraMode")) }
            assertThrows(SocketTimeoutException::class.java) { server.accept().use { } }
        }
    }

    @Test fun publicHttpTransportSendsDeclaredParametersExactlyOnce(): Unit = runBlocking {
        for ((profile, value) in listOf("deepseek" to "high", "openrouter" to "none", "custom" to "ultra", "auto" to "default")) {
            val server = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
            val worker = Executors.newSingleThreadExecutor()
            val future = worker.submit<JSONObject> {
                server.accept().use { socket ->
                    socket.soTimeout = 5000
                    val input = socket.getInputStream().bufferedReader()
                    var length = 0
                    while (true) {
                        val line = input.readLine()
                        if (line.isEmpty()) break
                        if (line.startsWith("Content-Length:", true)) length = line.substringAfter(':').trim().toInt()
                    }
                    val data = CharArray(length); var offset = 0
                    while (offset < length) { val count = input.read(data, offset, length - offset); check(count > 0); offset += count }
                    val response = JSONObject().put("choices", JSONArray().put(JSONObject().put("finish_reason", "stop")
                        .put("message", JSONObject().put("content", "fixture reply")))).toString().toByteArray()
                    socket.getOutputStream().write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${response.size}\r\nConnection: close\r\n\r\n").toByteArray() + response)
                    JSONObject(String(data))
                }
            }
            try {
                val config = DirectApiConfig("phone", "r", "http://127.0.0.1:${server.localPort}/v1", "alias", "PRIVATE_HTTP_KEY",
                    "role", 10, value, 0, reasoningProfile = profile)
                assertEquals("fixture reply", HttpDirectChatGateway().complete(config, JSONArray()))
                val payload = future.get(5, TimeUnit.SECONDS)
                val expected = ModelReasoning.body(config.baseUrl, config.model, value, profile)
                for (key in expected.keys()) assertEquals(expected.get(key).toString(), payload.get(key).toString())
                for (key in listOf("thinking", "reasoning", "reasoning_effort", "enable_thinking", "thinking_budget", "output_config")) {
                    assertEquals(expected.has(key), payload.has(key))
                }
                assertTrue(payload.getBoolean("stream")); assertFalse(payload.has("temperature"))
            } finally { server.close(); worker.shutdownNow() }
        }
    }
}
