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
        val olderButInBudget = listOf(user("outside", "d".repeat(64))) + (0 until 20).map { user("plain-$it") }
        val referenced = directMessages(config, olderButInBudget, "上图", null) {
            assertEquals("d".repeat(64), it); "image/png" to byteArrayOf(1)
        }
        assertEquals(1, images(referenced))
        assertTrue(referenced.getJSONObject(referenced.length() - 1).get("content") is String)
        directMessages(config, listOf(user("outside", "d".repeat(64)), user("long").copy(content = "x".repeat(48001))),
            "上图", null) { error("Image outside the 48000 character budget must not be loaded") }
    }

    @Test fun missingHistoricalCacheStillAllowsTextWithoutInventingANewAttachment() {
        val messages = directMessages(config, listOf(user("original", "a".repeat(64))), "上图", null) { null }
        assertEquals(0, images(messages))
        assertEquals("上图", messages.getJSONObject(messages.length() - 1).getString("content"))
    }

    @Test fun generalImageTopicsNeverUploadAnEarlierPrivatePhoto() {
        for (text in listOf("图片压缩算法怎么做", "如何选择照片格式", "how does image compression work", "photography basics")) {
            val loaded = mutableListOf<String>()
            val messages = directMessages(config, listOf(user("private-photo", "a".repeat(64))), text, null) {
                loaded += it; "image/png" to byteArrayOf(1)
            }
            assertTrue("A general topic must not read private photo bytes: $text", loaded.isEmpty())
            assertEquals(0, images(messages))
            assertEquals(text, messages.getJSONObject(messages.length() - 1).getString("content"))
        }
    }

    @Test fun explicitChineseAndEnglishReferencesStillReadOneOriginalImage() {
        for (text in listOf("上面那张照片", "this photo", "图里这个", "previous image")) {
            val messages = directMessages(config, listOf(user("original", "a".repeat(64))), text, null) {
                "image/png" to byteArrayOf(1)
            }
            assertEquals(1, images(messages))
            assertTrue(messages.getJSONObject(messages.length() - 1).get("content") is String)
        }
    }

    @Test fun explicitNewVersusPreviousComparisonKeepsOneImageAtEachOriginalUserTurn() {
        for (text in listOf("这张和上一张照片哪里不一样？", "跟上面那张图比较一下", "对比这两张图片",
                "Compare this photo with the previous image", "what changed from the last picture?")) {
            val loaded = mutableListOf<String>()
            val old = user("original", "a".repeat(64))
            val messages = directMessages(config, listOf(old), text, "image/png" to byteArrayOf(4, 5)) {
                loaded += it; "image/png" to byteArrayOf(1, 2)
            }
            assertEquals("Explicit two-image comparison must read one old original: $text", listOf("a".repeat(64)), loaded)
            assertEquals(2, images(messages))
            assertEquals("a".repeat(64), old.attachmentId)
            val current = messages.getJSONObject(messages.length() - 1).getJSONArray("content")
            assertEquals(2, current.length())
            assertEquals(text, current.getJSONObject(0).getString("text"))
            assertEquals("data:image/png;base64,BAU=", current.getJSONObject(1).getJSONObject("image_url").getString("url"))
            assertEquals("data:image/png;base64,AQI=", messages.getJSONObject(1).getJSONArray("content")
                .getJSONObject(1).getJSONObject("image_url").getString("url"))
        }
    }

    @Test fun ordinaryNewImageAndGeneralComparisonNeverReloadPrivateHistoricalPixels() {
        for (text in listOf("这张图片是什么？", "对比这张图片", "JPEG和PNG有什么区别", "你好", "?", "compare image formats",
                "比较这两张表的数据", "对比这两张车票的价格", "比较这两张图表的数据", "比较这两张")) {
            val messages = directMessages(config, listOf(user("old-private", "a".repeat(64))), text,
                "image/png" to byteArrayOf(4)) { error("No explicit previous-image comparison: $text") }
            assertEquals(1, images(messages))
            assertTrue(messages.getJSONObject(1).get("content") is String)
        }
    }

    @Test fun explicitComparisonUsesOnlyLatestEligibleOriginal() {
        val history = listOf(user("older", "a".repeat(64)), user("latest", "b".repeat(64)),
            user("failed", "c".repeat(64), "unknown"))
        val loaded = mutableListOf<String>()
        val messages = directMessages(config, history, "这张和上一张照片有什么差异", "image/png" to byteArrayOf(4)) {
            loaded += it; "image/png" to byteArrayOf(1)
        }
        assertEquals(listOf("b".repeat(64)), loaded)
        assertEquals(2, images(messages))
    }

    @Test fun explicitComparisonRequiresReadablePriorImageBeforeSendingAnyPayload() {
        val recent = listOf(user("original", "a".repeat(64)))
        val outsideBudget = recent + user("long").copy(content = "x".repeat(48001))
        val unavailable = listOf(user("failed", "b".repeat(64), "unknown"))
        for (history in listOf(recent, emptyList(), outsideBudget, unavailable)) {
            try {
                directMessages(config, history, "跟上一张图比较", "image/png" to byteArrayOf(4)) { null }
                fail("An explicit comparison must not silently send only the new image")
            } catch (invalid: IllegalArgumentException) {
                assertEquals("无法读取上一张图片，请重新选择图片后再比较。", invalid.message)
            }
        }
    }

    @Test fun compareTwoAlreadySentImagesWithoutAttachingAgainKeepsBothOriginalTurns() {
        val first = "a".repeat(64)
        val second = "b".repeat(64)
        val loaded = mutableListOf<String>()
        val messages = directMessages(config, listOf(user("first", first), user("second", second)),
            "对比这两张图片，有什么区别？", null) {
            loaded += it
            "image/png" to if (it == first) byteArrayOf(1) else byteArrayOf(2)
        }
        assertEquals(listOf(first, second), loaded)
        assertEquals(2, images(messages))
        assertEquals("data:image/png;base64,AQ==", messages.getJSONObject(1).getJSONArray("content")
            .getJSONObject(1).getJSONObject("image_url").getString("url"))
        assertEquals("data:image/png;base64,Ag==", messages.getJSONObject(2).getJSONArray("content")
            .getJSONObject(1).getJSONObject("image_url").getString("url"))
        assertEquals("对比这两张图片，有什么区别？", messages.getJSONObject(messages.length() - 1).getString("content"))
    }

    @Test fun historicalPairNeedsTwoReadableEligibleImagesWithoutInventingAnAttachment() {
        val first = user("first", "a".repeat(64))
        val second = user("second", "b".repeat(64))
        for (history in listOf(emptyList(), listOf(first), listOf(first, second.copy(localState = "unknown")),
                listOf(first, second, user("long").copy(content = "x".repeat(48001))))) {
            try {
                directMessages(config, history, "compare these two images", null) { "image/png" to byteArrayOf(1) }
                fail("A historical pair requires both original images inside the bounded eligible history")
            } catch (_: IllegalArgumentException) { }
        }
        try {
            directMessages(config, listOf(first, second), "对比这两张图片", null) {
                if (it == first.attachmentId) "image/png" to byteArrayOf(1) else null
            }
            fail("A missing image must not silently become a one-image comparison")
        } catch (_: IllegalArgumentException) { }
    }
}
