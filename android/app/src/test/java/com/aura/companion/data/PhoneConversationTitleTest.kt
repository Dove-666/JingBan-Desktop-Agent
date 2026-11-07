package com.aura.companion.data

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class PhoneConversationTitleTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private fun withDatabase(block: (CompanionDatabase) -> Unit) {
        val db = CompanionDatabase(context, null)
        try { block(db) } finally { db.close() }
    }
    private fun title(db: CompanionDatabase, cid: String) = db.conversations().single { it.id == cid }.title
    private fun mirrored(cid: String, title: String) = JSONObject().put("entity", "conversation").put("id", cid)
        .put("conversation_id", cid).put("deleted", false).put("data", JSONObject().put("id", cid).put("title", title)
            .put("created_at", "2026-11-03T00:00:00Z").put("updated_at", "2026-11-03T00:01:00Z").put("message_count", 0))
    private fun snapshot(item: JSONObject) = JSONObject().put("items", JSONArray().put(item)).put("watermark", 1)
        .put("has_more", false).put("next_cursor", "")

    @Test fun firstCommittedTurnGetsImmediateTitleAndLaterTurnsCannotRenameIt() {
        withDatabase { db ->
            val cid = db.createConversation()
            val first = db.beginDirectTurn(cid, "  帮我规划\n下周的学习  ", null, allowUnpaired = true)
            assertEquals("帮我规划 下周的学习", title(db, cid))
            assertEquals("generating", first.state)
            db.failDirectTurn(first.id, "Synthetic interruption", interrupted = true)
            db.beginDirectTurn(cid, "另一个完全不同的话题", null, allowUnpaired = true)
            assertEquals("帮我规划 下周的学习", title(db, cid))
        }
    }

    @Test fun localLabelsStayBoundedAndNeverSplitUnicodeCodePoints() {
        val long = "🐳".repeat(30)
        assertEquals("🐳".repeat(24) + "…", phoneConversationTitle(long, false))
        assertEquals("一句 话", phoneConversationTitle("\n一句\t\u3000话\u0000 ", false))
        assertEquals("图片对话", phoneConversationTitle(" \n", true))
    }

    @Test fun existingLocalMeaningfulNameAndDesktopRenamesStayAuthoritative() {
        withDatabase { db ->
            val cid = db.createConversation()
            db.writableDatabase.execSQL("UPDATE conversations SET title=? WHERE id=?", arrayOf("用户设置的标题", cid))
            db.beginDirectTurn(cid, "第一个手机问题", null, allowUnpaired = true)
            assertEquals("用户设置的标题", title(db, cid))
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            db.stageSnapshotPage(snapshot(mirrored(cid, "电脑上重新命名")), true)
            db.beginDirectTurn(cid, "后续问题", null)
            assertEquals("电脑上重新命名", title(db, cid))
        }
    }

    @Test fun pairedSnapshotReplacesFallbackAndDefaultDesktopTitleIsNotAssumedAutomatic() {
        withDatabase { db ->
            val cid = db.createConversation()
            val turn = db.beginDirectTurn(cid, "本地最初主题", null, allowUnpaired = true)
            db.finishDirectTurn(turn.id, "Reply")
            assertEquals("本地最初主题", title(db, cid))
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            db.stageSnapshotPage(snapshot(mirrored(cid, "同步后的正式主题")), true)
            assertEquals("同步后的正式主题", title(db, cid))
            val remoteCid = UUID.randomUUID().toString()
            db.applyPull(JSONObject().put("changes", JSONArray().put(mirrored(remoteCid, "新对话").put("seq", 2)))
                .put("next_since", 2).put("has_more", false))
            db.beginDirectTurn(remoteCid, "不能覆盖可能的人为改名", null)
            assertEquals("新对话", title(db, remoteCid))
        }
    }

    @Test fun explicitQueuedMigrationLabelsOnlyOnceAndSecondAttemptDoesNotCreateTurn() {
        withDatabase { db ->
            val cid = db.createConversation()
            val queued = db.queueMessage(cid, "已排队的手机主题")
            val first = db.beginDirectTurn(cid, "已排队的手机主题", null, allowUnpaired = true, legacyRequestId = queued)
            assertEquals("已排队的手机主题", title(db, cid))
            db.failDirectTurn(first.id, "Synthetic interruption", interrupted = true)
            try {
                db.beginDirectTurn(cid, "错误的重复主题", null, allowUnpaired = true, legacyRequestId = queued)
                fail("A migrated request must not run twice")
            } catch (_: IllegalArgumentException) { }
            assertEquals(1, db.directTurns().size)
            assertEquals("已排队的手机主题", title(db, cid))
        }
    }

    @Test fun explicitAutomaticHostTitleKeepsFallbackButManualDefaultNameClearsIt() {
        withDatabase { db ->
            val cid = db.createConversation()
            db.beginDirectTurn(cid, "首条本地主题", null, allowUnpaired = true)
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            val automatic = mirrored(cid, "新对话").apply { getJSONObject("data").put("title_manual", false) }
            db.stageSnapshotPage(snapshot(automatic), true)
            assertEquals("首条本地主题", title(db, cid))
            val manual = mirrored(cid, "新对话").apply { getJSONObject("data").put("title_manual", true) }
            db.applyPull(JSONObject().put("changes", JSONArray().put(manual.put("seq", 2))).put("next_since", 2))
            assertEquals("新对话", title(db, cid))
            assertNull(db.getSetting("phone_title:$cid"))
            db.beginDirectTurn(cid, "不能更改手工标题", null)
            assertEquals("新对话", title(db, cid))
        }
    }

    @Test fun pairedAutomaticConversationCanLabelFirstPhoneTurnButMalformedFlagCannot() {
        for (manualValue in listOf<Any>(false, "false", 0)) withDatabase { db ->
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            val cid = UUID.randomUUID().toString()
            val item = mirrored(cid, "新对话").apply { getJSONObject("data").put("title_manual", manualValue) }
            db.stageSnapshotPage(snapshot(item), true)
            db.beginDirectTurn(cid, "本次手机主题", null)
            assertEquals(if (manualValue == false) "本次手机主题" else "新对话", title(db, cid))
        }
    }

    @Test fun completeSnapshotPreservesSameGenerationAndReplacementGenerationDropsOldFallback() {
        withDatabase { db ->
            val cid = db.createConversation()
            val turn = db.beginDirectTurn(cid, "旧的手机主题", null, allowUnpaired = true)
            db.saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
            db.writableDatabase.execSQL("UPDATE direct_turns SET state='imported' WHERE id=?", arrayOf(turn.id))
            fun page(userId: String): JSONObject {
                val conversation = mirrored(cid, "新对话").apply { getJSONObject("data").put("title_manual", false) }
                val user = JSONObject().put("entity", "message").put("id", userId).put("conversation_id", cid)
                    .put("data", JSONObject().put("id", userId).put("conversation_id", cid).put("role", "user")
                        .put("source", "user").put("content", "Mirrored fixture").put("created_at", turn.userAt))
                return JSONObject().put("items", JSONArray().put(conversation).put(user)).put("watermark", 1)
                    .put("has_more", false).put("next_cursor", "")
            }
            db.stageSnapshotPage(page(turn.userId), true)
            assertEquals("旧的手机主题", title(db, cid))
            db.stageSnapshotPage(page(UUID.randomUUID().toString()), true)
            assertEquals("新对话", title(db, cid))
            assertNull(db.getSetting("phone_title:$cid"))
        }
    }

    private val config get() = DirectApiConfig(LOCAL_PHONE_AUTHORITY, "local", "https://test.invalid/v1", "synthetic",
        "SYNTHETIC_TITLE_SECRET", "test", 30, "default", 0, ApiConfigSource.LOCAL)
    private class Vault(private val config: DirectApiConfig) : ApiConfigVault {
        override fun load() = config
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }
    private class OfflineRpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject = error("Offline fixture")
    }
    private class Stream : DirectChatGateway {
        val entered = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<Unit>()
        val calls = mutableListOf<JSONArray>()
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray) = error("Expected stream")
        override suspend fun stream(config: DirectApiConfig, messages: JSONArray, onUpdate: (DirectChatUpdate) -> Unit): String {
            calls += messages; entered.complete(Unit); finish.await()
            return "已完成"
        }
    }
    private suspend fun await(check: () -> Boolean) = withTimeout(5000) { while (!check()) delay(10) }

    @Test fun repositoryPublishesRedactedTitleBeforeFirstTokenWithoutExtraModelCall() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = Stream()
        val repo = AppRepository(context, OfflineRpc(), db, Vault(config), gateway)
        try {
            val cid = repo.newConversation()
            repo.sendMessage(cid, "主题 ${config.apiKey}")
            withTimeout(3000) { gateway.entered.await() }
            assertEquals("主题 [已隐藏密钥]", repo.state.value.conversations.single { it.id == cid }.title)
            assertTrue(repo.state.value.directChatBusy)
            assertEquals(1, gateway.calls.size)
            gateway.finish.complete(Unit); await { !repo.state.value.directChatBusy }
        } finally { repo.close() }
    }

    @Test fun imageOnlyTitleIsFriendlyAndNextTextNeverInheritsConsumedImage() = runBlocking {
        val db = CompanionDatabase(context, null); val gateway = Stream()
        val repo = AppRepository(context, OfflineRpc(), db, Vault(config), gateway)
        try {
            val cid = repo.newConversation()
            val png = Base64.decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==", Base64.NO_WRAP)
            repo.stageImage(cid, png, "phone.png", "image/png")
            repo.sendMessage(cid, " ")
            withTimeout(3000) { gateway.entered.await() }
            assertEquals("图片对话", repo.state.value.conversations.single { it.id == cid }.title)
            assertNull(db.pendingImage(cid))
            gateway.finish.complete(Unit); await { !repo.state.value.directChatBusy }
            repo.sendMessage(cid, "新的纯文字问题"); await { !repo.state.value.directChatBusy }
            assertEquals("图片对话", title(db, cid))
            assertNull(db.messages(cid).single { it.content == "新的纯文字问题" }.attachmentId)
            assertEquals("新的纯文字问题", gateway.calls.last().getJSONObject(gateway.calls.last().length() - 1).getString("content"))
            assertEquals(2, gateway.calls.size)
        } finally { repo.close() }
    }
}
