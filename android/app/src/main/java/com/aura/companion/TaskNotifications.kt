package com.aura.companion

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat

internal object TaskNotifications {
    const val SYNC_ID = 7310
    private const val SYNC_CHANNEL = "desktop_task_sync"
    private const val RESULT_CHANNEL = "desktop_task_results"

    fun allowed(context: Context): Boolean = NotificationManagerCompat.from(context).areNotificationsEnabled() &&
        (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)

    fun channels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(SYNC_CHANNEL, "电脑任务连接", NotificationManager.IMPORTANCE_LOW)
            .apply { description = "仅在观察电脑任务时维持连接"; setShowBadge(false) })
        manager.createNotificationChannel(NotificationChannel(RESULT_CHANNEL, "电脑任务结果", NotificationManager.IMPORTANCE_DEFAULT)
            .apply { description = "电脑任务完成或需要处理时提醒" })
    }

    private fun builder(context: Context, channel: String, conversation: String? = null): NotificationCompat.Builder {
        val launch = Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        if (conversation != null) launch.putExtra("conversation_id", conversation)
            .setData(Uri.Builder().scheme("jingban").authority("conversation").appendPath(conversation).build())
        val content = PendingIntent.getActivity(context, conversation?.hashCode() ?: 0, launch,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(context, channel).setSmallIcon(R.drawable.ic_task_notification)
            .setContentIntent(content).setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .setPublicVersion(NotificationCompat.Builder(context, channel).setSmallIcon(R.drawable.ic_task_notification)
                .setContentTitle("鲸伴").setContentText("电脑任务有新进展").build())
    }

    fun ongoing(context: Context): Notification = builder(context, SYNC_CHANNEL)
        .setContentTitle("正在等待电脑任务")
        .setContentText("完成后提醒，任务结束后自动关闭连接")
        .setOnlyAlertOnce(true).setOngoing(true).setCategory(NotificationCompat.CATEGORY_SERVICE).build()

    fun result(context: Context, completion: TaskCompletion) {
        if (!allowed(context)) return
        val title = when (completion.outcome) {
            "completed" -> "电脑任务已完成"
            "needs_user" -> "电脑任务需要你处理"
            "cancelled", "stopped" -> "电脑任务已停止"
            else -> "电脑任务未完成"
        }
        try {
            NotificationManagerCompat.from(context).notify("task:${completion.requestId}", 1,
                builder(context, RESULT_CHANNEL, completion.conversationId).setContentTitle(title)
                    .setContentText("点击查看对话与执行记录").setAutoCancel(true).build())
        } catch (_: SecurityException) { /* Permission can be revoked after allowed(). */ }
    }

    fun monitoringStopped(context: Context) {
        if (!allowed(context)) return
        try {
            NotificationManagerCompat.from(context).notify("task-monitor-stopped", 2,
                builder(context, RESULT_CHANNEL).setContentTitle("后台任务提醒已暂停")
                    .setContentText("电脑任务仍会继续，打开鲸伴后可重新连接查看").setAutoCancel(true).build())
        } catch (_: SecurityException) { /* Permission can be revoked after allowed(). */ }
    }
}
