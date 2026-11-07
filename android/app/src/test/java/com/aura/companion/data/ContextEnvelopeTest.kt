package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
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
class ContextEnvelopeTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val cid = UUID.randomUUID().toString()
    private val config = DirectApiConfig("phone", "rev", "https://test.invalid/v1", "test", "SYNTHETIC_KEY", "system", 30, "default", 0)
    private fun history(count: Int, size: Int = 15) = (0 until count).map { index ->
        Message(UUID.randomUUID().toString(), cid, if (index % 2 == 0) "user" else "assistant", "记录$index " + "鲸".repeat(size),
            "2026-11-02T00:00:${index.toString().padStart(2, '0')}Z")
    }

    @Test fun moreThanTwentyTurnsStayInBudgetAndOldOmissionsHaveExplicitAttributedSummary() {
        val short = history(60)
        val messages = directMessages(config, short, "new question", null)
        assertEquals(62, messages.length())
        assertEquals(short.first().content, messages.getJSONObject(1).getString("content"))
        val incomplete = short.last().copy(id = "unfinished-desktop-reply", content = "partial reply", requestStatus = "failed")
        assertFalse(incomplete.id in ContextEnvelope.local(cid, short + incomplete, "question").recentIds)
        val long = history(80, 400)
        val envelope = ContextEnvelope.local(cid, long, "记录1")
        assertTrue(envelope.json.getInt("omitted_count") > 0)
        assertFalse(envelope.json.getJSONObject("summary").getBoolean("verified"))
        assertTrue(envelope.json.getJSONObject("summary").getString("text").contains("未覆盖"))
        assertTrue(envelope.referenceText().toByteArray(Charsets.UTF_8).size < 12 * 1024)
        assertTrue(directMessages(config, long, "new question", null).toString().toByteArray(Charsets.UTF_8).size < 56 * 1024)
        assertNotEquals(envelope.revision, ContextEnvelope.local(cid, long.mapIndexed { index, message ->
            if (index == 2) message.copy(content = "Changed old omitted record") else message
        }, "记录1").revision)
    }

    @Test fun cacheBindsPeerConversationGenerationAndDisabledMemoryReplacesOldActiveEntries() {
        val db = CompanionDatabase(context, null)
        try {
            db.saveDevice(DeviceInfo("test pc", "peer-one", "ticket"), "phone")
            val rows = history(3); val generation = rows.first().id
            db.createConversation(cid)
            for (row in rows) db.writableDatabase.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
                arrayOf(row.id, cid, row.role, row.content, row.createdAt, row.role))
            val local = ContextEnvelope.local(cid, rows, "question").json
                .put("memory_status", "available").put("memories", JSONArray().put(JSONObject()
                    .put("id", "memory-one").put("text", "SYNTHETIC_KEY user preference").put("source_ids", JSONArray())
                    .put("updated_at", "2026-11-02")))
            val cache = ConversationContextCache(db)
            cache.replace("peer-one", cid, generation, local, listOf("SYNTHETIC_KEY"))
            val loaded = cache.read("peer-one", cid, generation)!!
            assertFalse(loaded.json.toString().contains("SYNTHETIC_KEY"))
            assertEquals(1, loaded.json.getJSONArray("memories").length())
            cache.replace("peer-one", cid, generation, local.put("memory_status", "disabled"))
            assertEquals(0, cache.read("peer-one", cid, generation)!!.json.getJSONArray("memories").length())
            assertNull(cache.read("peer-one", cid, UUID.randomUUID().toString()))
            assertNull(cache.read("peer-one", cid, generation))
            cache.replace("peer-one", cid, generation, local)
            db.saveDevice(DeviceInfo("other", "peer-two", "new ticket"), "phone")
            assertNull(cache.read("peer-two", cid, generation)); assertNull(cache.read("peer-one", cid, generation))
        } finally { db.close() }
    }

    @Test fun incompatibleOrAuthoritativeEnvelopeIsRejectedAndUtf8ClippingKeepsWholeEmoji() {
        val value = ContextEnvelope.local(cid, history(1), "hello").json
        for (bad in listOf(JSONObject(value.toString()).put("reference_data_only", false),
            JSONObject(value.toString()).put("version", 2), JSONObject(value.toString()).put("conversation_id", "other"))) {
            try { ContextEnvelope.parse(bad, cid); fail("Invalid envelope must fail") } catch (_: IllegalArgumentException) { }
        }
        assertEquals("鲸🐳", "鲸🐳好".takeUtf8(7))
        assertEquals("鲸", "鲸🐳好".takeUtf8(6))
    }

    @Test fun modelContextCanReadBeyondFiveHundredUiMessagesWithoutChangingUiWindow() {
        val db = CompanionDatabase(context, null)
        try {
            db.createConversation(cid)
            val sql = db.writableDatabase
            sql.beginTransaction()
            try {
                repeat(510) { i -> sql.execSQL("INSERT INTO messages(id,conversation_id,role,content,created_at,source) VALUES(?,?,?,?,?,?)",
                    arrayOf(UUID.randomUUID().toString(), cid, "user", "archived-$i", i.toString().padStart(5, '0'), "user")) }
                sql.setTransactionSuccessful()
            } finally { sql.endTransaction() }
            assertEquals(500, db.messages(cid).size)
            assertEquals(510, db.messages(cid, limit = null).size)
            assertEquals("archived-0", db.messages(cid, limit = null).first().content)
        } finally { db.close() }
    }
}
