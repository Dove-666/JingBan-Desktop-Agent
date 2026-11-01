package com.aura.companion

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayOutputStream

/** Downsample before decoding: camera originals never become full-size bitmaps. */
internal object ImagePreparer {
    data class Prepared(val bytes: ByteArray, val filename: String, val mimeType: String)
    fun prepare(context: Context, uri: Uri): Prepared {
        val resolver = context.contentResolver
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri).use { stream -> require(stream != null); BitmapFactory.decodeStream(stream, null, bounds) }
        require(bounds.outWidth > 0 && bounds.outHeight > 0)
        var sample = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / sample > 1800) sample *= 2
        val options = BitmapFactory.Options().apply { inSampleSize = sample; inPreferredConfig = Bitmap.Config.ARGB_8888 }
        val bitmap = resolver.openInputStream(uri).use { stream -> BitmapFactory.decodeStream(stream, null, options) }
            ?: error("Unreadable image")
        var prepared = bitmap
        try {
            val orientation = runCatching { resolver.openInputStream(uri).use { stream ->
                if (stream == null) 1 else ExifInterface(stream).getAttributeInt(ExifInterface.TAG_ORIENTATION, 1)
            } }.getOrDefault(1)
            val matrix = Matrix()
            when (orientation) {
                ExifInterface.ORIENTATION_ROTATE_90 -> matrix.postRotate(90f)
                ExifInterface.ORIENTATION_ROTATE_180 -> matrix.postRotate(180f)
                ExifInterface.ORIENTATION_ROTATE_270 -> matrix.postRotate(270f)
                ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.postScale(-1f, 1f)
                ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.postScale(1f, -1f)
                ExifInterface.ORIENTATION_TRANSPOSE -> { matrix.postRotate(90f); matrix.postScale(-1f, 1f) }
                ExifInterface.ORIENTATION_TRANSVERSE -> { matrix.postRotate(270f); matrix.postScale(-1f, 1f) }
            }
            if (!matrix.isIdentity) prepared = Bitmap.createBitmap(bitmap, 0, 0, bitmap.width, bitmap.height, matrix, true)
            val output = ByteArrayOutputStream()
            require(prepared.compress(Bitmap.CompressFormat.JPEG, 88, output))
            val bytes = output.toByteArray()
            require(bytes.size <= 4 * 1024 * 1024)
            return Prepared(bytes, "手机图片-${System.currentTimeMillis()}.jpg", "image/jpeg")
        } finally {
            if (prepared !== bitmap) prepared.recycle()
            bitmap.recycle()
        }
    }
}
