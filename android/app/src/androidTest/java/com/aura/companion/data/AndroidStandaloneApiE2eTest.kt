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

/** Real HTTP and desktop RPC. Only private synthetic mock credentials are accepted. */
@RunWith(AndroidJUnit4::class)
class AndroidStandaloneApiE2eTest {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test(timeout = 300_000)
    fun unpairedImageRestartPairAdoptionAndExplicitLegacyMigrationNeverRepeatAi() = runBlocking {
        val privateFile = File(context.filesDir, "aura-e2e-config.json")
        assumeTrue("Start --direct-api mock fixture and renew private pairing", privateFile.isFile)
        val input = JSONObject(privateFile.readText())
        assumeTrue("Synthetic direct API fixture only", input.optBoolean("isolated_fixture") && input.optBoolean("mock_chat_model") &&
            input.optBoolean("direct_api") && !input.optBoolean("use_existing_api") && input.has("manual_api"))
        val api = input.getJSONObject("manual_api")
        require(api.getString("api_key").startsWith("ISOLATED_ANDROID_")) { "Only synthetic mock credentials are allowed" }
        require(api.getString("base_url").startsWith("http://10.0.2.2:"))
        val name = "standalone-e2e-${UUID.randomUUID()}"
        val vault = EncryptedApiConfigVault(context, "$name.enc")
        var database = CompanionDatabase(context, "$name.db")
        var repository = AppRepository(context, IrohRpcClient(context), database, vault)
        val steps = mutableListOf<String>()
        var passed = false
        var inspection = JSONObject()
        val start = command("inspect")
        val initialHttp = start.getInt("mock_chat_requests")
        val initialImages = start.getInt("image_requests")
        val initialSends = start.getInt("desktop_chat_send_operations")
        val initialImports = start.getInt("desktop_import_operations")
        try {
            assertTrue(repository.saveDirectApiConfig(api.getString("base_url"), api.getString("model"), api.getString("api_key"),
                api.getInt("timeout_seconds"), api.getString("reasoning_effort")))
            assertNull(repository.state.value.device)
            assertTrue(repository.state.value.directApi.available)
            assertEquals(ApiConfigSource.LOCAL, repository.state.value.directApi.source)
            steps += "unpaired_manual_api_saved_in_keystore_ciphertext"

            val bitmap = Bitmap.createBitmap(32, 24, Bitmap.Config.ARGB_8888).apply { eraseColor(0xff74ace7.toInt()) }
            val bytes = ByteArrayOutputStream().use { out -> bitmap.compress(Bitmap.CompressFormat.PNG, 100, out); out.toByteArray() }
            bitmap.recycle()
            val imageId = ImageCache.digest(bytes)
            val cid = repository.newConversation()
            repository.stageImage(cid, bytes, "standalone-public.png", "image/png")
            assertEquals(initialHttp, command("inspect").getInt("mock_chat_requests"))
            repository.sendMessage(cid, "ANDROID_STANDALONE_IMAGE")
            awaitReply(repository, initialHttp + 1)
            assertEquals(imageId, repository.state.value.messages.single { it.content == "ANDROID_STANDALONE_IMAGE" }.attachmentId)
            assertTrue(database.directTurns().all { it.authorityId == LOCAL_PHONE_AUTHORITY })
            steps += "unpaired_phone_http_image_reply_without_computer_connection"

            val firstIds = repository.state.value.messages.map { it.id }
            repository.close()
            database = CompanionDatabase(context, "$name.db")
            repository = AppRepository(context, IrohRpcClient(context), database, vault)
            repository.selectConversation(cid)
            assertTrue(repository.state.value.directApi.available)
            assertEquals(ApiConfigSource.LOCAL, repository.state.value.directApi.source)
            assertEquals(firstIds, repository.state.value.messages.map { it.id })
            assertNull(repository.state.value.device)
            assertTrue(repository.saveDirectApiConfig(api.getString("base_url"), api.getString("model"), "", 30, "default"))
            steps += "offline_db_and_encrypted_config_restart_blank_key_keeps_saved_key"

            repository.sendMessage(cid, "上面那张图片 ANDROID_STANDALONE_FOLLOWUP")
            awaitReply(repository, initialHttp + 2)
            val follow = command("inspect")
            assertEquals(initialImages + 2, follow.getInt("image_requests"))
            assertFalse(follow.getBoolean("last_current_user_has_image"))
            assertTrue(follow.getBoolean("last_history_has_image"))
            assertNull(repository.state.value.messages.single { it.content.contains("ANDROID_STANDALONE_FOLLOWUP") }.attachmentId)
            assertEquals(imageId, follow.getString("last_image_sha256")); assertTrue(follow.getBoolean("image_payloads_valid"))
            assertEquals(initialImports, follow.getInt("desktop_import_operations"))
            assertEquals(initialSends, follow.getInt("desktop_chat_send_operations"))
            steps += "unpaired_same_conversation_image_followup_uses_original_bytes"

            val idsBeforePair = repository.state.value.messages.map { it.id }
            repository.pair(input.getString("pairing_uri"), "Isolated standalone Android QA")
            assertEquals("Pair failed safely: ${repository.state.value.error}", ConnectionState.CONNECTED, repository.state.value.connectionState)
            awaitImports(repository)
            repository.selectConversation(cid)
            assertEquals(idsBeforePair, repository.state.value.messages.map { it.id })
            assertEquals(ApiConfigSource.LOCAL, repository.state.value.directApi.source)
            val imported = command("inspect")
            assertEquals(initialHttp + 2, imported.getInt("mock_chat_requests"))
            assertEquals(initialImports + 2, imported.getInt("desktop_import_operations"))
            assertEquals(initialSends, imported.getInt("desktop_chat_send_operations"))
            steps += "explicit_pair_adopts_only_local_history_with_stable_ids_and_no_model_replay"

            repository.disconnect()
            repository.stageImage(cid, bytes, "legacy-queued-public.png", "image/png")
            // An isolated fixture for an existing queue from an older version.
            val queuedId = database.queueMessage(cid, "ANDROID_STANDALONE_MIGRATED_IMAGE")
            repository.selectConversation(cid)
            assertTrue(database.operation(queuedId)!!.item.canSendOnPhone)
            repository.sendQueuedOnPhone(queuedId)
            awaitReply(repository, initialHttp + 3)
            assertEquals("discarded", database.operation(queuedId)!!.item.status)
            val migrated = repository.state.value.messages.single { it.content == "ANDROID_STANDALONE_MIGRATED_IMAGE" }
            assertEquals(queuedId, migrated.id); assertEquals(imageId, migrated.attachmentId)
            repository.sendQueuedOnPhone(queuedId)
            assertEquals(initialHttp + 3, command("inspect").getInt("mock_chat_requests"))
            steps += "explicit_unattempted_queue_migration_preserves_user_id_and_image_once"

            repository.reconnect(); awaitImports(repository)
            val synced = command("inspect")
            assertEquals(initialImports + 3, synced.getInt("desktop_import_operations"))
            assertEquals(initialSends, synced.getInt("desktop_chat_send_operations"))
            assertEquals(initialHttp + 3, synced.getInt("mock_chat_requests"))
            assertEquals(initialImages + 3, synced.getInt("image_requests"))
            assertEquals(imageId, synced.getString("last_image_sha256"))
            steps += "migrated_reply_imports_without_dispatching_old_computer_chat"

            command("revoke_devices"); repository.poll()
            assertEquals(ConnectionState.UNAUTHORIZED, repository.state.value.connectionState)
            assertTrue(repository.state.value.directApi.available)
            assertEquals(ApiConfigSource.LOCAL, repository.state.value.directApi.source)
            assertNotNull(vault.load()); assertNull(vault.load()!!.desktopCache)
            steps += "desktop_revocation_keeps_independent_phone_credentials"

            val unsafeCid = repository.newConversation()
            val unsafeId = database.queueMessage(unsafeCid, "ANDROID_STANDALONE_UNKNOWN_NO_REPLAY")
            assertTrue(database.claimOutboxForSend(unsafeId, UUID.randomUUID().toString()))
            database.updateOutbox(unsafeId, "unknown")
            assertFalse(database.operation(unsafeId)!!.item.canSendOnPhone)
            repository.sendQueuedOnPhone(unsafeId)
            assertEquals(initialHttp + 3, command("inspect").getInt("mock_chat_requests"))
            steps += "possibly_paid_unknown_request_cannot_be_moved_to_another_model"

            val independentCid = repository.newConversation()
            repository.sendMessage(independentCid, "ANDROID_STANDALONE_AFTER_PC_REVOCATION")
            awaitReply(repository, initialHttp + 4)
            assertEquals(LOCAL_PHONE_AUTHORITY, database.directTurns().single { it.text == "ANDROID_STANDALONE_AFTER_PC_REVOCATION" }.authorityId)
            steps += "phone_model_still_works_after_pc_revocation_without_auto_pairing"
            inspection = command("inspect")
            assertEquals(initialHttp + 4, inspection.getInt("mock_chat_requests"))
            assertEquals(initialImages + 3, inspection.getInt("image_requests"))
            assertEquals(initialSends, inspection.getInt("desktop_chat_send_operations"))
            assertEquals(initialImports + 3, inspection.getInt("desktop_import_operations"))
            assertEquals(0, inspection.getInt("authorized_devices"))
            passed = true
        } finally {
            File(context.filesDir, "aura-standalone-api-e2e-result.json").writeText(JSONObject().put("passed", passed)
                .put("steps", JSONArray(steps)).put("mock_chat_model", true).put("real_android_http", true)
                .put("real_encrypted_rpc", true).put("hardware_phone_tested", false).put("cross_network_tested", false)
                .put("http_request_delta", inspection.optInt("mock_chat_requests", initialHttp) - initialHttp)
                .put("image_request_delta", inspection.optInt("image_requests", initialImages) - initialImages)
                .put("desktop_chat_send_delta", inspection.optInt("desktop_chat_send_operations", initialSends) - initialSends)
                .put("desktop_import_delta", inspection.optInt("desktop_import_operations", initialImports) - initialImports)
                .put("inspection", inspection).toString(2))
            privateFile.delete(); repository.close(); vault.clear(); context.deleteDatabase("$name.db")
        }
    }

    private suspend fun awaitReply(repository: AppRepository, number: Int) = withTimeout(60000) {
        while (repository.state.value.directChatBusy || repository.state.value.messages.none { it.content == "Android E2E reply $number" }) delay(150)
    }

    private suspend fun awaitImports(repository: AppRepository) = withTimeout(90000) {
        while (repository.state.value.historyPendingCount != 0) {
            assertEquals(ConnectionState.CONNECTED, repository.state.value.connectionState)
            repository.poll(); delay(300)
        }
    }

    private suspend fun command(action: String): JSONObject {
        val id = UUID.randomUUID().toString(); val temporary = File(context.filesDir, "aura-e2e-command.tmp")
        temporary.writeText(JSONObject().put("id", id).put("action", action).put("params", JSONObject()).toString())
        check(temporary.renameTo(File(context.filesDir, "aura-e2e-command.json")))
        return withTimeout(30000) {
            var result: JSONObject? = null
            while (result == null) {
                val ack = runCatching { JSONObject(File(context.filesDir, "aura-e2e-ack.json").readText()) }.getOrNull()
                if (ack?.optString("id") == id) { assertTrue("Isolated fixture control failed", ack.optBoolean("ok")); result = ack.getJSONObject("result") }
                else delay(200)
            }
            requireNotNull(result)
        }
    }
}
