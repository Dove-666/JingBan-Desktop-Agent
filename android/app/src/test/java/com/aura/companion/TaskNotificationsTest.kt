package com.aura.companion

import android.app.Application
import android.app.NotificationManager
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class TaskNotificationsTest {
    private val app = ApplicationProvider.getApplicationContext<Application>()
    private val manager = app.getSystemService(NotificationManager::class.java)
    @Before fun prepare() { manager.cancelAll(); shadowOf(manager).setNotificationsEnabled(true); TaskNotifications.channels(app) }

    @Test fun disabledNotificationPermissionDoesNotPostAnything() {
        shadowOf(manager).setNotificationsEnabled(false)
        assertFalse(TaskNotifications.allowed(app))
        TaskNotifications.result(app, TaskCompletion("request", "conversation", "completed"))
        TaskNotifications.monitoringStopped(app)
        assertEquals(0, shadowOf(manager).size())
    }

    @Test fun differentConversationsWithSameHashHaveDifferentPendingIntentIdentities() {
        val a = "2341335a-c13c-5c3e-bd3b-a59ac67288a1"
        val b = "ec852bc0-aa36-5903-8d45-d8395093f38b"
        assertEquals(a.hashCode(), b.hashCode())
        TaskNotifications.result(app, TaskCompletion("request-a", a, "completed"))
        val first = shadowOf(manager).getNotification("task:request-a", 1).contentIntent
        TaskNotifications.result(app, TaskCompletion("request-b", b, "failed"))
        val second = shadowOf(manager).getNotification("task:request-b", 1).contentIntent
        assertNotEquals(first, second)
        assertEquals(a, shadowOf(first).savedIntent.getStringExtra("conversation_id"))
        assertEquals(b, shadowOf(second).savedIntent.getStringExtra("conversation_id"))
        assertNotEquals(shadowOf(first).savedIntent.data, shadowOf(second).savedIntent.data)
    }

    @Test fun stoppedObservationNeverClaimsTheComputerTaskWasCancelled() {
        TaskNotifications.monitoringStopped(app)
        val notice = shadowOf(manager).getNotification("task-monitor-stopped", 2)
        assertEquals("后台任务提醒已暂停", notice.extras.getString("android.title"))
        assertTrue(notice.extras.getString("android.text")!!.contains("电脑任务仍会继续"))
    }
}
