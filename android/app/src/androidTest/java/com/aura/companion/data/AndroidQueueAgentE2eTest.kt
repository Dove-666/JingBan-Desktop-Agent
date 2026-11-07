package com.aura.companion.data

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aura.companion.transport.IrohRpcClient
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

/** Real native QUIC and real isolated host SDK; only a loopback scripted model. */
@RunWith(AndroidJUnit4::class)
class AndroidQueueAgentE2eTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test(timeout = 180_000)
    fun busyHostDurablyQueuesTwoFreshPhoneConversationsWithoutRepeatingEitherTask() = runBlocking {
        val configuration = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Start the independent native queue host fixture", configuration.isFile)
        val input = JSONObject(configuration.readText())
        assumeTrue(input.optBoolean("isolated_fixture") && input.optBoolean("agent_queue_qa") &&
            input.optBoolean("actual_sdk") && input.optBoolean("mock_chat_model") && !input.optBoolean("direct_api"))
        assertFalse(input.has("manual_api"))
        val name = "native-agent-queue-${UUID.randomUUID()}"
        val database = CompanionDatabase(context, "$name.db")
        val vault = EncryptedApiConfigVault(context, "$name.enc")
        val rpc = IrohRpcClient(context)
        val repository = AppRepository(context, rpc, database, vault)
        var passed = false
        var inspection = JSONObject()
        var duplicateStable = false
        var path = "unknown"
        val started = System.nanoTime()
        try {
            repository.pair(input.getString("pairing_uri"), "Owned native SDK queue QA")
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            assertTrue(repository.state.value.desktopTaskAvailable)
            assertFalse(repository.state.value.directApi.available)
            path = repository.state.value.connectionType
            assertEquals("The fixture must use real native direct QUIC", "direct", path)

            val firstCid = repository.newConversation()
            val aTask = input.getString("a_task")
            val bTask = input.getString("b_task")
            assertTrue("First fresh phone CID was not admitted", repository.sendDesktopMessage(firstCid, aTask))
            val aOperation = database.outbox().single { it.text == aTask }
            assertEquals("sent", aOperation.status)
            withTimeout(30_000) {
                while (!command("inspect").optBoolean("first_planner_entered")) delay(150)
            }
            val status = checked(rpc.request("status"))
            assertTrue(status.getBoolean("agent_available"))
            assertTrue(status.getBoolean("agent_busy"))
            assertTrue((0 until status.getJSONArray("capabilities").length()).any {
                status.getJSONArray("capabilities").optString(it) == "agent.queue.v1"
            })

            val secondCid = repository.newConversation()
            assertNotEquals(firstCid, secondCid)
            assertTrue("Busy host did not admit the second fresh phone CID", repository.sendDesktopMessage(secondCid, bTask))
            val bOperation = database.outbox().single { it.text == bTask }
            assertEquals("sent", bOperation.status)
            assertNotEquals(aOperation.requestId, bOperation.requestId)
            assertFalse(bOperation.canSendOnPhone)
            inspection = command("inspect")
            assertFalse(inspection.getBoolean("released"))
            assertEquals(1, inspection.getInt("planner_calls"))
            assertEquals(2, inspection.getInt("admission_count"))
            assertEquals("running", inspection.getJSONArray("jobs").getJSONObject(0).getString("state"))
            assertEquals("queued", inspection.getJSONArray("jobs").getJSONObject(1).getString("state"))

            val repeat = checked(rpc.request("agent.send", JSONObject().put("conversation_id", secondCid).put("text", bTask), bOperation.requestId))
            assertTrue(repeat.getBoolean("duplicate"))
            assertEquals(bOperation.messageId, repeat.getString("message_id"))
            assertEquals("queued", repeat.getString("queue_state"))
            assertEquals(2, command("inspect").getInt("unique_jobs"))
            duplicateStable = true
            command("release")

            withTimeout(90_000) {
                while (true) {
                    repository.poll()
                    inspection = command("inspect")
                    val jobs = inspection.getJSONArray("jobs")
                    if (jobs.length() == 2 && (0 until jobs.length()).all {
                        jobs.getJSONObject(it).optString("state") == "completed" && jobs.getJSONObject(it).optBoolean("worker_closed")
                    }) break
                    delay(200)
                }
            }
            repository.selectConversation(firstCid); repository.syncNow()
            val firstUser = repository.state.value.messages.single { it.role == "user" && it.content == aTask }
            assertEquals("complete", firstUser.requestStatus)
            assertEquals(input.getString("a_reply"), repository.state.value.messages.single {
                it.role == "assistant" && it.replyTo == firstUser.id }.content)
            repository.selectConversation(secondCid); repository.syncNow()
            val secondUser = repository.state.value.messages.single { it.role == "user" && it.content == bTask }
            assertEquals("complete", secondUser.requestStatus)
            assertEquals(input.getString("b_reply"), repository.state.value.messages.single {
                it.role == "assistant" && it.replyTo == secondUser.id }.content)
            assertEquals(2, inspection.getInt("admission_count"))
            assertEquals(2, inspection.getInt("unique_jobs"))
            assertEquals(8, inspection.getInt("planner_calls"))
            assertEquals(0, inspection.getInt("fixture_error_count"))
            assertEquals(4, inspection.getInt("a_context_checks"))
            assertTrue(inspection.getBoolean("a_context_clean"))
            assertTrue(inspection.getBoolean("actual_a_bytes"))
            assertTrue(inspection.getBoolean("actual_b_bytes"))
            assertTrue((0 until 2).all { inspection.getJSONArray("jobs").getJSONObject(it).getBoolean("saved_reply") })
            assertEquals(2, database.outbox().count { it.method == "agent.send" })
            passed = true
        } finally {
            // Release a blocked owned fixture even if an Android assertion fails.
            runCatching { command("release") }
            File(context.filesDir, "aura-queue-e2e-result.json").writeText(JSONObject()
                .put("passed", passed).put("actual_native_quic", true).put("actual_sdk", true)
                .put("model", "synthetic_loopback").put("path", path).put("duplicate_stable", duplicateStable)
                .put("cross_network_tested", false).put("physical_phone_tested", false)
                .put("duration_ms", (System.nanoTime() - started) / 1_000_000).put("inspection", inspection).toString(2))
            repository.close(); vault.clear(); context.deleteDatabase("$name.db")
        }
    }

    private fun checked(response: JSONObject): JSONObject {
        assertTrue("Native RPC failed", response.optBoolean("ok"))
        return response.getJSONObject("result")
    }

    private suspend fun command(action: String): JSONObject {
        val id = UUID.randomUUID().toString()
        val temporary = File(context.filesDir, "aura-e2e-command.tmp")
        temporary.writeText(JSONObject().put("id", id).put("action", action).put("params", JSONObject()).toString())
        check(temporary.renameTo(File(context.filesDir, "aura-e2e-command.json")))
        return withTimeout(15_000) {
            var result: JSONObject? = null
            while (result == null) {
                val ack = runCatching { JSONObject(File(context.filesDir, "aura-e2e-ack.json").readText()) }.getOrNull()
                if (ack?.optString("id") == id) {
                    assertTrue("Owned host control failed", ack.optBoolean("ok"))
                    result = ack.getJSONObject("result")
                } else delay(100)
            }
            checkNotNull(result)
        }
    }
}
