package com.aura.companion

import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Opt-in, task-duration sync only. It never starts a computer task or replays an action. */
class TaskSyncService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var session: CompanionSession? = null
    private var monitoring = false
    private var paused = false

    override fun onBind(intent: Intent?): IBinder? = null
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val active = CompanionSession.current()
        if (active == null || !getSharedPreferences("appearance", 0).getBoolean("task_notifications", false) ||
            !TaskNotifications.allowed(this)) {
            stopSelf(); return START_NOT_STICKY
        }
        TaskNotifications.channels(this)
        session = active
        try { ServiceCompat.startForeground(this, TaskNotifications.SYNC_ID, TaskNotifications.ongoing(this),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC) }
        catch (_: Exception) { paused = true; stopSelf(); return START_NOT_STICKY }
        if (!monitoring) {
            monitoring = true; session = active
            active.serviceStarted()
            var seen = active.serviceStartSequence
            scope.launch {
                active.monitor.collect { value ->
                    for (event in value.events) if (event.sequence > seen) {
                        seen = event.sequence
                        if (!active.foreground) TaskNotifications.result(this@TaskSyncService, event.completion)
                    }
                    if (value.pending == 0) stopSelf()
                }
            }
            // A missing/locked/sleeping peer must not retain a phone service indefinitely.
            // This ends observation only; it never cancels the computer's task.
            scope.launch {
                var backgroundSeconds = 0
                while (true) {
                    delay(1_000)
                    if (!TaskNotifications.allowed(this@TaskSyncService) ||
                        !getSharedPreferences("appearance", 0).getBoolean("task_notifications", false)) {
                        stopSelf(); break
                    }
                    backgroundSeconds = if (active.foreground) 0 else backgroundSeconds + 1
                    if (backgroundSeconds >= 1_800) { pauseMonitoring(); break }
                }
            }
        }
        return START_NOT_STICKY
    }

    private fun pauseMonitoring() {
        paused = true
        TaskNotifications.monitoringStopped(this)
        stopSelf()
    }

    override fun onTimeout(startId: Int, fgsType: Int) { pauseMonitoring() }
    override fun onDestroy() {
        scope.cancel()
        session?.serviceStopped(paused)
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }
}
