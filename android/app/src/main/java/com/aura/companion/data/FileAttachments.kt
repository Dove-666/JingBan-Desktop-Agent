package com.aura.companion.data

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.File
import java.io.InputStream
import java.security.MessageDigest
import java.util.UUID

internal const val MAX_UPLOAD_FILE_BYTES = 32L * 1024 * 1024
internal const val FILE_UPLOAD_CHUNK_BYTES = 256 * 1024
private const val MAX_FILE_CACHE_BYTES = 128L * 1024 * 1024

data class PendingFile(val conversationId: String, val id: String, val filename: String,
    val mimeType: String, val sizeBytes: Long, val localPath: String, val authorityId: String)

internal fun safeAttachmentName(value: String): String = value.replace('\\', '/').substringAfterLast('/')
    .take(160).also { require(it.isNotBlank() && it !in setOf(".", "..") && it.none { c -> c.code < 32 }) { "文件名无效" } }

/** Private, bounded streaming storage. Remote names never become cache paths. */
internal class FileAttachmentCache(private val directory: File) {
    constructor(context: Context) : this(File(context.filesDir, "file-attachments"))

    fun prepare(context: Context, uri: Uri, conversationId: String, authorityId: String,
        protected: Set<String>): PendingFile {
        val resolver = context.contentResolver
        var name = "文件"
        resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) {
                cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME).takeIf { it >= 0 && !cursor.isNull(it) }
                    ?.let { name = cursor.getString(it) }
                cursor.getColumnIndex(OpenableColumns.SIZE).takeIf { it >= 0 && !cursor.isNull(it) }?.let {
                    require(cursor.getLong(it) <= MAX_UPLOAD_FILE_BYTES) { "文件不能超过 32 MB" }
                }
            }
        }
        val mime = resolver.getType(uri)?.takeIf { it.length <= 120 && it.matches(Regex("[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")) }
            ?: "application/octet-stream"
        return resolver.openInputStream(uri)?.use { store(it, conversationId, authorityId, name, mime, protected) }
            ?: throw IllegalArgumentException("无法读取文件，请重新选择")
    }

    @Synchronized fun store(input: InputStream, conversationId: String, authorityId: String,
        filename: String, mime: String, protected: Set<String>): PendingFile {
        val label = safeAttachmentName(filename)
        directory.mkdirs()
        var used = directory.listFiles()?.filter { it.isFile }?.sumOf { it.length() } ?: 0L
        for (candidate in directory.listFiles().orEmpty().filter { it.isFile && it.name !in protected &&
            (it.name.matches(Regex("[0-9a-f]{64}")) || it.name.endsWith(".part")) }.sortedBy { it.lastModified() }) {
            if (used + MAX_UPLOAD_FILE_BYTES <= MAX_FILE_CACHE_BYTES) break
            val bytes = candidate.length()
            if (candidate.delete()) used -= bytes
        }
        require(used + MAX_UPLOAD_FILE_BYTES <= MAX_FILE_CACHE_BYTES) { "待发文件缓存已满，请移除不需要的附件" }
        val temporary = File(directory, UUID.randomUUID().toString() + ".part")
        val digest = MessageDigest.getInstance("SHA-256")
        var size = 0L
        try {
            temporary.outputStream().use { output ->
                val buffer = ByteArray(FILE_UPLOAD_CHUNK_BYTES)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    if (count == 0) continue
                    size += count
                    require(size <= MAX_UPLOAD_FILE_BYTES) { "文件不能超过 32 MB" }
                    digest.update(buffer, 0, count)
                    output.write(buffer, 0, count)
                }
                output.fd.sync()
            }
            val id = digest.digest().joinToString("") { "%02x".format(it) }
            val destination = File(directory, id)
            if (!destination.exists()) check(temporary.renameTo(destination)) { "无法保存文件，请检查手机存储空间" }
            require(destination.length() == size) { "文件缓存校验失败，请重新选择" }
            return PendingFile(conversationId, id, label, mime, size, destination.absolutePath, authorityId)
        } finally { temporary.delete() }
    }

    fun verifiedFile(pending: PendingFile): File {
        require(pending.id.matches(Regex("[0-9a-f]{64}"))) { "文件标识无效" }
        val file = File(directory, pending.id)
        require(file.canonicalPath == File(pending.localPath).canonicalPath && file.isFile && file.length() == pending.sizeBytes) {
            "待发送文件已不可用，请重新选择"
        }
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(FILE_UPLOAD_CHUNK_BYTES)
            while (true) { val count = input.read(buffer); if (count < 0) break; digest.update(buffer, 0, count) }
        }
        require(digest.digest().joinToString("") { "%02x".format(it) } == pending.id) { "文件已发生变化，请重新选择" }
        return file
    }
}
