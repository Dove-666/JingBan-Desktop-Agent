package com.aura.companion

import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.aura.companion.data.AppRepository
import com.aura.companion.data.AppState
import com.aura.companion.data.ConnectionState
import com.aura.companion.data.OutboxItem
import com.aura.companion.transport.IrohRpcClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChangedBy
import kotlinx.coroutines.launch
import kotlinx.coroutines.cancel
import kotlinx.coroutines.withContext

internal data class TaskMonitorEvent(val sequence: Long, val completion: TaskCompletion)
internal data class TaskMonitorState(val pending: Int = 0, val events: List<TaskMonitorEvent> = emptyList())

internal data class TaskPhase(val id: String, val conversation: String, val request: String?, val local: String?, val execution: String?)
internal data class TaskMonitorStamp(val authority: String?, val connection: ConnectionState, val syncedAt: Long?,
    val busyConversation: String?, val operations: List<OutboxItem>, val phases: List<TaskPhase>, val conversations: Set<String>)

internal fun AppState.monitorStamp() = TaskMonitorStamp(device?.id, connectionState, lastSyncedAt,
    desktopAgentConversationId.takeIf { desktopAgentBusy }, outbox.filter { it.method == "agent.send" },
    messages.filter { it.role == "user" && it.executionMode == "agent" }.map {
        TaskPhase(it.id, it.conversationId, it.requestStatus, it.localState, it.execution?.state)
    }, conversations.map { it.id }.toSet())

/** One transport per app process. A task service can outlive an Activity without owning its UI. */
internal class CompanionSession internal constructor(private val context: Context, val repository: AppRepository) {
    private val preferences = context.getSharedPreferences("appearance", 0)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val tracker = TaskCompletionTracker()
    private val notificationsEnabled = MutableStateFlow(preferences.getBoolean("task_notifications", false))
    private val mutableMonitor = MutableStateFlow(TaskMonitorState())
    val monitor: StateFlow<TaskMonitorState> = mutableMonitor
    var foreground = false; private set
    private var serviceRequested = false
    private var pausedUntilForeground = false
    private var eventSequence = 0L
    var serviceStartSequence = 0L; private set
    private val sync = ForegroundSyncLoop(scope, repository.state,
        { preferences.getBoolean("auto_connect", true) },
        reconnect = { repository.reconnect(silent = true) }, poll = { repository.poll() },
        disconnect = { repository.disconnect() })

    init {
        scope.launch {
            combine(repository.state, notificationsEnabled) { state, enabled -> state to enabled }
                .distinctUntilChangedBy { (state, enabled) -> if (enabled) state.monitorStamp() else null }
                .collect { (state, enabled) ->
                if (!enabled || state.device == null) {
                    tracker.clear()
                    mutableMonitor.value = TaskMonitorState()
                    reconcile()
                    return@collect
                }
                val messages = withContext(Dispatchers.IO) {
                    readTaskStatuses(repository.database, tracker.messageIds, state.outbox, tracker.requestIds)
                }
                val finished = tracker.observe(state, messages)
                val events = mutableMonitor.value.events + finished.map { TaskMonitorEvent(++eventSequence, it) }
                mutableMonitor.value = TaskMonitorState(tracker.pendingCount, events.takeLast(32))
                reconcile()
            }
        }
    }

    fun setForeground(value: Boolean) {
        foreground = value
        if (value) pausedUntilForeground = false
        sync.setForeground(value)
        reconcile()
    }

    fun settingsChanged() {
        pausedUntilForeground = false
        notificationsEnabled.value = preferences.getBoolean("task_notifications", false)
        reconcile()
    }

    private fun reconcile() {
        if (!preferences.getBoolean("task_notifications", false) ||
            !TaskNotifications.allowed(context) || !preferences.getBoolean("auto_connect", true) ||
            repository.state.value.connectionState == ConnectionState.UNAUTHORIZED) {
            if (serviceRequested) context.stopService(Intent(context, TaskSyncService::class.java))
            serviceRequested = false
            sync.setBackgroundTaskActive(false)
            return
        }
        if (foreground && !pausedUntilForeground && monitor.value.pending > 0 && !serviceRequested) {
            serviceStartSequence = eventSequence
            serviceRequested = true
            try { ContextCompat.startForegroundService(context, Intent(context, TaskSyncService::class.java)) }
            catch (_: Exception) { serviceRequested = false; pausedUntilForeground = true }
        }
    }

    fun serviceStarted() { sync.setBackgroundTaskActive(true) }
    fun serviceStopped(pause: Boolean) {
        serviceRequested = false
        pausedUntilForeground = pause
        sync.setBackgroundTaskActive(false)
        if (!pause && foreground) reconcile()
    }

    internal suspend fun close() {
        sync.close()
        scope.cancel()
        repository.close()
    }

    companion object {
        @Volatile private var instance: CompanionSession? = null
        fun get(context: Context): CompanionSession = instance ?: synchronized(this) {
            instance ?: CompanionSession(context.applicationContext,
                AppRepository(context.applicationContext, IrohRpcClient(context.applicationContext))).also { instance = it }
        }
        fun current(): CompanionSession? = instance
    }
}
