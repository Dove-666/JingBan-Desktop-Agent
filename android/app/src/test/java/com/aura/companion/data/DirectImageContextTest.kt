package com.aura.companion.data

import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class DirectImageContextTest {
    private val config = DirectApiConfig("phone", "revision", "https://example.test/v1", "model", "private-key",
        "system", 30, "default", 0)
    private fun user(id: String, image: String? = null, state: String? = null) = Message(id, "thread", "user", id,
        "2026-11-01T00:00:00Z", attachmentId = image, localState = state)
    private fun images(messages: JSONArray): Int = (0 until messages.length()).sumOf { index ->
        val content = messages.getJSONObject(index).optJSONArray("content")
        if (content == null) 0 else (0 until content.length()).count { content.getJSONObject(it).optString("type") == "image_url" }
    }

    @Test fun explicitFollowupKeepsOneImageAtOriginalUserAndCurrentUserPlain() {
        val loaded = mutableListOf<String>()
        val messages = directMessages(config, listOf(user("original", "a".repeat(64)),
            Message("reply", "thread", "assistant", "answer", "2026-11-01T00:00:01Z")), "上面那张图片", null) {
            loaded += it; "image/png" to byteArrayOf(1, 2, 3)
        }
        assertEquals(listOf("a".repeat(64)), loaded)
        assertNotNull(messages.getJSONObject(1).optJSONArray("content"))
        assertEquals("上面那张图片", messages.getJSONObject(messages.length() - 1).getString("content"))
        assertEquals(1, images(messages))
    }

    @Test fun unrelatedTextAndNewAttachmentNeverReloadAnOldImage() {
        var loads = 0
        for (text in listOf("?", "你好", "new topic")) {
            val messages = directMessages(config, listOf(user("original", "a".repeat(64))), text, null) {
                loads++; error("Unrelated text must not load image bytes")
            }
            assertEquals(0, images(messages))
        }
        val withNew = directMessages(config, listOf(user("original", "a".repeat(64))), "对比这张图片", "image/png" to byteArrayOf(4)) {
            loads++; error("A new explicit attachment must not resend an old image")
        }
        assertEquals(0, loads); assertEquals(1, images(withNew))
        assertTrue(withNew.getJSONObject(1).get("content") is String)
    }

    @Test fun imageLoadingIsBoundedToLatestEligibleImageInsideTextHistoryBudget() {
        val loaded = mutableListOf<String>()
        val history = listOf(user("old", "a".repeat(64)), user("new", "b".repeat(64)),
            user("failed", "c".repeat(64), "unknown"))
        val messages = directMessages(config, history, "what is in the PHOTO?", null) {
            loaded += it; "image/png" to byteArrayOf(1)
        }
        assertEquals(listOf("b".repeat(64)), loaded); assertEquals(1, images(messages))
        val tooOld = listOf(user("outside", "d".repeat(64))) + (0 until 20).map { user("plain-$it") }
        directMessages(config, tooOld, "上图", null) { error("Image outside the recent 20 turns must not be loaded") }
        directMessages(config, listOf(user("outside", "d".repeat(64)), user("long").copy(content = "x".repeat(48001))),
            "上图", null) { error("Image outside the 48000 character budget must not be loaded") }
    }

    @Test fun missingHistoricalCacheStillAllowsTextWithoutInventingANewAttachment() {
        val messages = directMessages(config, listOf(user("original", "a".repeat(64))), "上图", null) { null }
        assertEquals(0, images(messages))
        assertEquals("上图", messages.getJSONObject(messages.length() - 1).getString("content"))
    }
}
