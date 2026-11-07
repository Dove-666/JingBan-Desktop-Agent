package com.aura.companion.ui

private val stoppedReplyTemplates = setOf(
    "任务已停止；已经完成的操作会保留，未自动重试。",
    "任务已停止，已经完成的操作会保留，未自动重试。",
)

/** Presentation only: the archived reply and its outcome are never rewritten. */
internal fun displayedChatReply(message: UiMessage): String {
    if (message.role != MessageRole.ASSISTANT) return message.content
    val content = message.content.trim()
    return when {
        message.execution?.state == "cancelled" && content in stoppedReplyTemplates -> ""
        message.execution?.state in setOf("needs_user", "failed") &&
            (content.startsWith("任务尚未确认完成。") ||
             content.startsWith("任务尚未通过验收：存在未完成步骤、工具失败或产物校验问题。")) ->
                "未完成：执行结果检查未通过。"
        else -> message.content
    }
}

/** Keep original terminal explanations available beneath the disclosure. */
internal fun executionMessageDetail(message: UiMessage): String = buildList {
    if (message.detail.isNotBlank()) add(message.detail)
    if (message.content.isNotBlank() && displayedChatReply(message) != message.content) add(message.content)
}.distinct().joinToString("\n\n")

internal fun executionStateLabel(state: String): String = when (state) {
    "running" -> "进行中"
    "completed" -> "已完成"
    "cancelled" -> "已停止"
    "interrupted" -> "已中断"
    else -> "未完成"
}

/** Count observed operations only; phase/summary updates are not extra actions. */
internal fun UiExecution.operationEvents(): List<UiExecutionStep> = events.filter {
    it.kind == "tool" || it.kind == "computer"
}
