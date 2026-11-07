package com.aura.companion.ui

import org.junit.Assert.*
import org.junit.Test

class ChatPresentationTest {
    private fun message(state: String? = null, text: String, detail: String = "") = UiMessage(
        "reply", MessageRole.ASSISTANT, text, detail = detail,
        execution = state?.let { UiExecution(it, "2026-11-07T00:00:00Z", 1200, emptyList()) })

    @Test fun onlyKnownUnverifiedReplyTemplatesAreShortenedAndOriginalRemainsAvailable() {
        for (state in listOf("needs_user", "failed")) {
            val original = "任务尚未确认完成。已写入一份草稿，但缺少结果验收。"
            val reply = message(state, original, "结果检查未通过")
            assertEquals("未完成：执行结果检查未通过。", displayedChatReply(reply))
            assertEquals("结果检查未通过\n\n$original", executionMessageDetail(reply))
            assertEquals(original, reply.content)
        }
        val specific = "无法读取目标文件，请确认文件仍存在。"
        assertEquals(specific, displayedChatReply(message("failed", specific)))
        val legacy = "任务尚未确认完成。原始回复"
        assertEquals(legacy, displayedChatReply(message(null, legacy)))
        assertEquals(legacy, displayedChatReply(message("completed", legacy)))
        assertEquals(legacy, displayedChatReply(message("failed", legacy).copy(role = MessageRole.USER)))
    }

    @Test fun stoppedBoilerplateUsesOutcomeOnceButActualPartialResultsAreNeverHidden() {
        val old = "任务已停止；已经完成的操作会保留，未自动重试。"
        val stopped = message("cancelled", old, old)
        assertEquals("", displayedChatReply(stopped))
        assertEquals(old, executionMessageDetail(stopped))
        assertEquals("已停止", executionStateLabel("cancelled"))
        val useful = "$old\n已保存 report.txt，可在电脑上查看。"
        assertEquals(useful, displayedChatReply(message("cancelled", useful)))
        assertEquals(old, displayedChatReply(message("completed", old)))
        assertEquals(old, displayedChatReply(message(null, old)))
    }

    @Test fun needsUserRemainsUncompletedAndDoesNotInventAnApprovalRequest() {
        assertEquals("未完成", executionStateLabel("needs_user"))
        assertEquals("未完成", executionStateLabel("failed"))
        assertEquals("已中断", executionStateLabel("interrupted"))
        assertEquals("已完成", executionStateLabel("completed"))
        assertEquals("进行中", executionStateLabel("running"))
        assertEquals("未完成", executionStateLabel("unrecognized"))
    }

    @Test fun operationCountDoesNotTreatPhaseUpdatesOrSummariesAsComputerActions() {
        val process = UiExecution("running", "2026-11-07T00:00:00Z", 1300, listOf(
            UiExecutionStep("phase", "progress", "正在规划任务…", "completed", 0),
            UiExecutionStep("read", "tool", "读取文件", "completed", 500),
            UiExecutionStep("click", "computer", "点击界面元素", "running", 1000),
            UiExecutionStep("summary", "summary", "开始处理请求", "completed", 1200)))
        assertEquals(listOf("read", "click"), process.operationEvents().map { it.id })
        assertEquals(listOf(500L, 1000L), process.operationEvents().map { it.elapsedMs })
        assertEquals(4, process.events.size)
    }
}
