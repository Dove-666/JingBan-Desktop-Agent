package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class ExecutionProcessUpgradeTest {
    @Test fun v4UpgradeRetainsCacheAndRefreshesSnapshot() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "ep.db"
        var cid = ""
        var queued = ""
        // Rebuild the two populated tables with their genuine v4 definitions;
        // the legacy Robolectric SQLite engine predates DROP COLUMN.
        CompanionDatabase(context, name).use { old ->
            old.saveDevice(DeviceInfo("Old desktop", "old-desktop", "old-ticket"), "old-phone")
            cid = old.createConversation()
            val mirror = JSONObject().put("entity", "message").put("id", UUID.randomUUID().toString()).put("conversation_id", cid)
                .put("data", JSONObject().put("role", "assistant").put("content", "Visible old history")
                    .put("created_at", "2026-11-01T00:00:00Z"))
            val conversation = JSONObject().put("entity", "conversation").put("id", cid).put("conversation_id", cid)
                .put("data", JSONObject().put("title", "Old conversation").put("created_at", "2026-11-01T00:00:00Z")
                    .put("updated_at", "2026-11-01T00:00:00Z"))
            old.stageSnapshotPage(JSONObject().put("items", JSONArray().put(conversation).put(mirror))
                .put("watermark", 41).put("has_more", false), true)
            queued = old.queueMessage(cid, "Preserved pending draft")
            old.setSetting("sync_ready", "1"); old.setSetting("sync_cursor", "41")
            old.setSetting("snapshot_cursor", "stale-partial-cursor")
            old.setDesktopAgentMode(cid, true)
            old.writableDatabase.apply {
                execSQL("""CREATE TABLE messages_v4(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,role TEXT NOT NULL,
                    content TEXT NOT NULL,created_at TEXT NOT NULL,source TEXT NOT NULL,request_status TEXT,reply_to TEXT,
                    image_id TEXT,image_error TEXT,agent_error_code TEXT,agent_activity TEXT,execution_mode TEXT)""")
                execSQL("""INSERT INTO messages_v4 SELECT id,conversation_id,role,content,created_at,source,
                    request_status,reply_to,image_id,image_error,agent_error_code,agent_activity,execution_mode FROM messages""")
                execSQL("DROP TABLE messages")
                execSQL("ALTER TABLE messages_v4 RENAME TO messages")
                execSQL("CREATE INDEX messages_conversation ON messages(conversation_id,created_at,id)")
                execSQL("""CREATE TABLE direct_turns_v4(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,
                    authority_id TEXT NOT NULL,user_id TEXT NOT NULL UNIQUE,assistant_id TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,user_at TEXT NOT NULL,reply TEXT,assistant_at TEXT,image_id TEXT,
                    state TEXT NOT NULL,error TEXT,import_request_id TEXT)""")
                execSQL("""INSERT INTO direct_turns_v4 SELECT id,conversation_id,authority_id,user_id,assistant_id,
                    text,user_at,reply,assistant_at,image_id,state,error,import_request_id FROM direct_turns""")
                execSQL("DROP TABLE direct_turns")
                execSQL("ALTER TABLE direct_turns_v4 RENAME TO direct_turns")
                execSQL("CREATE INDEX direct_turns_conversation ON direct_turns(conversation_id,user_at)")
            }
            old.writableDatabase.version = 4
        }
        try {
            CompanionDatabase(context, name).use { upgraded ->
                assertEquals(5, upgraded.writableDatabase.version)
                assertEquals("old-desktop", upgraded.device()!!.endpointId)
                assertEquals("old-ticket", upgraded.device()!!.ticket)
                assertEquals("queued", upgraded.operation(queued)!!.item.status)
                assertTrue(cid in upgraded.desktopAgentConversations())
                assertEquals(setOf("Visible old history", "Preserved pending draft"), upgraded.messages(cid).map { it.content }.toSet())
                assertFalse(upgraded.initialized())
                assertEquals("", upgraded.snapshotCursor())
                assertEquals(41L, upgraded.cursor())
                assertTrue(upgraded.messages(cid).all { it.execution == null })
                val turn = upgraded.beginDirectTurn(cid, "New actual request", null)
                assertEquals("running", upgraded.messages(cid).first { it.id == turn.userId }.execution!!.state)
            }
        } finally { context.deleteDatabase(name) }
    }
}
