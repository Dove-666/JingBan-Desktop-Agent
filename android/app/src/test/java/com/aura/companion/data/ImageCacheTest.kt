package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class ImageCacheTest {
    @Test fun oversizedInputIsRejectedBeforeBitmapDecodeOrFileWrite() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val cache = ImageCache(context)
        assertThrows(IllegalArgumentException::class.java) {
            cache.store(ByteArray(ImageCache.MAX_IMAGE_BYTES + 1), "image/png", emptySet())
        }
        assertTrue(java.io.File(context.filesDir, "companion-images").listFiles().orEmpty().isEmpty())
    }

    @Test fun callerCannotTurnImageIdIntoFilesystemPath() {
        val cache = ImageCache(ApplicationProvider.getApplicationContext<Context>())
        assertThrows(IllegalArgumentException::class.java) { cache.file("../../shared-file") }
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", ImageCache.digest(byteArrayOf()))
    }
}
