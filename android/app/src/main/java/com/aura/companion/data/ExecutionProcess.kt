package com.aura.companion.data

import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.temporal.ChronoUnit

/** Public operation records only; model reasoning, tool arguments and context are never accepted. */
data class ExecutionStep(val id: String, val kind: String, val label: String,
    val status: String, val elapsedMs: Long)

data class ExecutionProcess(val version: Int = 1, val state: String, val startedAt: String,
    val durationMs: Long, val events: List<ExecutionStep>, val omittedCount: Int = 0)

internal const val MAX_EXECUTION_MS = 31_536_000_000L
private val PROCESS_STATES = setOf("running", "completed", "failed", "cancelled", "needs_user", "interrupted")
private val PROCESS_KINDS = setOf("progress", "tool", "computer", "summary")
private val PROCESS_STATUSES = setOf("running", "completed", "failed")
private val PROCESS_EXTRA_LABELS = setOf("开始处理请求", "回复已完成", "本次请求未能完成", "本次请求已停止",
    "等待你完成当前步骤", "任务未完成", "上次请求意外中断", "任务分派说明（模型公开回复）", "任务分派说明（直接请求）", "正在请求模型…")
private val PROCESS_START = Regex("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,6})?(?:Z|[+-]\\d{2}:\\d{2})")
private val PROCESS_ID = Regex("step-[0-9]{1,10}")

private fun JSONObject.strictLong(key: String, maximum: Long): Long? {
    val value = opt(key)
    if (value !is Int && value !is Long) return null
    return (value as Number).toLong().takeIf { it in 0..maximum }
}

/** Canonicalizes an untrusted synced JSON object and drops every non-public field. */
internal fun parseExecutionProcess(value: Any?): ExecutionProcess? {
    val source = when (value) {
        is JSONObject -> value
        is String -> if (value.length <= 64_000) runCatching { JSONObject(value) }.getOrNull() else null
        else -> null
    } ?: return null
    if (source.toString().length > 64_000 || source.strictLong("version", 1) != 1L) return null
    val state = source.opt("state") as? String ?: return null
    val started = source.opt("started_at") as? String ?: return null
    if (state !in PROCESS_STATES || !PROCESS_START.matches(started) || runCatching { Instant.parse(started) }.isFailure) return null
    val duration = source.strictLong("duration_ms", MAX_EXECUTION_MS) ?: return null
    val omitted = if (source.has("omitted_count")) source.strictLong("omitted_count", Int.MAX_VALUE.toLong())?.toInt()
        ?: return null else 0
    val input = source.optJSONArray("events") ?: return null
    if (input.length() > 80) return null
    val seen = mutableSetOf<String>()
    val events = buildList {
        for (index in 0 until input.length()) {
            val item = input.optJSONObject(index) ?: continue
            val id = item.opt("id") as? String ?: continue
            val kind = item.opt("kind") as? String ?: continue
            val label = item.opt("label") as? String ?: continue
            val status = item.opt("status") as? String ?: continue
            val elapsed = item.strictLong("elapsed_ms", duration) ?: continue
            if (!PROCESS_ID.matches(id) || !seen.add(id) || kind !in PROCESS_KINDS || status !in PROCESS_STATUSES ||
                (validAgentActivity(label) == null && label !in PROCESS_EXTRA_LABELS)) continue
            add(ExecutionStep(id, kind, label, status, elapsed))
        }
    }
    return ExecutionProcess(state = state, startedAt = started, durationMs = duration, events = events, omittedCount = omitted)
}

internal fun ExecutionProcess.canonicalJson(): String = JSONObject().put("version", version).put("state", state)
    .put("started_at", startedAt).put("duration_ms", durationMs).put("omitted_count", omittedCount)
    .put("events", JSONArray(events.map { JSONObject().put("id", it.id).put("kind", it.kind).put("label", it.label)
        .put("status", it.status).put("elapsed_ms", it.elapsedMs) })).toString()

internal fun newDirectExecution(): ExecutionProcess = ExecutionProcess(state = "running",
    startedAt = Instant.now().truncatedTo(ChronoUnit.MILLIS).toString(), durationMs = 0,
    events = listOf(ExecutionStep("step-0", "progress", "正在请求模型…", "running", 0)))

internal fun finishDirectExecution(current: ExecutionProcess?, state: String): ExecutionProcess? {
    current ?: return null // An old request with no recorded phases must not gain invented history.
    val duration = runCatching { Duration.between(Instant.parse(current.startedAt), Instant.now()).toMillis() }
        .getOrDefault(current.durationMs).coerceIn(current.durationMs, MAX_EXECUTION_MS)
    val label = when (state) {
        "completed" -> "回复已完成"
        "cancelled" -> "本次请求已停止"
        "interrupted" -> "上次请求意外中断"
        else -> "本次请求未能完成"
    }
    return current.copy(state = state, durationMs = duration,
        events = current.events.map { if (it.status == "running") it.copy(status = if (state == "completed") "completed" else "failed") else it } +
            ExecutionStep("step-${current.events.size}", "summary", label, if (state == "completed") "completed" else "failed", duration))
}
