package com.aura.companion

import com.aura.companion.data.AppState
import com.aura.companion.data.ExecutionProcess
import com.aura.companion.data.Message
import com.aura.companion.data.OutboxItem
import com.aura.companion.ui.MessageRole
import com.aura.companion.ui.MessageStatus
import com.aura.companion.ui.UiExecution
import com.aura.companion.ui.UiExecutionStep
import com.aura.companion.ui.UiMessage

/** One presentation record per real message, plus an assistant slot for a request in progress. */
internal fun conversationPresentation(data: AppState): List<UiMessage> {
    val outbox = data.outbox.filter { !it.hidden && it.status != "discarded" }
    val images = data.images.associateBy { it.id }
    fun queued(message: Message) = outbox.firstOrNull { it.messageId == message.id || it.requestId == message.id }
    fun failureDetail(message: Message, queued: OutboxItem?): String = when (message.imageError) {
        "image_missing" -> "图片已从电脑删除，请重新选择图片后发送。"
        "image_unavailable" -> "电脑暂时无法读取这张图片，请重新选择后发送。"
        "vision_rejected" -> "图文请求未被模型接口接受，请确认所选模型支持图片并检查接口配置。"
        else -> message.agentError ?: message.localError ?: queued?.error ?: ""
    }
    val base = data.messages.associate { message ->
        val operation = queued(message)
        val status = when (message.localState ?: message.requestStatus ?: operation?.status) {
            "queued" -> MessageStatus.QUEUED
            "sending", "generating" -> MessageStatus.SENDING
            "pending" -> MessageStatus.PENDING
            "failed", "cancelled", "interrupted" -> MessageStatus.FAILED
            "unknown", "needs_attention" -> MessageStatus.UNCERTAIN
            else -> MessageStatus.SENT
        }
        message.id to UiMessage(message.id, when (message.role) {
            "assistant" -> MessageRole.ASSISTANT
            "user" -> MessageRole.USER
            else -> MessageRole.SYSTEM
        }, message.content, status,
            detail = failureDetail(message, operation).ifEmpty { message.agentActivity.orEmpty() },
            attachmentUri = message.attachmentId?.let { images[it]?.localPath },
            retryable = operation?.method != "chat.import" && operation?.status in setOf("queued", "unknown", "failed"),
            attachmentId = message.attachmentId,
            retryOnPhone = operation?.canSendOnPhone == true && data.directApi.available,
            desktopAgent = message.executionMode == "agent")
    }.toMutableMap()
    val synthetic = mutableMapOf<String, UiMessage>()
    for (request in data.messages.filter { it.role == "user" && it.execution != null }) {
        val process = request.execution ?: continue
        val requestUi = base.getValue(request.id)
        val linked = data.messages.firstOrNull { it.role == "assistant" && it.replyTo == request.id }
        val detail = failureDetail(request, queued(request))
        if (linked != null) {
            val reply = base.getValue(linked.id)
            base[linked.id] = reply.copy(execution = process.presentation(), desktopAgent = requestUi.desktopAgent,
                detail = detail.ifEmpty { failureDetail(linked, queued(linked)) })
        } else {
            synthetic[request.id] = UiMessage("process:${request.id}", MessageRole.ASSISTANT, "",
                status = requestUi.status, detail = detail, desktopAgent = requestUi.desktopAgent,
                execution = process.presentation())
        }
        // The process and actual failure now appear once on the assistant side.
        base[request.id] = requestUi.copy(status = MessageStatus.SENT, detail = "", retryable = false,
            retryOnPhone = false, desktopAgent = false)
    }
    return buildList {
        for (message in data.messages) {
            add(base.getValue(message.id))
            synthetic[message.id]?.let(::add)
        }
    }
}

private fun ExecutionProcess.presentation() = UiExecution(state, startedAt, durationMs,
    events.map { UiExecutionStep(it.id, it.kind, it.label, it.status, it.elapsedMs) }, omittedCount)
