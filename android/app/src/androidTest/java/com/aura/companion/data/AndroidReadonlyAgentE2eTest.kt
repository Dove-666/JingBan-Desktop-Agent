package com.aura.companion.data

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
import java.io.File
import java.util.UUID

/** Opt-in real native QUIC, configured real planner, actual read-only C-drive metadata. */
@RunWith(AndroidJUnit4::class)
class AndroidReadonlyAgentE2eTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test(timeout = 240_000)
    fun phoneRunsConfiguredPlannerAgainstActualReadOnlyCDrive() = runBlocking {
        val config = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Start --readonly-real-agent-qa --use-existing-api isolated fixture", config.isFile)
        val input = JSONObject(config.readText())
        assumeTrue(input.optBoolean("isolated_fixture") && input.optBoolean("readonly_real_agent_qa") &&
            input.optBoolean("use_existing_api") && !input.optBoolean("mock_chat_model"))
        assertFalse("Real credentials must never be exported to Android", input.has("manual_api"))
        assertFalse("This acceptance must not use phone-direct generation", input.optBoolean("direct_api"))
        val name = "readonly-real-agent-e2e-${UUID.randomUUID()}"
        val database = CompanionDatabase(context, "$name.db")
        val vault = EncryptedApiConfigVault(context, "$name.enc")
        val rpc = IrohRpcClient(context)
        val repository = AppRepository(context, rpc, database, vault)
        val activities = mutableSetOf<String>()
        val steps = mutableListOf<String>()
        var inspection = JSONObject()
        var passed = false
        var replyLength = 0
        var runningTraceSeen = false
        var completedTraceSteps = 0
        val started = System.nanoTime()
        try {
            repository.pair(input.getString("pairing_uri"), "Real read-only Agent acceptance")
            assertEquals("Native pairing failed", ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertTrue("Actual Agent unavailable", repository.state.value.desktopTaskAvailable)
            assertFalse("Real model config must remain on desktop", repository.state.value.directApi.available)
            assertFalse("chat.config must not export the real key", rpc.request("chat.config").optBoolean("ok"))
            steps += "paired_actual_native_quic_without_exporting_real_api_credentials"

            val cid = repository.newConversation()
            repository.setDesktopAgentMode(cid, true)
            val task = "帮我清理一下C盘内存，告诉我有哪些可以清理的，先不要自己清理"
            assertTrue("Agent submission rejected", repository.sendDesktopMessage(cid, task))
            steps += "phone_submitted_exact_readonly_c_drive_request_through_agent_send"
            withTimeout(150_000) {
                while (true) {
                    repository.poll()
                    val state = repository.state.value
                    state.messages.mapNotNullTo(activities) { it.agentActivity }
                    val user = state.messages.firstOrNull { it.role == "user" && it.content == task }
                    val observedTrace = user?.execution
                    if (observedTrace?.state == "running" && observedTrace.events.isNotEmpty()) {
                        runningTraceSeen = true
                    }
                    if (user?.requestStatus in setOf("complete", "failed", "cancelled", "interrupted")) break
                    delay(250)
                }
            }
            repository.syncNow()
            val user = repository.state.value.messages.single { it.role == "user" && it.content == task }
            assertEquals("Actual Agent did not complete", "complete", user.requestStatus)
            assertEquals("agent", user.executionMode)
            assertNull("No Agent error expected", user.agentErrorCode)
            val trace = requireNotNull(user.execution) { "Completed execution process did not synchronize" }
            assertEquals("completed", trace.state)
            assertTrue("Running execution process was not received", runningTraceSeen)
            assertTrue("Actual tool observation must be in the persisted timeline",
                trace.events.any { it.label == "检查磁盘占用" && it.status == "completed" })
            assertTrue(trace.durationMs > 0 && trace.events.all { it.elapsedMs in 0..trace.durationMs })
            completedTraceSteps = trace.events.size
            val reply = repository.state.value.messages.firstOrNull { it.role == "assistant" && it.replyTo == user.id }
            assertNotNull("Actual planner reply did not synchronize", reply)
            replyLength = reply!!.content.length
            assertTrue("Disk reply must contain meaningful content", replyLength > 30 &&
                listOf("C", "盘", "空间", "GB", "GiB").any { it in reply.content })
            assertTrue("Phone must receive pending activity", activities.isNotEmpty())
            assertTrue("Activity must be short fixed labels", activities.all {
                it.length < 50 && !it.contains("\\") && !it.contains("\n") && !it.contains("http")
            })
            steps += "actual_planner_reply_and_safe_activity_synced_back_to_phone"
            steps += "running_and_completed_safe_process_timeline_persisted_from_native_sync"

            inspection = command("inspect")
            assertTrue(inspection.getBoolean("readonly_real_agent_qa"))
            assertEquals(1, inspection.getInt("executions"))
            assertEquals(1, inspection.getInt("completed"))
            assertEquals(0, inspection.getInt("errors"))
            assertEquals(1, inspection.getInt("desktop_agent_send_operations"))
            assertTrue("Actual C-drive observation missing", inspection.getBoolean("actual_c_drive_verified"))
            assertTrue(inspection.getInt("disk_observations") >= 1)
            assertTrue(inspection.getBoolean("last_reply_meaningful"))
            assertTrue(inspection.getBoolean("safe_disk_activity_observed"))
            assertFalse(inspection.getBoolean("unexpected_action_requested"))
            assertFalse(inspection.getBoolean("cleanup_performed"))
            assertFalse(inspection.getBoolean("real_api_key_exported"))
            assertFalse(inspection.getBoolean("chat_config_advertised"))
            assertTrue(inspection.getBoolean("production_settings_unchanged"))
            assertEquals(0, inspection.getInt("real_desktop_captures"))
            assertEquals(0, inspection.getInt("real_desktop_actions"))
            assertEquals(0, inspection.getInt("desktop_completion_calls"))
            assertEquals(0, inspection.getInt("mock_chat_requests"))
            assertFalse(repository.state.value.directApi.available)
            steps += "server_verified_actual_readonly_observation_and_zero_gui_write_or_mock_model_calls"
            passed = true
        } finally {
            File(context.filesDir, "aura-readonly-agent-e2e-result.json").writeText(JSONObject()
                .put("passed", passed).put("steps", JSONArray(steps))
                .put("real_encrypted_desktop_rpc", true).put("real_configured_planner", true)
                .put("actual_native_c_drive_observation", inspection.optBoolean("actual_c_drive_verified"))
                .put("reply_characters", replyLength).put("safe_activity_count", activities.size)
                .put("running_trace_received", runningTraceSeen).put("completed_trace_steps", completedTraceSteps)
                .put("duration_ms", (System.nanoTime() - started) / 1_000_000)
                .put("cross_network_tested", false).put("inspection", inspection).toString(2))
            repository.close(); vault.clear(); context.deleteDatabase("$name.db")
        }
    }

    private suspend fun command(action: String): JSONObject {
        val id = UUID.randomUUID().toString()
        val tmp = File(context.filesDir, "aura-e2e-command.tmp")
        tmp.writeText(JSONObject().put("id", id).put("action", action).put("params", JSONObject()).toString())
        check(tmp.renameTo(File(context.filesDir, "aura-e2e-command.json")))
        return withTimeout(30_000) {
            var result: JSONObject? = null
            while (result == null) {
                val ack = runCatching { JSONObject(File(context.filesDir, "aura-e2e-ack.json").readText()) }.getOrNull()
                if (ack?.optString("id") == id) {
                    assertTrue("Isolated fixture inspect failed", ack.optBoolean("ok"))
                    result = ack.getJSONObject("result")
                } else delay(200)
            }
            checkNotNull(result)
        }
    }
}
