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

/**
 * Opt-in real Android/JNI -> Python/Qt/iroh test, with a MOCK chat model only.
 * tools/android_desktop_fixture.py delivers its temporary pairing URI directly
 * into app-private files. No ticket, code or key is an instrumentation argument.
 */
@RunWith(AndroidJUnit4::class)
class AndroidDesktopE2eTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test(timeout = 300_000)
    fun realPairSnapshotOfflineQueueReplyImageDeleteAndRevocation() = runBlocking {
        val configuration = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Start the isolated Python fixture and inject private config first", configuration.isFile)
        val input = JSONObject(configuration.readText())
        require(input.optBoolean("isolated_fixture")) { "Only isolated mock-model fixtures are permitted" }
        val databaseName = "android-e2e-${UUID.randomUUID()}.db"
        val database = CompanionDatabase(context, databaseName)
        val repository = AppRepository(context, IrohRpcClient(context), database)
        val steps = mutableListOf<String>()
        var selectedPath = "unknown"
        var passed = false
        try {
            repository.pair(input.getString("pairing_uri"), "Android emulator isolated QA")
            assertEquals("Pairing failed: ${repository.state.value.error}", ConnectionState.CONNECTED,
                repository.state.value.connectionState)
            await("real encrypted pairing") { repository.state.value.connectionState == ConnectionState.CONNECTED &&
                repository.state.value.syncState == SyncState.READY }
            selectedPath = repository.state.value.connectionType
            assertTrue(repository.state.value.conversations.any { it.id == input.getString("seed_conversation_id") })
            repository.selectConversation(input.getString("seed_conversation_id"))
            assertTrue(repository.state.value.messages.any { it.content == "ANDROID_DESKTOP_SEED" })
            steps += "real_pair_and_snapshot"

            val conversation = repository.newConversation()
            repository.sendMessage(conversation, "ANDROID_E2E_ONLINE")
            await("first mocked AI reply", repository) { repository.state.value.messages.any { it.content == "Android E2E reply 1" } }
            steps += "android_to_desktop_mock_ai_reply"

            repository.disconnect()
            repository.sendMessage(conversation, "ANDROID_E2E_OFFLINE")
            val queued = repository.state.value.outbox.single { it.text == "ANDROID_E2E_OFFLINE" }
            assertEquals("queued", queued.status)
            repository.reconnect()
            await("offline queue reply", repository) { repository.state.value.messages.any { it.content == "Android E2E reply 2" } }
            assertEquals(queued.requestId, repository.state.value.outbox.single { it.text == "ANDROID_E2E_OFFLINE" }.requestId)
            steps += "durable_offline_queue_reconnect"

            val bitmap = Bitmap.createBitmap(32, 24, Bitmap.Config.ARGB_8888)
            bitmap.eraseColor(0xff75b8f4.toInt())
            val imageBytes = ByteArrayOutputStream().use { out -> bitmap.compress(Bitmap.CompressFormat.PNG, 100, out); out.toByteArray() }
            bitmap.recycle()
            repository.sendImage(imageBytes, "android-e2e.png", "image/png")
            await("image uploaded", repository) { repository.state.value.outbox.any { it.method == "image.upload" && it.status == "sent" } }
            val image = repository.state.value.images.first { it.filename == "android-e2e.png" }
            // Force image.get over the real encrypted connection rather than
            // accidentally testing only the already-cached upload source.
            image.localPath?.let { File(it).delete() }
            val downloaded = repository.downloadImage(image.id)
            assertNotNull(downloaded)
            assertArrayEquals(imageBytes, downloaded!!.readBytes())
            repository.petCommand("expression", "happy")
            val inspection = command("inspect")
            assertEquals(2, inspection.getInt("mock_chat_requests"))
            assertEquals("happy", inspection.getString("expression"))
            steps += "real_image_upload_download_without_ai"
            steps += "real_pet_command"

            repository.disconnect()
            repository.sendMessage(conversation, "ANDROID_E2E_MUST_NOT_REVIVE")
            command("delete_conversation", JSONObject().put("conversation_id", conversation))
            repository.reconnect()
            await("desktop deletion propagated", repository) {
                repository.state.value.conversations.none { it.id == conversation } && repository.state.value.outbox.any {
                    it.text == "ANDROID_E2E_MUST_NOT_REVIVE" && it.status == "needs_attention"
                }
            }
            assertEquals(2, command("inspect").getInt("mock_chat_requests"))
            steps += "deleted_conversation_does_not_replay_queue"

            repository.deleteImage(image.id)
            await("image deletion", repository) { repository.state.value.images.none { it.id == image.id } }
            steps += "image_delete"

            command("revoke_devices")
            repository.syncNow()
            assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            repository.reconnect()
            assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            assertEquals(0, command("inspect").getInt("authorized_devices"))
            steps += "live_revocation_blocks_automatic_repair"
            passed = true
        } finally {
            val report = JSONObject().put("passed", passed).put("steps", JSONArray(steps))
                .put("selected_path", selectedPath).put("mock_chat_model", true).put("emulator_app_tested", true)
                .put("phone_hardware_tested", false).put("cross_network_tested", false)
                .put("public_relay_path_observed", selectedPath == "relay")
            File(context.filesDir, "aura-e2e-result.json").writeText(report.toString(2))
            configuration.delete() // Remove the consumed temporary authorization code.
            repository.close()
            context.deleteDatabase(databaseName)
        }
    }

    private suspend fun await(label: String, repository: AppRepository? = null, condition: () -> Boolean) {
        withTimeout(90_000) {
            while (!condition()) {
                if (repository != null) {
                    assertTrue("$label: encrypted connection lost", repository.state.value.connectionState == ConnectionState.CONNECTED)
                    repository.syncNow()
                }
                delay(2000)
            }
        }
    }

    /** The host polls this private file via adb run-as; it is not production RPC. */
    private suspend fun command(action: String, params: JSONObject = JSONObject()): JSONObject {
        val id = UUID.randomUUID().toString()
        val payload = JSONObject().put("id", id).put("action", action).put("params", params)
        val temporary = File(context.filesDir, "aura-e2e-command.tmp")
        temporary.writeText(payload.toString())
        check(temporary.renameTo(File(context.filesDir, "aura-e2e-command.json")))
        return withTimeout(30_000) {
            var result: JSONObject? = null
            while (result == null) {
                val ack = runCatching { JSONObject(File(context.filesDir, "aura-e2e-ack.json").readText()) }.getOrNull()
                if (ack?.optString("id") == id) {
                    assertTrue("Isolated desktop control failed", ack.optBoolean("ok"))
                    result = ack.getJSONObject("result")
                } else delay(300)
            }
            checkNotNull(result)
        }
    }
}
