package com.aura.companion.data

import android.content.Context
import android.content.ContextWrapper
import android.database.sqlite.SQLiteDatabase
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.Rule
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.IOException
import java.io.File
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class DirectApiRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    @get:Rule val databaseFiles = TemporaryFolder()
    private val durableContext by lazy {
        // Native SQLite/WAL cannot use Robolectric's very long Windows method sandbox paths.
        object : ContextWrapper(context) {
            override fun getApplicationContext(): Context = this
            override fun getDatabasePath(name: String): File = File(databaseFiles.root, name).also {
                require(it.parentFile!!.isDirectory || it.parentFile!!.mkdirs())
            }
            override fun deleteDatabase(name: String): Boolean = SQLiteDatabase.deleteDatabase(getDatabasePath(name))
        }
    }
    private class Vault(var value: DirectApiConfig? = null) : ApiConfigVault {
        var persona: SyncedPersona? = null
        override fun load() = value
        override fun save(config: DirectApiConfig) { value = config }
        override fun clear() { value = null }
        override fun loadPersona() = persona
        override fun savePersona(persona: SyncedPersona) { this.persona = persona }
        override fun clearPersona() { persona = null }
    }
    private class Model : DirectChatGateway {
        val calls = mutableListOf<Pair<DirectApiConfig, JSONArray>>()
        var wait: CompletableDeferred<Unit>? = null
        var failure = false
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String {
            calls += config to messages
            wait?.await()
            if (failure) throw DirectApiFailure("模型请求未完成，可能已计费；不会自动重试")
            return "direct reply ${calls.size}"
        }
    }
    private class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var revision = "a".repeat(64)
        var apiKey = "private-test-key"
        var model = "model-a"
        var baseUrl = "https://example.test/v1"
        var available = true
        var authorized = true
        var supportsConfig = true
        var invalidConfig = false
        var personaName: String? = null
        var personaPrompt = "shared role alpha"
        var personaRevision = "role-a"
        var configGate: CompletableDeferred<Unit>? = null
        val configEntered = CompletableDeferred<Unit>()
        var statusGate: CompletableDeferred<Unit>? = null
        val statusEntered = CompletableDeferred<Unit>()
        var uploadGate: CompletableDeferred<Unit>? = null
        val uploadEntered = CompletableDeferred<Unit>()
        var uploadFailure = false
        var uploadCancellation = false
        var importCalls = 0
        var legacyCalls = 0
        var uploads = 0
        var loseImport = false
        var failPullAfterImport = false
        var connectGate: CompletableDeferred<Unit>? = null
        val connectEntered = CompletableDeferred<Unit>()
        val imports = mutableListOf<JSONObject>()
        val operations = mutableMapOf<String, JSONObject>()
        override suspend fun connect(ticket: String) { connectEntered.complete(Unit); connectGate?.await() }
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(result: JSONObject) = JSONObject().put("ok", true).put("result", result)
            fun fail(code: String) = JSONObject().put("ok", false).put("error", JSONObject().put("code", code).put("message", "safe failure"))
            if (!authorized) return fail("unauthorized")
            return when (method) {
                "pair" -> ok(JSONObject().put("paired", true))
                "status" -> {
                    statusGate?.let { gate -> statusGate = null; statusEntered.complete(Unit); gate.await() }
                    ok(JSONObject().put("protocol_version", 1).put("capabilities", JSONArray(if (supportsConfig) listOf("chat.config", "chat.import", "chat.image") else listOf("chat.image"))))
                }
                "chat.config" -> {
                  val result = JSONObject().put("version", if (invalidConfig) 99 else 1).put("available", available).put("revision", revision).apply {
                    personaName?.let { put("persona", JSONObject().put("version", 1).put("name", it)
                        .put("revision", personaRevision).put("system_prompt", personaPrompt)) }
                    if (available) put("base_url", baseUrl).put("api_key", apiKey).put("model", model)
                        .put("system_prompt", "SYSTEM PROMPT FROM PC").put("timeout_seconds", 1.5).put("reasoning_effort", "max")
                  }
                  configGate?.let { gate -> configGate = null; configEntered.complete(Unit); gate.await() }
                  ok(result)
                }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> if (failPullAfterImport && importCalls > 0) fail("sync_error") else ok(JSONObject().put("changes", JSONArray())
                    .put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                "operation.get" -> ok(operations[params.getString("request_id")] ?: JSONObject().put("state", "not_found"))
                "image.upload" -> {
                    uploads++
                    uploadGate?.let { gate -> uploadGate = null; uploadEntered.complete(Unit); gate.await() }
                    if (uploadFailure) { uploadFailure = false
                        if (uploadCancellation) throw CancellationException("Deliberate upload cancellation")
                        throw IOException("Deliberate upload failure after migration")
                    }
                    val bytes = Base64.decode(params.getString("data_base64"), Base64.NO_WRAP)
                    assertEquals(params.getString("sha256"), ImageCache.digest(bytes))
                    val image = JSONObject().put("id", params.getString("sha256")).put("filename", params.getString("filename"))
                        .put("mime_type", params.getString("mime_type")).put("size", bytes.size).put("width", 1).put("height", 1)
                        .put("created_at", "2026-10-31T10:00:00Z")
                    operations[requestId] = JSONObject().put("state", "complete").put("result", JSONObject().put("ok", true).put("result", image))
                    ok(image)
                }
                "chat.import" -> {
                    importCalls++; imports += JSONObject(params.toString())
                    val messages = params.getJSONArray("messages")
                    val result = JSONObject().put("imported", true).put("conversation_id", params.getString("conversation_id"))
                        .put("message_ids", JSONArray().put(messages.getJSONObject(0).getString("id")).put(messages.getJSONObject(1).getString("id")))
                    operations[requestId] = JSONObject().put("state", "complete").put("result", JSONObject().put("ok", true).put("result", result))
                    if (loseImport) { loseImport = false; throw IOException("private transport failure must not surface") }
                    ok(result)
                }
                "chat.send" -> { legacyCalls++; ok(JSONObject().put("conversation_id", params.getString("conversation_id"))
                    .put("message_id", AppRepository.stableMessageId(endpointId, requestId))) }
                else -> error("Unexpected RPC method")
            }
        }
    }
    private fun db() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""), true)
    }
    private suspend fun await(check: () -> Boolean) = withTimeout(5000) { while (!check()) delay(10) }

    @Test fun cachedApiWorksOfflineThenImportsSavedIdsWithoutSecondModelCall() = runBlocking {
        val db = db(); val rpc = Rpc(); val vault = Vault(); val model = Model()
        val repo = AppRepository(context, rpc, db, vault, model)
        try {
            repo.reconnect(); assertTrue(repo.state.value.directApi.available)
            repo.disconnect()
            val cid = repo.newConversation(); repo.sendMessage(cid, "offline direct")
            await { !repo.state.value.directChatBusy }
            assertEquals(1, model.calls.size); assertEquals(0, rpc.legacyCalls); assertEquals(0, rpc.importCalls)
            assertEquals(listOf("offline direct", "direct reply 1"), db.messages(cid).map { it.content })
            val user = db.messages(cid).first().id
            assertEquals(1, repo.state.value.historyPendingCount)
            repo.reconnect(); repo.syncNow()
            assertEquals(1, model.calls.size); assertEquals(1, rpc.importCalls); assertEquals(0, rpc.legacyCalls)
            assertEquals(user, rpc.imports.single().getJSONArray("messages").getJSONObject(0).getString("id"))
            assertEquals(2, db.messages(cid).size); assertEquals(0, repo.state.value.historyPendingCount)
            assertEquals("SYSTEM PROMPT FROM PC", model.calls.single().second.getJSONObject(0).getString("content"))
            assertEquals("max", model.calls.single().first.reasoningEffort)
            assertFalse(db.readableDatabase.rawQuery("SELECT value FROM settings", null).use { cursor ->
                buildList { while (cursor.moveToNext()) add(cursor.getString(0)) }.joinToString().contains("private-test-key") })
        } finally { repo.close() }
    }

    @Test fun lostImportResponseQueriesOriginalOperationAndKeepsVisibleReply() = runBlocking {
        val db = db(); val rpc = Rpc().apply { loseImport = true }; val model = Model()
        val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); repo.disconnect()
            val cid = repo.newConversation(); repo.sendMessage(cid, "stable import")
            await { !repo.state.value.directChatBusy }; repo.reconnect()
            val original = db.outbox().single { it.method == "chat.import" }.requestId
            assertEquals("unknown", db.operation(original)!!.item.status)
            repo.reconnect(); repo.syncNow()
            assertEquals(1, rpc.importCalls); assertEquals(1, model.calls.size)
            assertEquals("sent", db.operation(original)!!.item.status)
            assertEquals(2, db.messages(cid).size)
        } finally { repo.close() }
    }

    @Test fun modelFailureStaysUnknownAcrossSyncAndNeverFallsBackToPc() = runBlocking {
        val db = db(); val rpc = Rpc(); val model = Model().apply { failure = true }
        val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); val cid = repo.newConversation(); repo.sendMessage(cid, "uncertain billing")
            await { !repo.state.value.directChatBusy }; repo.syncNow(); repo.reconnect()
            assertEquals(1, model.calls.size); assertEquals(0, rpc.legacyCalls); assertEquals(0, rpc.importCalls)
            assertEquals("unknown", db.messages(cid).single().localState)
            assertTrue(repo.state.value.error!!.contains("不会自动重试"))
        } finally { repo.close() }
    }

    @Test fun importAcknowledgmentFollowedByLostPullKeepsReplyAndNeverCallsModelAgain() = runBlocking {
        val db = db(); val rpc = Rpc().apply { failPullAfterImport = true }; val model = Model()
        val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); repo.disconnect()
            val cid = repo.newConversation(); repo.sendMessage(cid, "reply must survive lost pull")
            await { !repo.state.value.directChatBusy }; repo.reconnect()
            assertEquals(SyncState.ERROR, repo.state.value.syncState)
            assertEquals(2, db.messages(cid).size)
            assertEquals("sent", db.outbox().single { it.method == "chat.import" }.status)
            repo.disconnect()
            assertEquals(listOf("reply must survive lost pull", "direct reply 1"), repo.state.value.messages.map { it.content })
            assertEquals(1, rpc.importCalls); assertEquals(1, model.calls.size); assertEquals(0, rpc.legacyCalls)
        } finally { repo.close() }
    }

    @Test fun configRotationUnavailableKeepsCacheButObservedRevocationClearsDesktopCredentials() = runBlocking {
        val db = db(); val rpc = Rpc(); val vault = Vault(); val repo = AppRepository(context, rpc, db, vault, Model())
        try {
            repo.reconnect(); rpc.apiKey = "rotated-private-key"; rpc.model = "model-b"; rpc.revision = "b".repeat(64)
            repo.syncNow(); assertEquals("rotated-private-key", vault.value!!.apiKey); assertEquals("model-b", repo.state.value.directApi.model)
            rpc.available = false; repo.syncNow(); assertNotNull(vault.value); assertTrue(repo.state.value.directApi.available)
            assertTrue(repo.state.value.directApi.message.contains("继续使用"))
            rpc.available = true; repo.syncNow(); assertNotNull(vault.value)
            rpc.authorized = false; repo.syncNow()
            assertNull(vault.value); assertFalse(repo.state.value.directApi.available); assertEquals(ConnectionState.UNAUTHORIZED, repo.state.value.connectionState)
        } finally { repo.close() }
    }

    @Test fun invalidHttpConfigCannotSilentlyChargePcFallback() = runBlocking {
        val db = db(); val rpc = Rpc().apply { baseUrl = "http://example.test/v1" }; val model = Model()
        val repo = AppRepository(context, rpc, db, Vault(), model, allowCleartextApi = false)
        try {
            repo.reconnect(); assertFalse(repo.state.value.directApi.available)
            assertTrue(repo.state.value.directApi.message.contains("HTTPS"))
            repo.sendMessage(repo.newConversation(), "must not silently fall back")
            assertEquals(0, model.calls.size); assertEquals(0, rpc.legacyCalls); assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun accidentallyPastedKnownKeyNeverEntersLocalTranscriptImportOrHttpContent() = runBlocking {
        val db = db(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); repo.disconnect()
            val cid = repo.newConversation(); repo.sendMessage(cid, "accidental paste ${rpc.apiKey}")
            await { !repo.state.value.directChatBusy }
            assertEquals("accidental paste [已隐藏密钥]", db.messages(cid).first().content)
            assertFalse(db.directTurns().single().text.contains(rpc.apiKey))
            assertFalse(model.calls.single().second.toString().contains(rpc.apiKey))
            repo.reconnect(); repo.syncNow()
            assertFalse(rpc.imports.single().toString().contains(rpc.apiKey))
            assertEquals(1, model.calls.size); assertEquals(1, rpc.importCalls)
        } finally { repo.close() }
    }

    @Test fun pcReconnectMutexDoesNotBlockLocalComposerOrDirectModelAndBusyPreventsDuplicate() = runBlocking {
        val db = db(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); rpc.connectGate = CompletableDeferred(); model.wait = CompletableDeferred()
            val reconnect = launch { repo.reconnect() }; delay(30)
            val cid = withTimeout(1500) {
                val id = repo.newConversation(); repo.selectConversation(id)
                repo.stageImage(id, png(), "one.png", "image/png"); repo.removePendingImage(id)
                repo.sendMessage(id, "direct while PC reconnects"); id
            }
            await { model.calls.size == 1 }
            assertEquals("generating", db.messages(cid).single().localState)
            repo.sendMessage(cid, "duplicate click")
            assertEquals(1, db.directTurns().size); assertEquals(1, model.calls.size)
            model.wait!!.complete(Unit); rpc.connectGate!!.complete(Unit); reconnect.join()
            await { !repo.state.value.directChatBusy }
        } finally { rpc.connectGate?.complete(Unit); model.wait?.complete(Unit); repo.close() }
    }

    @Test fun directImageAndSameConversationFollowupIncludeSameBytesButOtherConversationDoesNot() = runBlocking {
        val db = db(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); repo.disconnect()
            val cid = repo.newConversation(); repo.stageImage(cid, png(), "one.png", "image/png")
            assertEquals(0, model.calls.size)
            repo.sendMessage(cid, ""); await { !repo.state.value.directChatBusy }
            repo.sendMessage(cid, "上面那张图片"); await { !repo.state.value.directChatBusy }
            repo.sendMessage(repo.newConversation(), "other thread"); await { !repo.state.value.directChatBusy }
            for ((index, call) in model.calls.take(2).withIndex()) {
                val content = call.second.getJSONObject(if (index == 0) call.second.length() - 1 else 1).getJSONArray("content")
                val imageUrl = content.getJSONObject(1).getJSONObject("image_url").getString("url")
                assertArrayEquals(png(), Base64.decode(imageUrl.substringAfter(','), Base64.NO_WRAP))
            }
            assertEquals("请描述这张图片。", db.messages(cid).first().content)
            assertNull(db.messages(cid).single { it.content == "上面那张图片" }.attachmentId)
            assertEquals("上面那张图片", model.calls[1].second.getJSONObject(model.calls[1].second.length() - 1).getString("content"))
            assertEquals("other thread", model.calls.last().second.getJSONObject(model.calls.last().second.length() - 1).getString("content"))
            repo.reconnect(); repo.syncNow()
            assertEquals(3, rpc.importCalls); assertEquals(3, model.calls.size); assertEquals(0, rpc.legacyCalls)
            assertFalse(rpc.imports.single { it.getJSONArray("messages").getJSONObject(0).getString("content") == "上面那张图片" }
                .getJSONArray("messages").getJSONObject(0).has("image_id"))
            assertEquals(1, rpc.uploads)
        } finally { repo.close() }
    }

    @Test fun authoritySwitchBetweenRouteChoiceAndTransactionRejectsOldConfigAndNeverLeaksIntoNewPc() = runBlocking {
        val db = db(); val cid = db.createConversation()
        db.saveDevice(DeviceInfo("B", "desktop-b", "public-b"), "phone")
        try {
            try { db.beginDirectTurn(cid, "old route", null, "desktop"); fail("Expected authority rejection") }
            catch (expected: IllegalArgumentException) { assertFalse(expected.message!!.contains("private")) }
            assertTrue(db.directTurns().isEmpty())
        } finally { db.close() }
    }

    private fun png() = Base64.decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==", Base64.NO_WRAP)

    @Test fun absentHistoricalImageComparisonKeepsComposerAndNeverCallsModel() = runBlocking {
        val db = CompanionDatabase(context, null); val rpc = Rpc(); val model = Model()
        val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "model", "SYNTHETIC_KEY", 30, "default"))
            val cid = repo.newConversation(); repo.stageImage(cid, png(), "new.png", "image/png")
            val pending = db.pendingImage(cid); assertNotNull(pending)
            repo.sendMessage(cid, "这张和上一张图片有什么区别？")
            await { !repo.state.value.directChatBusy }
            assertEquals(0, model.calls.size); assertEquals(0, rpc.legacyCalls)
            assertTrue(db.outbox().isEmpty()); assertTrue(db.directTurns().isEmpty()); assertTrue(db.messages(cid).isEmpty())
            assertEquals(pending, db.pendingImage(cid))
            assertEquals("无法读取上一张图片，请重新选择图片后再比较。", repo.state.value.error)
        } finally { repo.close() }
    }

    @Test fun missingHistoricalImageCacheComparisonKeepsComposerAndNeverCallsModel() = runBlocking {
        val db = CompanionDatabase(context, null); val rpc = Rpc(); val model = Model()
        val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "model", "SYNTHETIC_KEY", 30, "default"))
            val cid = repo.newConversation(); val oldImage = "a".repeat(64)
            val previous = db.beginDirectTurn(cid, "original photo", oldImage, allowUnpaired = true)
            db.finishDirectTurn(previous.id, "original visible answer")
            assertNull(db.image(oldImage))
            val originalMessages = db.messages(cid); val originalTurns = db.directTurns()
            repo.stageImage(cid, png(), "new.png", "image/png")
            val pending = db.pendingImage(cid); assertNotNull(pending)
            repo.sendMessage(cid, "Compare this photo with the previous image")
            await { !repo.state.value.directChatBusy }
            assertEquals(0, model.calls.size); assertEquals(0, rpc.legacyCalls)
            assertTrue(db.outbox().isEmpty()); assertEquals(originalTurns, db.directTurns())
            assertEquals(originalMessages, db.messages(cid)); assertEquals(pending, db.pendingImage(cid))
            assertEquals("无法读取上一张图片，请重新选择图片后再比较。", repo.state.value.error)
        } finally { repo.close() }
    }

    @Test fun missingModelNeverCreatesAnImplicitLegacyQueueAndKeepsComposerImage() = runBlocking {
        val db = CompanionDatabase(context, null); val rpc = Rpc(); val model = Model()
        val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            val cid = repo.newConversation(); repo.stageImage(cid, png(), "draft.png", "image/png")
            repo.sendMessage(cid, "keep this draft")
            assertTrue(db.outbox().isEmpty()); assertTrue(db.directTurns().isEmpty()); assertEquals(0, model.calls.size)
            assertTrue(repo.state.value.error!!.contains("配置手机模型")); assertNotNull(db.pendingImage(cid))
        } finally { repo.close() }
    }

    @Test fun standaloneManualApiSupportsUnpairedImageFollowupBlankKeyAndOfflineRestart() = runBlocking {
        val name = "standalone-${UUID.randomUUID()}.db"; val vault = Vault(); val model = Model(); val rpc = Rpc()
        var db = CompanionDatabase(durableContext, name); var repo = AppRepository(durableContext, rpc, db, vault, model)
        try {
            val saved = repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "SYNTHETIC_PHONE_SECRET", 30, "low")
            assertTrue("Independent configuration save failed: ${repo.state.value.error}", saved)
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model-updated", "", 40, "max"))
            assertEquals("SYNTHETIC_PHONE_SECRET", vault.value!!.apiKey)
            assertEquals(ApiConfigSource.LOCAL, repo.state.value.directApi.source); assertTrue(repo.state.value.directApi.keySaved)
            val cid = repo.newConversation(); repo.stageImage(cid, png(), "local.png", "image/png"); repo.sendMessage(cid, "")
            await { !repo.state.value.directChatBusy }; repo.sendMessage(cid, "上面那张图片"); await { !repo.state.value.directChatBusy }
            assertNull(db.device()); assertEquals(2, model.calls.size); assertTrue(db.directTurns().all { it.authorityId == LOCAL_PHONE_AUTHORITY && it.state == "ready" })
            val ids = db.messages(cid).map { it.id }; repo.close()
            db = CompanionDatabase(durableContext, name); repo = AppRepository(durableContext, rpc, db, vault, model)
            repo.selectConversation(cid)
            assertTrue(repo.state.value.directApi.available); assertEquals(ids, repo.state.value.messages.map { it.id })
            assertEquals("图片对话", repo.state.value.conversations.single { it.id == cid }.title)
            assertEquals(2, model.calls.size); assertEquals(0, rpc.legacyCalls)
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            repo.reconnect(); repo.syncNow()
            assertEquals(2, rpc.importCalls); assertEquals(2, model.calls.size); assertEquals(ids, db.messages(cid).map { it.id })
            assertEquals(ApiConfigSource.LOCAL, repo.state.value.directApi.source)
        } finally { repo.close(); durableContext.deleteDatabase(name) }
    }

    @Test fun cachedDesktopConfigSurvivesLegacyCapabilitiesUnavailableAndInvalidRefresh() = runBlocking {
        val db = db(); val rpc = Rpc(); val vault = Vault(); val model = Model(); val repo = AppRepository(context, rpc, db, vault, model)
        try {
            repo.reconnect(); val key = vault.value!!.apiKey
            rpc.supportsConfig = false; repo.reconnect()
            assertTrue(repo.state.value.directApi.available); assertEquals(key, vault.value!!.apiKey)
            rpc.supportsConfig = true; rpc.available = false; repo.syncNow()
            assertTrue(repo.state.value.directApi.available); assertEquals(key, vault.value!!.apiKey)
            rpc.available = true; rpc.invalidConfig = true; repo.syncNow()
            assertTrue(repo.state.value.directApi.available); assertEquals(key, vault.value!!.apiKey)
            repo.disconnect(); repo.sendMessage(repo.newConversation(), "cached while PC disconnected")
            await { !repo.state.value.directChatBusy }; assertEquals(1, model.calls.size); assertEquals(0, rpc.legacyCalls)
        } finally { repo.close() }
    }

    @Test fun independentManualConfigIsNotOverwrittenOrRevokedWithDesktopAuthorization() = runBlocking {
        val db = db(); val rpc = Rpc(); val vault = Vault(); val model = Model(); val repo = AppRepository(context, rpc, db, vault, model)
        try {
            repo.reconnect(); assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "my-phone-model", "PHONE_PRIVATE_SYNTHETIC", 25, "default"))
            rpc.model = "new-desktop-model"; rpc.revision = "c".repeat(64); repo.syncNow()
            assertEquals("my-phone-model", repo.state.value.directApi.model); assertEquals("PHONE_PRIVATE_SYNTHETIC", vault.value!!.apiKey)
            assertEquals("new-desktop-model", vault.value!!.desktopCache!!.model)
            rpc.available = false; repo.syncNow(); assertTrue(repo.state.value.directApi.available)
            rpc.authorized = false; repo.syncNow()
            assertEquals(ConnectionState.UNAUTHORIZED, repo.state.value.connectionState)
            assertTrue(repo.state.value.directApi.available); assertNull(vault.value!!.desktopCache)
            repo.sendMessage(repo.newConversation(), "independent after desktop revoke"); await { !repo.state.value.directChatBusy }
            assertEquals(1, model.calls.size); assertEquals(LOCAL_PHONE_AUTHORITY, db.directTurns().single().authorityId)
        } finally { repo.close() }
    }

    @Test fun manualProviderFollowsRoleChangesAndDefaultWithoutUsingDesktopCredentials() = runBlocking {
        val rpc = Rpc().apply { personaName = "Alpha" }; val vault = Vault(); val model = Model()
        val repo = AppRepository(context, rpc, db(), vault, model)
        try {
            repo.reconnect()
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_ROLE_TEST_SECRET", 25, "low"))
            rpc.available = false; rpc.personaName = "Beta"; rpc.personaPrompt = "shared role beta"; rpc.personaRevision = "role-b"
            repo.syncNow()
            val active = vault.value!!
            assertEquals(ApiConfigSource.LOCAL, active.source); assertEquals("https://phone.example/v1", active.baseUrl)
            assertEquals("phone-model", active.model); assertEquals("PHONE_ROLE_TEST_SECRET", active.apiKey)
            assertEquals(25, active.timeoutSeconds); assertEquals("low", active.reasoningEffort)
            assertEquals("shared role beta", active.systemPrompt); assertEquals("role-b", vault.persona!!.revision)
            repo.disconnect(); repo.sendMessage(repo.newConversation(), "offline with current role")
            await { !repo.state.value.directChatBusy }
            assertEquals("shared role beta", model.calls.single().second.getJSONObject(0).getString("content"))
            rpc.personaName = "鲸鱼娘"; rpc.personaPrompt = "default whale role"; rpc.personaRevision = "role-default"
            repo.reconnect()
            assertEquals("default whale role", vault.value!!.systemPrompt); assertEquals("phone-model", vault.value!!.model)
            assertEquals(1, model.calls.size)
        } finally { repo.close() }
    }

    @Test fun roleReceivedBeforeAnyApiSurvivesRestartAndIsUsedByNewManualProvider() = runBlocking {
        val name = "role-before-api-${UUID.randomUUID()}.db"
        val rpc = Rpc().apply { available = false; personaName = "Alpha" }
        val vault = Vault(); val model = Model()
        var database = CompanionDatabase(durableContext, name).apply { saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone") }
        var repo = AppRepository(durableContext, rpc, database, vault, model)
        try {
            repo.reconnect(); assertNull(vault.value); assertEquals("shared role alpha", vault.persona!!.systemPrompt)
            repo.close()
            database = CompanionDatabase(durableContext, name); repo = AppRepository(durableContext, rpc, database, vault, model)
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_ROLE_TEST_SECRET", 30, "default"))
            assertEquals("shared role alpha", vault.value!!.systemPrompt)
            repo.sendMessage(repo.newConversation(), "role after restart")
            await { !repo.state.value.directChatBusy }
            assertEquals("shared role alpha", model.calls.single().second.getJSONObject(0).getString("content"))
        } finally { repo.close(); durableContext.deleteDatabase(name) }
    }

    @Test fun rolePollingIsIndependentOfApiRevisionAndDoesNotRefetchOnEveryIdlePoll() = runBlocking {
        var now = 1000L
        val rpc = Rpc().apply { personaName = "Alpha" }; val vault = Vault()
        val repo = AppRepository(context, rpc, db(), vault, Model(), monotonicMillis = { now })
        try {
            repo.reconnect(); val apiRevision = vault.value!!.revision
            rpc.personaPrompt = "shared role beta"; rpc.personaRevision = "role-b"
            repo.poll(); assertEquals("shared role alpha", vault.value!!.systemPrompt)
            now += 30_001; repo.poll()
            assertEquals(apiRevision, vault.value!!.revision)
            assertEquals("shared role beta", vault.value!!.systemPrompt)
        } finally { repo.close() }
    }

    @Test fun invalidRoleKeepsLastValidPersonaAndNeverReplacesManualApi() = runBlocking {
        val rpc = Rpc().apply { personaName = "Alpha" }; val vault = Vault()
        val repo = AppRepository(context, rpc, db(), vault, Model())
        try {
            repo.reconnect(); assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_ROLE_TEST_SECRET", 30, "default"))
            rpc.personaPrompt = "x".repeat(32769); rpc.personaRevision = "invalid-role"
            repo.syncNow()
            assertEquals("shared role alpha", vault.value!!.systemPrompt); assertEquals("role-a", vault.persona!!.revision)
            assertEquals("PHONE_ROLE_TEST_SECRET", vault.value!!.apiKey)
        } finally { repo.close() }
    }

    @Test fun pairingAndChangingComputersSynchronizesNewRoleWithTheSamePhoneProvider() = runBlocking {
        val database = CompanionDatabase(context, null)
        val rpc = Rpc().apply { personaName = "Alpha"; available = false }; val vault = Vault()
        val repo = AppRepository(context, rpc, database, vault, Model())
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_ROLE_TEST_SECRET", 30, "default"))
            fun pairing(id: String) = JSONObject().put("version", 1).put("ticket", "public-ticket").put("code", "temporary-test-code")
                .put("endpoint_id", id).toString()
            repo.pair(pairing("desktop"), "owned phone")
            assertEquals("desktop", vault.persona!!.authorityId); assertEquals("shared role alpha", vault.value!!.systemPrompt)
            rpc.personaName = "Beta"; rpc.personaPrompt = "other desktop role"; rpc.personaRevision = "other-role"
            repo.pair(pairing("other-desktop"), "owned phone")
            assertEquals("other-desktop", vault.persona!!.authorityId); assertEquals("other desktop role", vault.value!!.systemPrompt)
            assertEquals("phone-model", vault.value!!.model); assertEquals("PHONE_ROLE_TEST_SECRET", vault.value!!.apiKey)
        } finally { repo.close() }
    }

    @Test fun responseFromPreviousComputerCannotOverwriteCurrentCachedRole() = runBlocking {
        val database = db(); val rpc = Rpc().apply { personaName = "Alpha" }; val vault = Vault()
        val repo = AppRepository(context, rpc, database, vault, Model())
        val gate = CompletableDeferred<Unit>()
        try {
            repo.reconnect(); rpc.personaPrompt = "stale role beta"; rpc.personaRevision = "role-b"; rpc.configGate = gate
            val refresh = launch { repo.refreshApiConfig() }
            withTimeout(5000) { rpc.configEntered.await() }
            database.saveDevice(DeviceInfo("Other PC", "other-desktop", "other-public-ticket"), "phone")
            gate.complete(Unit); refresh.join()
            assertEquals("role-a", vault.persona!!.revision); assertEquals("shared role alpha", vault.value!!.systemPrompt)
        } finally { gate.complete(Unit); repo.close() }
    }

    @Test fun explicitQueuedImageMigrationPreservesUserIdAndImageAndDoesNotSendOldComputerRequest() = runBlocking {
        val db = db(); val rpc = Rpc(); val vault = Vault(); val model = Model(); val repo = AppRepository(context, rpc, db, vault, model)
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_PRIVATE_SYNTHETIC", 30, "default"))
            val cid = repo.newConversation(); repo.stageImage(cid, png(), "queued.png", "image/png")
            repo.sendLegacyMessage(cid, "queued image")
            val queued = db.outbox().single { it.method == "chat.send" }; assertTrue(queued.canSendOnPhone)
            repo.sendQueuedOnPhone(queued.requestId); await { !repo.state.value.directChatBusy }
            assertEquals("discarded", db.operation(queued.requestId)!!.item.status)
            assertEquals(queued.requestId, db.messages(cid).first().id); assertEquals(queued.attachmentId, db.messages(cid).first().attachmentId)
            repo.sendQueuedOnPhone(queued.requestId); assertEquals(1, model.calls.size)
            repo.reconnect(); repo.syncNow()
            assertEquals(0, rpc.legacyCalls); assertEquals(1, rpc.importCalls); assertEquals(1, model.calls.size)
        } finally { repo.close() }
    }

    @Test fun staleComputerFlushCannotClaimQueueAfterPhoneMigrationWins() = runBlocking {
        val db = db(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            repo.reconnect(); val cid = repo.newConversation(); val id = db.queueMessage(cid, "one model only")
            val gate = CompletableDeferred<Unit>(); rpc.statusGate = gate
            val oldFlush = launch { repo.poll() }; withTimeout(5000) { rpc.statusEntered.await() }
            repo.sendQueuedOnPhone(id); await { !repo.state.value.directChatBusy }
            gate.complete(Unit); oldFlush.join(); repo.syncNow()
            assertEquals("discarded", db.operation(id)!!.item.status)
            assertEquals(0, rpc.legacyCalls); assertEquals(1, model.calls.size)
        } finally { rpc.statusGate?.complete(Unit); repo.close() }
    }

    @Test fun failedOrCancelledOldUploadCannotReviveMigratedChatAndChargeSecondModel() = runBlocking {
        for (cancel in listOf(false, true)) {
            val db = db(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
            try {
                repo.reconnect(); val cid = repo.newConversation(); repo.stageImage(cid, png(), "race.png", "image/png")
                val id = db.queueMessage(cid, "upload is in progress")
                val gate = CompletableDeferred<Unit>(); rpc.uploadGate = gate; rpc.uploadFailure = true; rpc.uploadCancellation = cancel
                val oldFlush = launch { repo.poll() }; withTimeout(5000) { rpc.uploadEntered.await() }
                repo.sendQueuedOnPhone(id); await { !repo.state.value.directChatBusy }
                gate.complete(Unit); oldFlush.join()
                assertEquals("discarded", db.operation(id)!!.item.status)
                repo.reconnect(); repo.syncNow()
                assertEquals("discarded", db.operation(id)!!.item.status)
                assertEquals(0, rpc.legacyCalls); assertEquals(1, model.calls.size); assertEquals(1, rpc.importCalls)
            } finally { rpc.uploadGate?.complete(Unit); repo.close() }
        }
    }

    @Test fun attemptedUnknownRequestsCannotBeMovedToPhoneAndTerminalStatesCannotBeReopened() = runBlocking {
        val db = db(); val rpc = Rpc(); val model = Model(); val repo = AppRepository(context, rpc, db, Vault(), model)
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_PRIVATE_SYNTHETIC", 30, "default"))
            val cid = repo.newConversation(); val id = db.queueMessage(cid, "already admitted")
            assertTrue(db.claimOutboxForSend(id, AppRepository.stableMessageId("phone", id)))
            db.updateOutbox(id, "unknown"); assertFalse(db.operation(id)!!.item.canSendOnPhone)
            repo.sendQueuedOnPhone(id); assertEquals(0, model.calls.size)
            db.updateOutbox(id, "discarded"); db.updateOutbox(id, "queued", attempted = false); db.updateOutbox(id, "unknown")
            assertEquals("discarded", db.operation(id)!!.item.status); assertFalse(db.claimOutboxForSend(id, null))
        } finally { repo.close() }
    }

    @Test fun manualPublicFieldsCannotContainKnownSecretAndUnpairedDesktopCacheNeedsExplicitConfirmation() = runBlocking {
        val db = CompanionDatabase(context, null); val vault = Vault(DirectApiConfig("old-desktop", "r", "https://example.test/v1", "old-model",
            "SECRET_FOR_BOUND_CACHE", "prompt", 30, "default", 1)); val model = Model(); val repo = AppRepository(context, Rpc(), db, vault, model)
        try {
            repo.selectConversation(null)
            assertFalse(repo.state.value.directApi.available); assertTrue(repo.apiConfigDraft().keySaved)
            repo.sendMessage(repo.newConversation(), "must configure"); assertEquals(0, model.calls.size); assertTrue(db.outbox().isEmpty())
            assertFalse(repo.saveDirectApiConfig("https://phone.example/SECRET_FOR_BOUND_CACHE", "model", "", 30, "default"))
            assertFalse(repo.state.value.error!!.contains("SECRET_FOR_BOUND_CACHE"))
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "confirmed-local-model", "", 30, "default"))
            assertEquals(ApiConfigSource.LOCAL, repo.state.value.directApi.source); assertTrue(repo.state.value.directApi.available)
        } finally { repo.close() }
    }

    @Test fun reasoningFieldsRejectPreviousKeysAndNewDesktopSecretsAreNeverPublished() = runBlocking {
        val db = CompanionDatabase(context, null); val vault = Vault(); val rpc = Rpc(); val repo = AppRepository(context, rpc, db, vault, Model())
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "OldPhoneToken", 30, "default"))
            assertFalse(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "NewPhoneToken", 30,
                "OldPhoneToken", "custom"))
            val declaration = JSONObject().put("version", 1).put("base_url", "https://phone.example/v1").put("model", "phone-model")
                .put("profile", "openai").put("values", JSONArray(listOf("OldPhoneToken")))
            assertFalse(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "NewPhoneToken", 30,
                "default", "auto", reasoningMetadata = declaration.toString()))
            assertEquals("OldPhoneToken", vault.value!!.apiKey)
            assertFalse(repo.state.value.error.orEmpty().contains("OldPhoneToken"))
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "", 30, "UltraMode", "custom"))
            rpc.apiKey = "UltraMode"
            db.saveDevice(DeviceInfo("Computer", "desktop", "ticket"), "phone")
            repo.reconnect()
            assertFalse(repo.state.value.directApi.available)
            assertFalse(repo.state.value.directApi.toString().contains("UltraMode"))
            assertFalse(repo.apiConfigDraft().toString().contains("UltraMode"))
            assertFalse(repo.state.value.error.orEmpty().contains("UltraMode"))
            assertEquals("OldPhoneToken", vault.value!!.apiKey)
        } finally { repo.close() }
    }

    @Test fun standaloneUserCanKeepChattingBeyondFiveHundredUnsyncedExchanges() = runBlocking {
        val db = CompanionDatabase(context, null); val model = Model(); val repo = AppRepository(context, Rpc(), db, Vault(), model)
        try {
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "PHONE_PRIVATE_SYNTHETIC", 30, "default"))
            val cid = repo.newConversation(); val sql = db.writableDatabase
            sql.beginTransaction()
            try {
                repeat(500) { index -> sql.execSQL("""INSERT INTO direct_turns(id,conversation_id,authority_id,user_id,assistant_id,text,user_at,reply,assistant_at,state)
                    VALUES(?,?,?,?,?,'old question',?,'old reply',?,'ready')""", arrayOf(UUID.randomUUID().toString(), cid, LOCAL_PHONE_AUTHORITY,
                    UUID.randomUUID().toString(), UUID.randomUUID().toString(), "2026-10-30T00:00:${index.toString().padStart(3, '0')}Z", "2026-10-30T01:00:${index.toString().padStart(3, '0')}Z")) }
                sql.setTransactionSuccessful()
            } finally { sql.endTransaction() }
            repo.sendMessage(cid, "still usable without a PC"); await { !repo.state.value.directChatBusy }
            assertEquals(1, model.calls.size); assertEquals(501, db.directTurns().size)
            assertTrue(db.messages(cid).any { it.content == "direct reply 1" }); assertNull(db.device())
        } finally { repo.close() }
    }
}
