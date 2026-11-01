package com.aura.companion.data

import android.graphics.Bitmap
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aura.companion.transport.IrohRpcClient
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.UUID

/** Real Android HTTP + encrypted desktop RPC, isolated mock provider only. */
@RunWith(AndroidJUnit4::class)
class AndroidDirectApiE2eTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test(timeout = 300_000)
    fun directApiCacheImageContextImportsConfigurationRotationAndRevocation() = runBlocking {
        val configuration = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Start --direct-api isolated desktop fixture", configuration.isFile)
        val input = JSONObject(configuration.readText())
        assumeTrue("This test requires the isolated direct API mock fixture", input.optBoolean("isolated_fixture") && input.optBoolean("direct_api") && input.optBoolean("mock_chat_model"))
        val name = "direct-api-e2e-${UUID.randomUUID()}"
        val vault = EncryptedApiConfigVault(context, "$name.enc")
        var database = CompanionDatabase(context, "$name.db")
        var repository = AppRepository(context, IrohRpcClient(context), database, vault)
        val steps = mutableListOf<String>()
        var passed = false
        var finalInspection = JSONObject()
        try {
            repository.pair(input.getString("pairing_uri"), "Isolated direct API Android QA")
            assertEquals("Pairing failed safely: ${repository.state.value.error}", ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertTrue(repository.state.value.directApi.available)
            assertTrue(repository.state.value.directApi.baseUrl.startsWith("http://10.0.2.2:"))
            steps += "encrypted_api_config_sync"

            val cid = input.getString("seed_conversation_id")
            repository.selectConversation(cid)
            repository.sendMessage(cid, "ANDROID_DIRECT_API_ONLINE")
            await("direct online reply") { !repository.state.value.directChatBusy && repository.state.value.messages.any { it.content == "Android E2E reply 1" } }
            awaitImported(repository)
            assertEquals(1, command("inspect").getInt("mock_chat_requests"))
            steps += "phone_http_model_and_desktop_import_without_second_generation"

            repository.disconnect()
            repository.sendMessage(cid, "ANDROID_DIRECT_API_PC_OFFLINE")
            await("cached API offline reply") { !repository.state.value.directChatBusy && repository.state.value.messages.any { it.content == "Android E2E reply 2" } }
            assertEquals(ConnectionState.DISCONNECTED, repository.state.value.connectionState)
            assertEquals(1, repository.state.value.historyPendingCount)
            val offlineUserId = repository.state.value.messages.single { it.content == "ANDROID_DIRECT_API_PC_OFFLINE" }.id
            steps += "desktop_disconnected_cached_api_still_answers"

            // Close/reopen the real DB and authenticated configuration file.
            // Pending imports survive; no previously generated turn is replayed.
            repository.close()
            database = CompanionDatabase(context, "$name.db")
            repository = AppRepository(context, IrohRpcClient(context), database, vault)
            await("encrypted cache restored") { repository.state.value.directApi.available }
            repository.selectConversation(cid)
            assertEquals(offlineUserId, repository.state.value.messages.single { it.content == "ANDROID_DIRECT_API_PC_OFFLINE" }.id)
            assertTrue(repository.state.value.messages.any { it.content == "Android E2E reply 2" })
            assertEquals(2, command("inspect").getInt("mock_chat_requests"))
            steps += "restart_keeps_cached_api_and_completed_unsynced_reply"

            val bitmap = Bitmap.createBitmap(32, 24, Bitmap.Config.ARGB_8888).apply { eraseColor(0xff73a9ed.toInt()) }
            val bytes = ByteArrayOutputStream().use { out -> bitmap.compress(Bitmap.CompressFormat.PNG, 100, out); out.toByteArray() }
            bitmap.recycle()
            val imageId = ImageCache.digest(bytes)
            repository.stageImage(cid, bytes, "isolated-direct.png", "image/png")
            assertEquals(2, command("inspect").getInt("mock_chat_requests"))
            repository.sendMessage(cid, "ANDROID_DIRECT_API_IMAGE")
            await("direct image reply") { !repository.state.value.directChatBusy && repository.state.value.messages.any { it.content == "Android E2E reply 3" } }
            assertEquals(imageId, repository.state.value.messages.single { it.content == "ANDROID_DIRECT_API_IMAGE" }.attachmentId)
            repository.sendMessage(cid, "上面那张图片是什么颜色？ ANDROID_DIRECT_API_FOLLOWUP")
            await("same-thread image followup") { !repository.state.value.directChatBusy && repository.state.value.messages.any { it.content == "Android E2E reply 4" } }
            val imageInspect = command("inspect")
            assertEquals(2, imageInspect.getInt("image_requests"))
            assertFalse(imageInspect.getBoolean("last_current_user_has_image"))
            assertTrue(imageInspect.getBoolean("last_history_has_image"))
            assertNull(repository.state.value.messages.single { it.content.contains("ANDROID_DIRECT_API_FOLLOWUP") }.attachmentId)
            assertTrue(imageInspect.getBoolean("image_payloads_valid"))
            assertEquals(imageId, imageInspect.getString("last_image_sha256"))
            steps += "real_phone_data_uri_image_and_same_conversation_followup"

            val other = repository.newConversation()
            repository.sendMessage(other, "ANDROID_DIRECT_API_OTHER_CONVERSATION")
            await("other conversation no image") { !repository.state.value.directChatBusy && repository.state.value.messages.any { it.content == "Android E2E reply 5" } }
            assertFalse(command("inspect").getBoolean("last_request_has_image"))
            steps += "image_context_never_crosses_conversations"

            repository.reconnect(); awaitImported(repository)
            repository.selectConversation(cid)
            assertEquals(offlineUserId, repository.state.value.messages.single { it.content == "ANDROID_DIRECT_API_PC_OFFLINE" }.id)
            assertEquals(5, command("inspect").getInt("mock_chat_requests"))
            assertTrue(repository.state.value.outbox.filter { it.method == "chat.import" }.all { it.status == "sent" })
            steps += "offline_exchanges_import_stable_ids_images_first_no_model_replay"

            command("rotate_api"); repository.syncNow()
            assertEquals("isolated-rotated-model", repository.state.value.directApi.model)
            repository.selectConversation(other)
            repository.sendMessage(other, "ANDROID_DIRECT_API_ROTATED_CONFIGURATION")
            await("rotated model reply") { !repository.state.value.directChatBusy && repository.state.value.messages.any { it.content == "Android E2E reply 6" } }
            awaitImported(repository)
            val rotated = command("inspect")
            assertTrue(rotated.getBoolean("last_request_uses_current_key"))
            assertEquals("isolated-rotated-model", rotated.getString("last_request_model"))
            assertEquals("low", rotated.getString("last_request_reasoning_effort"))
            steps += "automatic_key_model_reasoning_rotation"

            command("disable_api"); repository.syncNow()
            assertTrue(repository.state.value.directApi.available)
            assertNotNull(vault.load())
            assertTrue(repository.state.value.directApi.message.contains("继续使用"))
            command("enable_api"); repository.syncNow(); assertTrue(repository.state.value.directApi.available)
            steps += "temporarily_unavailable_desktop_config_keeps_working_phone_cache"

            command("revoke_devices"); repository.syncNow()
            assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            assertFalse(repository.state.value.directApi.available); assertNull(vault.load())
            repository.reconnect(); assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            steps += "observed_revocation_clears_api_key_and_blocks_auto_pairing"
            finalInspection = command("inspect")
            assertEquals(6, finalInspection.getInt("mock_chat_requests"))
            assertEquals(2, finalInspection.getInt("image_requests"))
            assertEquals(0, finalInspection.getInt("authorized_devices"))
            assertEquals(0, finalInspection.getInt("desktop_chat_send_operations"))
            assertEquals(6, finalInspection.getInt("desktop_import_operations"))
            assertTrue(finalInspection.getBoolean("system_prompt_present"))
            passed = true
        } finally {
            val report = JSONObject().put("passed", passed).put("steps", JSONArray(steps)).put("mock_chat_model", true)
                .put("real_android_http", true).put("real_encrypted_desktop_rpc", true).put("hardware_phone_tested", false)
                .put("https_provider_tested", false).put("cross_network_tested", false)
                .put("inspection", finalInspection)
            File(context.filesDir, "aura-direct-api-e2e-result.json").writeText(report.toString(2))
            configuration.delete(); repository.close(); vault.clear(); context.deleteDatabase("$name.db")
        }
    }

    private suspend fun await(label: String, condition: () -> Boolean) = withTimeout(60_000) {
        while (!condition()) { check(label.isNotEmpty()); delay(200) }
    }

    private suspend fun awaitImported(repository: AppRepository) = withTimeout(90_000) {
        while (repository.state.value.historyPendingCount != 0) {
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            repository.syncNow(); delay(300)
        }
    }

    private suspend fun command(action: String, params: JSONObject = JSONObject()): JSONObject {
        val id = UUID.randomUUID().toString()
        val temporary = File(context.filesDir, "aura-e2e-command.tmp")
        temporary.writeText(JSONObject().put("id", id).put("action", action).put("params", params).toString())
        check(temporary.renameTo(File(context.filesDir, "aura-e2e-command.json")))
        return withTimeout(30_000) {
            var result: JSONObject? = null
            while (result == null) {
                val ack = runCatching { JSONObject(File(context.filesDir, "aura-e2e-ack.json").readText()) }.getOrNull()
                if (ack?.optString("id") == id) {
                    assertTrue("Isolated desktop control failed", ack.optBoolean("ok")); result = ack.getJSONObject("result")
                } else delay(200)
            }
            checkNotNull(result)
        }
    }
}
