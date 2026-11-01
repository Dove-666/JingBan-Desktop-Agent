package com.aura.companion.data

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.IOException
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class AppRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()

    private class FakeRpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var chatCalls = 0
        var pairCalls = 0
        var connectCalls = 0
        var closeCalls = 0
        var statusUnauthorized = false
        var rejectPairing = false
        var failSync = false
        var loseChatResponse = false
        var loseUploadResponse = false
        var supportsImageChat = true
        var uploadCalls = 0
        var desktopBusy = false
        var resets = 0
        var deletedConversation: String? = null
        val operations = mutableMapOf<String, JSONObject>()
        val sentChatIds = mutableListOf<String>()
        val chatParams = mutableListOf<JSONObject>()
        val deliveryMethods = mutableListOf<String>()
        val uploadedParams = mutableListOf<JSONObject>()
        override suspend fun connect(ticket: String) { connectCalls++; require(ticket == "public-ticket") }
        override suspend fun close() { closeCalls++ }
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("id", requestId).put("ok", true).put("result", result)
            fun failure(code: String) = JSONObject().put("id", requestId).put("ok", false)
                .put("error", JSONObject().put("code", code).put("message", code))
            if (statusUnauthorized && method != "pair") return failure("unauthorized")
            return when (method) {
                "pair" -> { pairCalls++; if (rejectPairing) failure("pairing_failed") else ok(JSONObject().put("paired", true)) }
                "status" -> ok(JSONObject().put("protocol_version", 1).put("chat_busy", desktopBusy)
                    .put("capabilities", JSONArray(if (supportsImageChat) listOf("chat.image") else emptyList<String>())))
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("next_cursor", "").put("has_more", false))
                "sync.pull" -> {
                    if (failSync) return failure("sync_error")
                    if (resets-- > 0) return failure("sync_reset_required")
                    val deleted = deletedConversation
                    val items = JSONArray()
                    if (deleted != null && params.getLong("since") == 0L) items.put(JSONObject().put("entity", "conversation")
                        .put("id", deleted).put("conversation_id", deleted).put("seq", 1).put("deleted", true).put("data", JSONObject()))
                    ok(JSONObject().put("changes", items).put("next_since", if (items.length() > 0) 1 else params.getLong("since"))
                        .put("has_more", false))
                }
                "operation.get" -> ok(operations[params.getString("request_id")] ?: JSONObject().put("state", "not_found"))
                "chat.send" -> {
                    chatCalls++; sentChatIds.add(requestId)
                    chatParams.add(JSONObject(params.toString())); deliveryMethods.add(method)
                    operations[requestId] = JSONObject().put("state", "pending")
                    if (loseChatResponse) { loseChatResponse = false; throw IOException("response deliberately lost") }
                    ok(JSONObject().put("accepted", true).put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId)))
                }
                "image.upload" -> {
                    uploadCalls++; uploadedParams.add(JSONObject(params.toString())); deliveryMethods.add(method)
                    val bytes = Base64.decode(params.getString("data_base64"), Base64.DEFAULT)
                    check(ImageCache.digest(bytes) == params.getString("sha256"))
                    val image = JSONObject().put("id", params.getString("sha256")).put("filename", params.getString("filename"))
                        .put("mime_type", params.getString("mime_type")).put("size", bytes.size).put("width", 1).put("height", 1)
                        .put("created_at", "2026-10-31T01:00:00Z")
                    operations[requestId] = JSONObject().put("state", "complete").put("result", JSONObject().put("ok", true).put("result", image))
                    if (loseUploadResponse) { loseUploadResponse = false; throw IOException("upload response deliberately lost") }
                    ok(image)
                }
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                else -> error("Unexpected method $method")
            }
        }
    }

    private fun seededDb(): CompanionDatabase = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("电脑", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }

    @Test fun offlineChatSurvivesReconnectAndIsSentWithOneStableRequestId() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.sendLegacyMessage(cid, "离线内容")
            val queuedId = db.outbox().single().requestId
            assertEquals(0, rpc.chatCalls)
            assertEquals("queued", db.outbox().single().status)
            repo.reconnect()
            assertEquals(listOf(queuedId), rpc.sentChatIds)
            assertEquals("sent", db.outbox().single().status)
            assertEquals(AppRepository.stableMessageId("phone", queuedId), db.outbox().single().messageId)
            repo.syncNow(); repo.reconnect()
            assertEquals(1, rpc.chatCalls)
        } finally { repo.close() }
    }

    @Test fun lostResponseQueriesPendingOperationAndNeverAutomaticallyRepeatsAi() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { loseChatResponse = true }
        val repo = AppRepository(context, rpc, db)
        try {
            repo.sendLegacyMessage(repo.newConversation(), "只生成一次")
            repo.reconnect()
            assertEquals("unknown", db.outbox().single().status)
            assertEquals(ConnectionState.ERROR, repo.state.value.connectionState)
            repo.reconnect()
            assertEquals(1, rpc.chatCalls)
            assertEquals("pending", db.outbox().single().status)
            repo.syncNow(); repo.retryOutbox(db.outbox().single().requestId)
            assertEquals(1, rpc.chatCalls)
            assertEquals(1, db.outbox().size)
        } finally { repo.close() }
    }

    @Test fun deletedConversationQueueIsRetainedForUserInsteadOfRevived() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.sendLegacyMessage(cid, "电脑删除后不能自动复活")
            rpc.deletedConversation = cid
            repo.reconnect()
            assertEquals(0, rpc.chatCalls)
            assertEquals("needs_attention", db.outbox().single().status)
            assertEquals("电脑删除后不能自动复活", db.outbox().single().text)
        } finally { repo.close() }
    }

    @Test fun authorizationRevocationBlocksAutomaticReconnectAndPairing() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { statusUnauthorized = true }
        val repo = AppRepository(context, rpc, db)
        try {
            repo.sendLegacyMessage(repo.newConversation(), "授权撤销时保留")
            repo.reconnect(); repo.reconnect(); repo.syncNow()
            assertEquals(ConnectionState.UNAUTHORIZED, repo.state.value.connectionState)
            assertEquals(0, rpc.pairCalls)
            assertEquals(0, rpc.chatCalls)
            assertEquals("queued", db.outbox().single().status)
        } finally { repo.close() }
    }

    @Test fun busyDesktopKeepsQueuedChatWithoutCreatingFailedDuplicateOperations() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { desktopBusy = true }
        val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.sendLegacyMessage(cid, "first"); repo.sendLegacyMessage(cid, "second")
            repo.reconnect(); repo.syncNow()
            assertEquals(0, rpc.chatCalls)
            assertTrue(db.outbox().all { it.status == "queued" })
            rpc.desktopBusy = false
            repo.syncNow()
            assertEquals(1, rpc.chatCalls)
            assertEquals(1, db.outbox().count { it.status == "queued" })
        } finally { repo.close() }
    }

    @Test fun syncResetObtainsFreshSnapshotAndPreservesUnsentDraft() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { resets = 1; desktopBusy = true }
        val repo = AppRepository(context, rpc, db)
        try {
            repo.sendLegacyMessage(repo.newConversation(), "重建缓存后仍保留")
            repo.reconnect()
            assertEquals(SyncState.READY, repo.state.value.syncState)
            assertEquals("queued", db.outbox().single().status)
            assertEquals("重建缓存后仍保留", repo.state.value.messages.single().content)
        } finally { repo.close() }
    }

    @Test fun nestedPairingUriWorksWithoutPersistingTemporaryCode() = runBlocking {
        val db = CompanionDatabase(context, null); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val temporary = "TEMPORARY_PAIR_SECRET"
            val payload = JSONObject().put("ticket", "public-ticket").put("endpoint_id", "desktop")
                .put("version", 1).put("code", temporary)
            val uri = "aura://pair/" + Base64.encodeToString(payload.toString().toByteArray(), Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
            repo.pair(JSONObject().put("pairing", JSONObject().put("uri", uri)).toString(), "测试手机")
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals("public-ticket", db.device()!!.ticket)
            val persisted = db.readableDatabase.rawQuery("SELECT value FROM settings", null).use {
                buildString { while (it.moveToNext()) append(it.getString(0)) }
            }
            assertFalse(persisted.contains(temporary)); assertFalse(persisted.contains("aura://pair/"))
        } finally { repo.close() }
    }

    @Test fun stableUuidMatchesDesktopPythonUuid5() {
        assertEquals("a73947cc-c9dc-5d9a-9a1f-2e31586fd55b",
            AppRepository.stableMessageId("phone", "91e374fc-5c45-44e0-b4c3-bf03b7c05470"))
    }

    @Test fun failedPairLeavesConnectingStateButKeepsPreviouslySavedComputer() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { rejectPairing = true }; val repo = AppRepository(context, rpc, db)
        try {
            repo.pair(JSONObject().put("ticket", "public-ticket").put("code", "expired-code")
                .put("endpoint_id", "another-desktop").toString(), "测试手机")
            assertEquals(ConnectionState.ERROR, repo.state.value.connectionState)
            assertEquals("desktop", db.device()!!.endpointId)
            assertEquals("pairing_failed", repo.state.value.error)
        } finally { repo.close() }
    }

    @Test fun businessSyncErrorDoesNotDisconnectValidEncryptedConnection() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { failSync = true }; val repo = AppRepository(context, rpc, db)
        try {
            repo.reconnect()
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.ERROR, repo.state.value.syncState)
        } finally { repo.close() }
    }

    @Test fun invalidPairingNeverConnectsOrClosesAnExistingSessionAndPreservesCacheAndQueue() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { desktopBusy = true }; val repo = AppRepository(context, rpc, db)
        try {
            repo.reconnect()
            val cid = repo.newConversation()
            repo.sendLegacyMessage(cid, "原连接的待发内容")
            val device = repo.state.value.device
            val queued = db.outbox().single()
            val cursor = db.cursor()
            val connections = rpc.connectCalls
            val closes = rpc.closeCalls
            val invalid = listOf("Q".repeat(24), "{broken-json", "aura://pair/%%%",
                JSONObject().put("code", "temporary-code").toString(),
                JSONObject().put("ticket", "public-ticket").put("code", "temporary-code").put("version", 2).toString(),
                JSONObject().put("ticket", "public-ticket").put("code", "temporary-code").put("expires_at", 1).toString(),
                "Q".repeat(32769))
            for (input in invalid) {
                repo.pair(input, "测试手机")
                assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
                assertEquals(SyncState.READY, repo.state.value.syncState)
                assertEquals(device, repo.state.value.device)
                assertEquals(cid, repo.state.value.selectedConversationId)
                assertEquals(cursor, db.cursor())
                assertEquals(queued, db.outbox().single())
                assertEquals(connections, rpc.connectCalls)
                assertEquals(closes, rpc.closeCalls)
                assertEquals(0, rpc.pairCalls)
                assertNotNull(repo.state.value.error)
                assertFalse(repo.state.value.error.orEmpty().contains("temporary-code"))
            }
        } finally { repo.close() }
    }

    @Test fun malformedPairingKeepsDisconnectedStateAndExplainsFullCopy() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            repo.sendLegacyMessage(repo.newConversation(), "离线草稿")
            repo.pair("Q".repeat(24), "测试手机")
            assertEquals(ConnectionState.DISCONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.IDLE, repo.state.value.syncState)
            assertTrue(repo.state.value.error.orEmpty().contains("复制配对信息"))
            assertEquals(0, rpc.connectCalls); assertEquals(0, rpc.closeCalls)
            repo.pair("{\"ticket\":\"NEVER_ECHO_THIS\"", "测试手机")
            assertEquals(ConnectionState.DISCONNECTED, repo.state.value.connectionState)
            assertFalse(repo.state.value.error.orEmpty().contains("NEVER_ECHO_THIS"))
            assertEquals("queued", db.outbox().single().status)
            assertEquals(0, rpc.connectCalls); assertEquals(0, rpc.closeCalls)
        } finally { repo.close() }
    }

    @Test fun completeNestedJsonReachesTransportAndSynchronizesNormally() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val info = JSONObject().put("ticket", "public-ticket").put("code", "temporary-code")
                .put("version", 1).put("endpoint_id", "desktop").put("expires_at", System.currentTimeMillis() / 1000.0 + 300)
            repo.pair(JSONObject().put("pairing", info).toString(), "测试手机")
            assertEquals(1, rpc.connectCalls); assertEquals(1, rpc.pairCalls)
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.READY, repo.state.value.syncState)
            assertNull(repo.state.value.error)
        } finally { repo.close() }
    }

    private fun tinyPng(): ByteArray = Base64.decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==", Base64.DEFAULT)

    @Test fun selectingImagesOnlyStagesEachConversationAndNeverSendsOrChangesSelection() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            repo.reconnect()
            val first = repo.newConversation(); val second = repo.newConversation()
            repo.stageImage(first, tinyPng(), "first.png", "image/png")
            assertNull("Valid PNG staging must succeed", repo.state.value.error)
            assertEquals(second, repo.state.value.selectedConversationId)
            repo.stageImage(second, tinyPng(), "second.png", "image/png")
            assertEquals(setOf(first, second), repo.state.value.pendingImages.keys)
            assertEquals("first.png", repo.state.value.pendingImages[first]!!.filename)
            assertEquals("first.png", repo.state.value.pendingImages[first]!!.filename)
            assertEquals("second.png", repo.state.value.pendingImages[second]!!.filename)
            assertTrue(db.outbox().isEmpty())
            assertEquals(0, rpc.uploadCalls); assertEquals(0, rpc.chatCalls)
            repo.removePendingImage(first)
            assertEquals(setOf(second), repo.state.value.pendingImages.keys)
        } finally { repo.close() }
    }

    @Test fun offlineImageAndTextAreOneVisibleMessageWithDurableUploadDependency() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "photo.png", "image/png")
            val imageId = repo.state.value.pendingImages[cid]!!.attachmentId
            repo.sendLegacyMessage(cid, "  看看这是什么  ")
            assertTrue(repo.state.value.pendingImages.isEmpty())
            val chat = db.outbox().single { it.method == "chat.send" }
            val upload = db.outbox().single { it.method == "image.upload" }
            assertEquals(upload.requestId, db.operation(chat.requestId)!!.dependencyId)
            assertEquals(imageId, db.operation(chat.requestId)!!.params.getString("image_id"))
            assertEquals("看看这是什么", db.messages(cid).single().content)
            assertEquals(imageId, db.messages(cid).single().attachmentId)
            assertEquals(0, rpc.chatCalls); assertEquals(0, rpc.uploadCalls)
            repo.reconnect()
            assertEquals(listOf("image.upload", "chat.send"), rpc.deliveryMethods)
            assertEquals(imageId, rpc.chatParams.single().getString("image_id"))
            assertEquals(chat.requestId, rpc.sentChatIds.single())
            assertTrue(db.outbox().all { it.status == "sent" })
            assertEquals(1, db.messages(cid).size)
        } finally { repo.close() }
    }

    @Test fun pendingComposerAndImageQueueSurviveDatabaseAndRepositoryRestart() = runBlocking {
        val name = "image-restart-${UUID.randomUUID()}.db"
        val rpc = FakeRpc()
        var repo = AppRepository(context, rpc, CompanionDatabase(context, name))
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "persisted.png", "image/png")
            val expectedImage = repo.state.value.pendingImages[cid]!!.attachmentId
            repo.close()
            repo = AppRepository(context, rpc, CompanionDatabase(context, name))
            repo.syncNow() // A local refresh also waits for the asynchronous initial load.
            assertEquals(expectedImage, repo.state.value.pendingImages[cid]!!.attachmentId)
            repo.sendLegacyMessage(cid, "")
            val request = repo.database.outbox().single { it.method == "chat.send" }.requestId
            assertEquals("请描述这张图片。", repo.database.operation(request)!!.params.getString("text"))
            repo.database.saveDevice(DeviceInfo("电脑", "desktop", "public-ticket"), "phone")
            repo.close()
            repo = AppRepository(context, rpc, CompanionDatabase(context, name))
            repo.reconnect()
            assertEquals(listOf(request), rpc.sentChatIds)
            assertEquals(expectedImage, rpc.chatParams.single().getString("image_id"))
            assertEquals(listOf("image.upload", "chat.send"), rpc.deliveryMethods)
        } finally { repo.close(); context.deleteDatabase(name) }
    }

    @Test fun oldComputerCannotSilentlyReceiveOnlyTheTextOfAnImageMessage() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { supportsImageChat = false }
        val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "photo.png", "image/png")
            repo.sendLegacyMessage(cid, "解释这张图片")
            val request = db.outbox().single { it.method == "chat.send" }.requestId
            repo.reconnect()
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertEquals(SyncState.ERROR, repo.state.value.syncState)
            assertEquals(AppRepository.IMAGE_CHAT_UPDATE_REQUIRED, repo.state.value.error)
            assertEquals("queued", db.operation(request)!!.item.status)
            assertFalse(db.operation(request)!!.attempted)
            assertEquals(0, rpc.chatCalls); assertEquals(0, rpc.uploadCalls)
            rpc.supportsImageChat = true
            repo.syncNow()
            assertEquals(listOf(request), rpc.sentChatIds)
            assertTrue(rpc.chatParams.single().has("image_id"))
        } finally { repo.close() }
    }

    @Test fun lostUploadResponseIsRecoveredBeforeChatWithoutUploadingOrGeneratingTwice() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { loseUploadResponse = true }
        val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "photo.png", "image/png")
            repo.sendLegacyMessage(cid, "识别")
            val request = db.outbox().single { it.method == "chat.send" }.requestId
            repo.reconnect()
            assertEquals(0, rpc.chatCalls); assertEquals(1, rpc.uploadCalls)
            assertFalse(db.operation(request)!!.attempted)
            repo.reconnect(); repo.syncNow()
            assertEquals(1, rpc.chatCalls); assertEquals(1, rpc.uploadCalls)
            assertEquals(listOf(request), rpc.sentChatIds)
        } finally { repo.close() }
    }

    @Test fun lostImageChatResponseQueriesOriginalOperationAndDoesNotRepeatAi() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { loseChatResponse = true }
        val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "photo.png", "image/png")
            repo.sendLegacyMessage(cid, "识别")
            repo.reconnect(); repo.reconnect(); repo.syncNow()
            val chat = db.outbox().single { it.method == "chat.send" }
            assertEquals("pending", chat.status)
            assertEquals(1, rpc.uploadCalls); assertEquals(1, rpc.chatCalls)
            assertEquals(chat.requestId, rpc.sentChatIds.single())
            assertNotNull(chat.attachmentId)
        } finally { repo.close() }
    }

    @Test fun pendingUploadBlocksItsChatUntilTheOriginalUploadCompletes() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc().apply { loseUploadResponse = true }
        val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "photo.png", "image/png")
            repo.sendLegacyMessage(cid, "先上传后识图")
            val uploadId = db.outbox().single { it.method == "image.upload" }.requestId
            repo.reconnect()
            val completion = rpc.operations[uploadId]!!
            rpc.operations[uploadId] = JSONObject().put("state", "pending")
            repo.reconnect(); repo.syncNow()
            assertEquals(0, rpc.chatCalls)
            assertEquals(1, rpc.uploadCalls)
            assertEquals("pending", db.operation(uploadId)!!.item.status)
            rpc.operations[uploadId] = completion
            repo.syncNow()
            assertEquals(1, rpc.chatCalls); assertEquals(1, rpc.uploadCalls)
        } finally { repo.close() }
    }

    @Test fun cancellingImageMessageAlsoCancelsItsUploadAndPreservesOtherComposer() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val first = repo.newConversation(); val second = repo.newConversation()
            repo.stageImage(first, tinyPng(), "first.png", "image/png")
            repo.stageImage(second, tinyPng(), "second.png", "image/png")
            repo.sendLegacyMessage(first, "取消这条")
            repo.discardOutbox(db.outbox().single { it.method == "chat.send" }.requestId)
            assertTrue(db.outbox().all { it.status == "discarded" })
            assertNotNull(db.pendingImage(second))
            repo.reconnect()
            assertEquals(0, rpc.chatCalls); assertEquals(0, rpc.uploadCalls)
        } finally { repo.close() }
    }

    @Test fun deletingImageConversationKeepsBothQueueStepsForAttentionAndNeverUploads() = runBlocking {
        val db = seededDb(); val rpc = FakeRpc(); val repo = AppRepository(context, rpc, db)
        try {
            val cid = repo.newConversation()
            repo.stageImage(cid, tinyPng(), "photo.png", "image/png")
            repo.sendLegacyMessage(cid, "离线图片")
            rpc.deletedConversation = cid
            repo.reconnect()
            assertEquals(0, rpc.uploadCalls); assertEquals(0, rpc.chatCalls)
            assertTrue(db.outbox().all { it.status == "needs_attention" })
            assertTrue(db.messages(cid).isEmpty())
            assertEquals("离线图片", db.outbox().single { it.method == "chat.send" }.text)
        } finally { repo.close() }
    }
}
