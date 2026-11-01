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

/** Real native QUIC + HTTP, synthetic foreground only; never controls the host. */
@RunWith(AndroidJUnit4::class)
class AndroidDesktopAssistE2eTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test(timeout = 300_000)
    fun persistentDesktopAgentUsesToolsAndExplicitImagesWithoutScreenshotGate() = runBlocking {
        val configuration = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Start the --desktop-assist-qa synthetic fixture", configuration.isFile)
        val input = JSONObject(configuration.readText())
        assumeTrue(input.optBoolean("isolated_fixture") && input.optBoolean("mock_chat_model") &&
            input.optBoolean("synthetic_desktop") && !input.optBoolean("use_existing_api"))
        val api = input.getJSONObject("manual_api")
        require(api.getString("api_key").startsWith("ISOLATED_ANDROID_"))
        require(api.getString("base_url").startsWith("http://10.0.2.2:"))
        val name = "desktop-assist-e2e-${UUID.randomUUID()}"
        val database = CompanionDatabase(context, "$name.db")
        val vault = EncryptedApiConfigVault(context, "$name.enc")
        val repository = AppRepository(context, IrohRpcClient(context), database, vault)
        val steps = mutableListOf<String>()
        var passed = false
        var inspection = JSONObject()
        try {
            repository.pair(input.getString("pairing_uri"), "Isolated desktop assistance QA")
            assertEquals("Pair failed safely: ${repository.state.value.error}", ConnectionState.CONNECTED,
                repository.state.value.connectionState)
            assertTrue(repository.state.value.desktopTaskAvailable)
            // Desktop uses localhost inside the fixture; phone gets its own
            // synthetic emulator URL, just as a real LOCAL preference can differ.
            assertTrue(repository.saveDirectApiConfig(api.getString("base_url"), api.getString("model"),
                api.getString("api_key"), 10, "default"))
            val initial = command("inspect")
            val initialHttp = initial.getInt("mock_chat_requests")
            val initialSends = initial.getInt("desktop_agent_send_operations")
            val initialAgent = initial.getInt("desktop_agent_executions")
            val initialCancels = initial.getInt("desktop_agent_cancel_operations")
            val initialCaptures = initial.getInt("synthetic_desktop_captures")
            val initialModel = initial.getInt("desktop_completion_calls")
            steps += "paired_real_native_quic_with_actual_agent_capability"

            val cid = repository.newConversation()
            repository.setDesktopAgentMode(cid, true)
            val beforeMessages = repository.state.value.messages.size
            val snapshotId = repository.requestDesktopScreenshot()
            assertNotNull(snapshotId)
            assertNotNull(repository.downloadImage(snapshotId!!))
            assertEquals(beforeMessages, repository.state.value.messages.size)
            val snapshot = command("inspect")
            assertEquals(initialHttp, snapshot.getInt("mock_chat_requests"))
            assertEquals(initialCaptures + 1, snapshot.getInt("synthetic_desktop_captures"))
            assertEquals(initialModel, snapshot.getInt("desktop_completion_calls"))
            steps += "explicit_snapshot_downloads_without_ai_or_fake_chat_message"

            val bitmap = Bitmap.createBitmap(16, 12, Bitmap.Config.ARGB_8888).apply { eraseColor(0xffc27c90.toInt()) }
            val bytes = ByteArrayOutputStream().use { output ->
                bitmap.compress(Bitmap.CompressFormat.PNG, 100, output); output.toByteArray()
            }
            bitmap.recycle()
            repository.stageImage(cid, bytes, "pending-phone.png", "image/png")
            val attachedId = repository.state.value.pendingImages.getValue(cid).attachmentId
            assertTrue(repository.sendDesktopMessage(cid, "AGENT_IMAGE_QA"))
            awaitReply(repository, "Android Agent reply ${initialAgent + 1}")
            val desktopUser = repository.state.value.messages.single { it.content == "AGENT_IMAGE_QA" }
            assertEquals(attachedId, desktopUser.attachmentId)
            assertEquals("agent", desktopUser.executionMode)
            assertNull(repository.state.value.pendingImages[cid])
            val desktop = command("inspect")
            assertEquals(initialHttp, desktop.getInt("mock_chat_requests"))
            assertEquals(initialModel, desktop.getInt("desktop_completion_calls"))
            assertEquals(initialSends + 1, desktop.getInt("desktop_agent_send_operations"))
            assertEquals(initialCaptures + 1, desktop.getInt("synthetic_desktop_captures"))
            assertTrue(desktop.getBoolean("agent_last_has_image"))
            assertEquals("agent", desktop.getString("agent_last_kind"))
            assertTrue(desktop.getBoolean("agent_context_classification_absent"))
            steps += "explicit_image_uses_real_agent_rpc_without_initial_capture_or_phone_model"

            val other = repository.newConversation()
            repository.selectConversation(cid)
            assertTrue(cid in repository.state.value.desktopAgentConversations)
            assertFalse(other in repository.state.value.desktopAgentConversations)
            assertTrue(repository.sendDesktopMessage(cid, "继续"))
            awaitReply(repository, "Android Agent reply ${initialAgent + 2}")
            val followup = command("inspect")
            assertEquals(2, followup.getInt("agent_session_turns"))
            assertFalse(followup.getBoolean("agent_last_has_image"))
            assertEquals(initialCaptures + 1, followup.getInt("synthetic_desktop_captures"))
            steps += "neutral_followup_keeps_agent_conversation_without_keyword_classification"

            assertTrue(repository.sendDesktopMessage(cid, "QA_WAIT_FOR_CANCEL"))
            awaitState(repository) { it.desktopAgentBusy }
            assertTrue(repository.cancelDesktopTask(cid))
            awaitState(repository) { !it.desktopAgentBusy && it.messages.any { message -> message.requestStatus == "cancelled" } }
            val cancelled = command("inspect")
            assertEquals(initialCancels + 1, cancelled.getInt("desktop_agent_cancel_operations"))
            assertEquals(initialSends + 3, cancelled.getInt("desktop_agent_send_operations"))
            steps += "running_agent_can_be_cancelled_and_actual_status_syncs"

            repository.sendMessage(cid, "ORDINARY_PHONE_CHAT_AFTER_DESKTOP_TASK")
            awaitReply(repository, "Android E2E reply ${initialHttp + 1}")
            val phone = command("inspect")
            val phoneTrace = repository.state.value.messages.single {
                it.role == "user" && it.content == "ORDINARY_PHONE_CHAT_AFTER_DESKTOP_TASK"
            }.execution
            assertNotNull("Phone direct API process must be retained", phoneTrace)
            assertEquals("completed", phoneTrace!!.state)
            assertTrue(phoneTrace.events.any { it.label == "回复已完成" })
            assertEquals(initialModel, phone.getInt("desktop_completion_calls"))
            assertEquals(initialSends + 3, phone.getInt("desktop_agent_send_operations"))
            assertFalse(phone.getBoolean("last_request_has_image"))
            steps += "next_ordinary_chat_is_phone_direct_without_stale_screen"

            command("desktop_permission", JSONObject().put("allowed", false))
            assertNull(repository.requestDesktopScreenshot())
            repository.syncNow()
            assertTrue(repository.state.value.desktopTaskAvailable)
            assertEquals(initialCaptures + 1, command("inspect").getInt("synthetic_desktop_captures"))
            command("desktop_permission", JSONObject().put("allowed", true))
            steps += "independent_screen_permission_does_not_gate_agent_availability"

            repository.disconnect()
            assertFalse(repository.sendDesktopMessage(cid, "OFFLINE_DESKTOP_TASK_NEVER_REPLAYS"))
            assertNull(repository.requestDesktopScreenshot())
            assertTrue(repository.state.value.outbox.none { it.text == "OFFLINE_DESKTOP_TASK_NEVER_REPLAYS" })
            repository.sendMessage(cid, "OFFLINE_PC_PHONE_API_STILL_WORKS")
            awaitReply(repository, "Android E2E reply ${initialHttp + 2}")
            repository.reconnect(); repository.syncNow()
            inspection = command("inspect")
            assertEquals(initialHttp + 2, inspection.getInt("mock_chat_requests"))
            assertEquals(initialModel, inspection.getInt("desktop_completion_calls"))
            assertEquals(initialSends + 3, inspection.getInt("desktop_agent_send_operations"))
            assertEquals(initialCaptures + 1, inspection.getInt("synthetic_desktop_captures"))
            steps += "pc_disconnected_phone_still_chats_and_no_deferred_actions_execute"
            passed = true
        } finally {
            File(context.filesDir, "aura-desktop-assist-e2e-result.json").writeText(JSONObject()
                .put("passed", passed).put("steps", JSONArray(steps)).put("real_encrypted_desktop_rpc", true)
                .put("real_android_http", true).put("synthetic_desktop", true)
                .put("real_desktop_captures", 0).put("real_desktop_actions", 0)
                .put("cross_network_tested", false).put("inspection", inspection).toString(2))
            repository.close(); vault.clear(); context.deleteDatabase("$name.db")
        }
    }

    private suspend fun awaitReply(repository: AppRepository, reply: String) = withTimeout(60_000) {
        while (repository.state.value.directChatBusy || repository.state.value.messages.none { it.content == reply }) {
            repository.poll(); delay(200)
        }
    }

    private suspend fun awaitState(repository: AppRepository, condition: (AppState) -> Boolean) = withTimeout(60_000) {
        while (!condition(repository.state.value)) { repository.poll(); delay(200) }
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
