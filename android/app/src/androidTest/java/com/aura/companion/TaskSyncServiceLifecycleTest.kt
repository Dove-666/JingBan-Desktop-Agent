package com.aura.companion

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.content.pm.PackageManager
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aura.companion.data.*
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID
import java.io.File
import java.io.IOException

/** Real Android service/notification acceptance with a private, in-memory, non-network repository. */
@RunWith(AndroidJUnit4::class)
class TaskSyncServiceLifecycleTest {
    @Test fun backgroundCompletionPostsOneResultStopsServiceAndReusesSessionAfterRecreation() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val app = ApplicationProvider.getApplicationContext<Application>()
        assertTrue("This isolated-device acceptance requires POST_NOTIFICATIONS to be pre-granted; the test preserves its state",
            Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(app, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
        val draftFiles = listOf("conversation-drafts.json", "conversation-drafts.json.bak", "conversation-drafts.json.new")
            .associate { name -> File(app.filesDir, name).let { it to if (it.exists()) it.readBytes() else null } }
        val preferences = app.getSharedPreferences("appearance", 0)
        val old = preferences.all
        val cid = UUID.randomUUID().toString()
        val mid = UUID.randomUUID().toString()
        val field = CompanionSession::class.java.getDeclaredField("instance").apply { isAccessible = true }
        val previousSession = field.get(null)
        var session: CompanionSession? = null
        var scenario: ActivityScenario<MainActivity>? = null
        val database = CompanionDatabase(app, null)
        val rpc = FixtureRpc(cid, mid)
        val vault = object : ApiConfigVault {
            override fun load(): DirectApiConfig? = null
            override fun save(config: DirectApiConfig) = error("No model configuration belongs in this test")
            override fun clear() = Unit
        }
        val repository = AppRepository(app, rpc, database, vault,
            object : DirectChatGateway { override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = error("No model calls") })
        try {
            preferences.edit().putBoolean("auto_connect", true).putBoolean("task_notifications", true).commit()
            database.saveDevice(DeviceInfo("测试电脑", "fixture-desktop", "fixture-ticket"), "fixture-phone")
            database.stageSnapshotPage(rpc.snapshot(), true)
            database.setSetting("selected_conversation", cid)
            runBlocking { withTimeout(5_000) { repository.reconnect() } }
            val fixtureSession = CompanionSession(app, repository)
            session = fixtureSession
            field.set(null, fixtureSession)
            val runningActivity = ActivityScenario.launch(MainActivity::class.java)
            scenario = runningActivity
            val manager = app.getSystemService(NotificationManager::class.java)
            await { manager.activeNotifications.any { it.id == TaskNotifications.SYNC_ID } }
            preferences.edit().putBoolean("task_notifications", false).commit()
            instrumentation.runOnMainSync { fixtureSession.settingsChanged() }
            await { manager.activeNotifications.none { it.id == TaskNotifications.SYNC_ID } }
            preferences.edit().putBoolean("task_notifications", true).commit()
            instrumentation.runOnMainSync { fixtureSession.settingsChanged() }
            await { manager.activeNotifications.any { it.id == TaskNotifications.SYNC_ID } }
            runningActivity.recreate()
            assertSame(fixtureSession, CompanionSession.current())
            assertSame(repository, CompanionSession.current()!!.repository)
            runningActivity.moveToState(Lifecycle.State.CREATED)
            await { !fixtureSession.foreground }
            rpc.offline = true
            await { repository.state.value.connectionState == ConnectionState.ERROR }
            assertTrue(manager.activeNotifications.any { it.id == TaskNotifications.SYNC_ID })
            assertTrue(manager.activeNotifications.none { it.tag == "task:$mid" })
            runBlocking { repository.newConversation() }
            val pollsBefore = rpc.pulls
            rpc.offline = false
            rpc.complete = true
            await { manager.activeNotifications.any { it.tag == "task:$mid" } }
            val result = manager.activeNotifications.single { it.tag == "task:$mid" }
            assertEquals("电脑任务已完成", result.notification.extras.getString("android.title"))
            assertFalse(result.notification.extras.toString().contains("PRIVATE_TASK_TEXT"))
            assertTrue(rpc.pulls > pollsBefore)
            await { manager.activeNotifications.none { it.id == TaskNotifications.SYNC_ID } }
            val postTime = result.postTime
            SystemClock.sleep(2_500)
            assertEquals(postTime, manager.activeNotifications.single { it.tag == "task:$mid" }.postTime)
            manager.cancel("task:$mid", 1)
            runningActivity.moveToState(Lifecycle.State.RESUMED)
            assertSame(fixtureSession, CompanionSession.current())
        } finally {
            scenario?.close()
            app.stopService(Intent(app, TaskSyncService::class.java))
            instrumentation.waitForIdleSync()
            session?.let { runBlocking { it.close() } } ?: runBlocking { repository.close() }
            field.set(null, previousSession)
            app.getSystemService(NotificationManager::class.java).apply {
                cancel("task:$mid", 1); cancel(TaskNotifications.SYNC_ID)
            }
            val restore = preferences.edit()
            for (key in listOf("auto_connect", "task_notifications")) {
                if (old.containsKey(key)) restore.putBoolean(key, old[key] as Boolean) else restore.remove(key)
            }
            restore.commit()
            for ((file, bytes) in draftFiles) {
                if (bytes == null) file.delete() else file.writeBytes(bytes)
            }
        }
    }

    private fun await(condition: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15_000
        while (!condition()) {
            assertTrue("Condition did not arrive before deadline", SystemClock.elapsedRealtime() < deadline)
            SystemClock.sleep(50)
        }
    }

    private class FixtureRpc(private val cid: String, private val mid: String) : RpcGateway {
        @Volatile var complete = false
        @Volatile var pulls = 0
        @Volatile var offline = false
        override val endpointId = "fixture-phone"
        override val connectionType = "direct"
        override suspend fun connect(ticket: String) { if (offline) throw IOException("Controlled offline fixture") }
        override suspend fun close() = Unit
        private fun record(done: Boolean) = JSONObject().put("id", mid).put("conversation_id", cid).put("role", "user")
            .put("content", "PRIVATE_TASK_TEXT").put("source", "user").put("created_at", "2026-11-07T00:00:00Z")
            .put("request_status", if (done) "complete" else "pending").put("execution_mode", "agent")
        fun snapshot() = JSONObject().put("items", JSONArray().put(JSONObject().put("entity", "conversation").put("id", cid)
            .put("conversation_id", cid).put("data", JSONObject().put("id", cid).put("title", "通知验收")
                .put("created_at", "2026-11-07T00:00:00Z").put("updated_at", "2026-11-07T00:00:00Z")))
            .put(JSONObject().put("entity", "message").put("id", mid).put("conversation_id", cid).put("data", record(false))))
            .put("watermark", 1).put("next_cursor", "").put("has_more", false)
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            if (offline) throw IOException("Controlled offline fixture")
            val result = when (method) {
                "status" -> JSONObject().put("protocol_version", 1).put("capabilities", JSONArray().put("agent.send"))
                    .put("agent_available", true).put("agent_busy", !complete).put("agent_conversation_id", cid)
                "sync.snapshot" -> snapshot()
                "sync.pull" -> {
                    pulls++
                    val since = params.getLong("since")
                    val rows = JSONArray()
                    if (complete && since < 2) rows.put(JSONObject().put("seq", 2).put("entity", "message").put("id", mid)
                        .put("conversation_id", cid).put("data", record(true)))
                    JSONObject().put("changes", rows).put("next_since", if (rows.length() > 0) 2 else since).put("has_more", false)
                }
                "image.list" -> JSONObject().put("items", JSONArray()).put("next_offset", 0).put("has_more", false)
                else -> error("Unexpected fixture method: $method")
            }
            return JSONObject().put("id", requestId).put("ok", true).put("result", result)
        }
    }
}
