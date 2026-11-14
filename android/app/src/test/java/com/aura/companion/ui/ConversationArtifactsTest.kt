package com.aura.companion.ui

import org.junit.Assert.*
import org.junit.Test

class ConversationArtifactsTest {
    private val first = UiMessage("answer-1", MessageRole.ASSISTANT, "文件已生成。")
    private val file = UiAgentArtifact("artifact-owned", "同学会.txt", 4096, messageId = first.id)

    @Test fun deliveryStaysWithItsReplyWhenLaterTurnsArrive() {
        val after = listOf(first, UiMessage("question-2", MessageRole.USER, "打开抖音"),
            UiMessage("answer-2", MessageRole.ASSISTANT, "已打开。"))
        val before = conversationArtifacts(listOf(first), listOf(file))
        val actual = conversationArtifacts(after, listOf(file, file))
        assertEquals(before, actual)
        assertEquals(listOf(file), actual.byMessage[first.id])
        assertFalse(actual.byMessage.containsKey("answer-2"))
        assertTrue(actual.unassigned.isEmpty())
    }

    @Test fun olderDesktopCanAssociateAnUnambiguousDeliveryMention() {
        val reply = first.copy(content = "[同学会.txt](sandbox:/mnt/data/同学会.txt)")
        val older = file.copy(messageId = null)
        val result = conversationArtifacts(listOf(reply,
            UiMessage("next", MessageRole.ASSISTANT, "已经打开抖音。")), listOf(older))
        assertEquals(listOf(older), result.byMessage[first.id])
    }

    @Test fun missingOrAmbiguousOwnersAreNeverGuessedAsTheLatestAnswer() {
        val answers = listOf(first.copy(content = "同学会.txt"),
            first.copy(id = "second", content = "同学会.txt"))
        for (candidate in listOf(file.copy(messageId = "not-loaded"), file.copy(messageId = null))) {
            val result = conversationArtifacts(answers, listOf(candidate))
            assertTrue(result.byMessage.isEmpty())
            assertEquals(listOf(candidate), result.unassigned)
        }
        val result = conversationArtifacts(listOf(first.copy(role = MessageRole.USER)), listOf(file))
        assertTrue(result.byMessage.isEmpty())
    }

    @Test fun sameFilenameDoesNotMergeSeparateDeliveries() {
        val second = first.copy(id = "answer-2")
        val replacement = file.copy(reference = "artifact-revised", messageId = second.id)
        val result = conversationArtifacts(listOf(first, second), listOf(file, replacement))
        assertEquals(listOf(file), result.byMessage[first.id])
        assertEquals(listOf(replacement), result.byMessage[second.id])
    }
}
