package com.aura.companion.data

import android.content.Context
import android.graphics.BitmapFactory
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest

internal class ImageCache(context: Context) {
    companion object {
        const val MAX_IMAGE_BYTES = 4 * 1024 * 1024
        const val MAX_CACHE_BYTES = 64L * 1024 * 1024
        fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it.toInt() and 255) }
    }

    private val directory = File(context.filesDir, "companion-images")

    data class Stored(val id: String, val file: File, val mimeType: String, val width: Int, val height: Int)

    /** Bounds-only parsing avoids allocating the source image's full bitmap. */
    fun store(bytes: ByteArray, mimeType: String, protectedIds: Set<String>): Stored {
        require(bytes.isNotEmpty() && bytes.size <= MAX_IMAGE_BYTES) { "单张图片不能超过 4 MiB" }
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
        val detected = options.outMimeType ?: ""
        require(detected in setOf("image/png", "image/jpeg", "image/webp") && options.outWidth > 0 && options.outHeight > 0 &&
            options.outWidth.toLong() * options.outHeight <= 16_000_000L) { "仅支持最多 1600 万像素的 PNG、JPEG、WebP 图片" }
        require(mimeType.isBlank() || mimeType == detected) { "图片类型与内容不一致" }
        val id = digest(bytes)
        require(directory.exists() || directory.mkdirs()) { "无法创建图片缓存" }
        val destination = File(directory, "$id.img")
        if (!destination.exists()) {
            directory.listFiles()?.filter { it.name.endsWith(".tmp") }?.forEach { it.delete() }
            var used = directory.listFiles()?.sumOf { if (it.isFile) it.length() else 0L } ?: 0L
            val evictable = directory.listFiles()?.filter { it.name.endsWith(".img") && it.name.removeSuffix(".img") !in protectedIds }
                ?.sortedBy { it.lastModified() }.orEmpty()
            for (old in evictable) {
                if (used + bytes.size <= MAX_CACHE_BYTES) break
                val length = old.length()
                if (old.delete()) used -= length
            }
            require(used + bytes.size <= MAX_CACHE_BYTES) { "图片缓存已达 64 MiB，请先处理待发图片" }
            val temporary = File(directory, "$id.tmp")
            try {
                FileOutputStream(temporary).use { it.write(bytes); it.fd.sync() }
                check(temporary.renameTo(destination)) { "无法保存图片缓存" }
            } finally { temporary.delete() }
        }
        destination.setLastModified(System.currentTimeMillis())
        return Stored(id, destination, detected, options.outWidth, options.outHeight)
    }

    fun file(id: String): File? {
        require(id.matches(Regex("[0-9a-f]{64}"))) { "图片标识无效" }
        return File(directory, "$id.img").takeIf { it.isFile && it.length() <= MAX_IMAGE_BYTES }
    }

    fun delete(id: String) { file(id)?.delete() }
}
