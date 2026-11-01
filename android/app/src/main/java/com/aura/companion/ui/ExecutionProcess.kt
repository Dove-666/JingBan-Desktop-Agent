package com.aura.companion.ui

import android.animation.ValueAnimator
import android.os.SystemClock
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive

/** A compact history of real phases and tools, kept separate from the answer. */
@Composable
internal fun ExecutionProcess(process: UiExecution, modifier: Modifier = Modifier, identity: String? = null) {
    val running = process.state == "running"
    var expanded by rememberSaveable(process.startedAt) { mutableStateOf(running) }
    var previouslyRunning by rememberSaveable(process.startedAt) { mutableStateOf(running) }
    var showAllRunningSteps by rememberSaveable(process.startedAt) { mutableStateOf(false) }
    var inViewport by remember { mutableStateOf(false) }
    // This effect runs only on a lifecycle transition, never on an ordinary sync update.
    // The user can reopen a completed trace and subsequent updates leave it open.
    LaunchedEffect(process.startedAt, running) {
        if (previouslyRunning && !running) expanded = false
        previouslyRunning = running
    }

    val lifecycle = LocalLifecycleOwner.current.lifecycle
    var foreground by remember(lifecycle) {
        mutableStateOf(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
    }
    var motion by remember { mutableStateOf(ValueAnimator.areAnimatorsEnabled()) }
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, _ ->
            foreground = lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
            if (foreground) motion = ValueAnimator.areAnimatorsEnabled()
        }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }

    var elapsed by remember(process.startedAt) { mutableLongStateOf(process.durationMs) }
    val elapsedAnchor = remember(process.startedAt) {
        longArrayOf(process.durationMs, SystemClock.elapsedRealtime())
    }
    LaunchedEffect(process.startedAt, running, foreground, inViewport, process.durationMs) {
        if (!running) {
            elapsed = process.durationMs
        } else if (foreground && inViewport) {
            val now = SystemClock.elapsedRealtime()
            // Retain the anchor while hidden: no background ticks, but elapsed
            // time is still correct when the user returns to the visible row.
            if (process.durationMs > elapsedAnchor[0] + now - elapsedAnchor[1]) {
                elapsedAnchor[0] = process.durationMs
                elapsedAnchor[1] = now
            }
            while (isActive) {
                elapsed = maxOf(elapsed, elapsedAnchor[0] + SystemClock.elapsedRealtime() - elapsedAnchor[1])
                delay(1000)
            }
        }
    }
    val title = when (process.state) {
        "running" -> "执行中"
        "completed" -> "已完成"
        "cancelled" -> "已停止"
        "needs_user" -> "等待确认"
        "interrupted" -> "已中断"
        else -> "未完成"
    }
    val duration = executionDuration(if (running) elapsed else process.durationMs)
    val current = process.events.lastOrNull { it.status == "running" }?.label
        ?: process.events.lastOrNull()?.label
    val colors = MaterialTheme.colorScheme
    val statusColor = if (process.state in setOf("failed", "interrupted")) colors.error else colors.primary

    Column(modifier.onGloballyPositioned { inViewport = !it.boundsInWindow().isEmpty }
        .testTag("execution-process")) {
        Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
            .semantics(mergeDescendants = true) {
                contentDescription = "执行过程"
                stateDescription = "${if (expanded) "已展开" else "已收起"}，$title，用时$duration"
            }
            .testTag("execution-toggle")
            .clickable(role = Role.Button,
                onClickLabel = if (expanded) "收起执行过程" else "展开执行过程") { expanded = !expanded }
            .padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(9.dp)) {
            ProcessGlyph(running = running && foreground && inViewport, animate = motion,
                state = process.state, modifier = Modifier.size(19.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                if (identity != null) Text(identity, color = colors.onSurfaceVariant,
                    style = MaterialTheme.typography.labelSmall)
                Text("$title · $duration", color = colors.onSurfaceVariant,
                    style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.Medium)
                if (running && !expanded && !current.isNullOrBlank()) {
                    Text(current, color = colors.onSurfaceVariant,
                        style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            Icon(if (expanded) Icons.Default.KeyboardArrowUp else Icons.Default.KeyboardArrowDown,
                contentDescription = null, tint = colors.onSurfaceVariant, modifier = Modifier.size(19.dp))
        }
        if (expanded) {
            val visible = if (running && !showAllRunningSteps) process.events.takeLast(6) else process.events
            Column(Modifier.fillMaxWidth().padding(start = 9.dp, top = 3.dp, bottom = 8.dp)
                .testTag("execution-events")) {
                if (running && !showAllRunningSteps && process.events.size > visible.size) {
                    TextButton({ showAllRunningSteps = true }, Modifier.heightIn(min = 48.dp)) {
                        Text("查看全部 ${process.events.size} 步", style = MaterialTheme.typography.bodySmall)
                    }
                }
                if (process.omittedCount > 0) {
                    Text("另有 ${process.omittedCount} 条较早记录", Modifier.padding(start = 20.dp, bottom = 8.dp),
                        color = colors.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
                }
                visible.forEachIndexed { index, event ->
                    val ink = if (event.status == "failed") colors.error else if (event.status == "running") statusColor
                        else colors.onSurfaceVariant
                    val status = when (event.status) {
                        "completed" -> "已完成"
                        "failed" -> "失败"
                        "running" -> "执行中"
                        "cancelled" -> "已停止"
                        else -> "已记录"
                    }
                    val offset = executionOffset(event.elapsedMs)
                    Row(Modifier.fillMaxWidth().testTag("execution-step-${event.id}")
                        .semantics(mergeDescendants = true) {
                            stateDescription = "$status，任务开始后 ${offset.removePrefix("+")}"
                        }.drawBehind {
                        val x = 9.dp.toPx()
                        if (index < visible.lastIndex) drawLine(colors.outlineVariant.copy(alpha = .7f),
                            Offset(x, 20.dp.toPx()), Offset(x, size.height), strokeWidth = 1.dp.toPx())
                    },
                        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Box(Modifier.width(18.dp).padding(top = 3.dp), contentAlignment = Alignment.TopCenter) {
                            if (event.status == "running") Canvas(Modifier.size(14.dp)) {
                                drawCircle(ink, style = androidx.compose.ui.graphics.drawscope.Stroke(1.5.dp.toPx()))
                                drawCircle(ink, radius = 2.dp.toPx())
                            } else Icon(when (event.status) {
                                "completed" -> Icons.Default.Check
                                "failed", "cancelled" -> Icons.Default.Close
                                else -> Icons.Default.Info
                            }, contentDescription = null, tint = ink, modifier = Modifier.size(14.dp))
                        }
                        Text(event.label, color = if (event.status == "failed") colors.error else colors.onSurfaceVariant,
                            style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.weight(1f).padding(bottom = 12.dp))
                        Text(offset, color = colors.onSurfaceVariant,
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(bottom = 12.dp))
                    }
                }
            }
        }
    }
}

/** Water-light petals animate only while active; system reduced motion keeps a still glyph. */
@Composable
private fun ProcessGlyph(running: Boolean, animate: Boolean, state: String, modifier: Modifier) {
    val colors = MaterialTheme.colorScheme
    if (state != "running") {
        Icon(when (state) {
            "completed" -> Icons.Default.Check
            "failed", "interrupted" -> Icons.Default.Close
            else -> Icons.Default.Info
        }, contentDescription = null,
            tint = if (state in setOf("failed", "interrupted")) colors.error else colors.primary,
            modifier = modifier)
    } else {
        val angle = if (running && animate) {
            val transition = rememberInfiniteTransition(label = "execution-water-light")
            val rotation by transition.animateFloat(0f, 360f,
                animationSpec = infiniteRepeatable(tween(1800, easing = LinearEasing), RepeatMode.Restart),
                label = "execution-water-light-angle")
            rotation
        } else 0f
        Canvas(modifier.testTag("execution-running-icon")) {
            rotate(angle) {
                for (index in 0 until 6) rotate(index * 60f) {
                    drawOval(colors.primary.copy(alpha = .35f + index * .11f),
                        topLeft = Offset(size.width * .43f, size.height * .04f),
                        size = androidx.compose.ui.geometry.Size(size.width * .14f, size.height * .27f))
                }
            }
            drawCircle(colors.primary.copy(alpha = .65f), size.width * .075f)
        }
    }
}

internal fun executionDuration(milliseconds: Long): String {
    val seconds = milliseconds.coerceAtLeast(0) / 1000
    return when {
        seconds < 60 -> "$seconds 秒"
        seconds < 3600 -> "${seconds / 60} 分 ${seconds % 60} 秒"
        else -> "${seconds / 3600} 小时 ${(seconds % 3600) / 60} 分"
    }
}

/** Event timestamps are offsets from task start, never invented per-tool durations. */
internal fun executionOffset(milliseconds: Long): String {
    val seconds = milliseconds.coerceAtLeast(0) / 1000
    val minute = (seconds / 60).toString().padStart(2, '0')
    val second = (seconds % 60).toString().padStart(2, '0')
    return "+$minute:$second"
}
