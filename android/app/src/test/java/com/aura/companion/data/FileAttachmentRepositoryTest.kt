package com.aura.companion.data

import android.content.Context
import android.content.ContextWrapper
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.io.RandomAccessFile

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class FileAttachmentRepositoryTest {
    @get:Rule val temporary = TemporaryFolder()
    private fun context(): Context {
        val root = temporary.newFolder()
        return object : ContextWrapper(ApplicationProvider.getApplicationContext<Context>()) {
            override fun getApplicationContext(): Context = this
            override fun getFilesDir(): File = root
        }
    }
    private class Vault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }
    private class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var supportsFiles = true
        var failChunk = -1
        var rejectTask = false
        var beforeTask: (() -> Unit)? = null
        var afterChunk: (() -> Unit)? = null
        val chunks = mutableListOf<JSONObject>()
        val tasks = mutableListOf<JSONObject>()
        val bytes = ByteArrayOutputStream()
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(value: JSONObject) = JSONObject().put("ok", true).put("result", value)
            return when (method) {
                "status" -> ok(JSONObject().put("protocol_version", 1).put("agent_available", true)
                    .put("agent_busy", false).put("capabilities", JSONArray(
                        if (supportsFiles) listOf("agent.send", "file.upload") else listOf("agent.send"))))
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0)
                    .put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                "file.upload" -> {
                    chunks += JSONObject(params.toString())
                    if (chunks.size == failChunk) return JSONObject().put("ok", false)
                        .put("error", JSONObject().put("code", "storage_error").put("message", "文件保存失败"))
                    val chunk = Base64.decode(params.getString("data_base64"), Base64.NO_WRAP)
                    require(chunk.size <= FILE_UPLOAD_CHUNK_BYTES)
                    require(params.getLong("offset") == bytes.size().toLong())
                    bytes.write(chunk)
                    afterChunk?.also { afterChunk = null }?.invoke()
                    ok(JSONObject().put("id", params.getString("sha256")).put("sha256", params.getString("sha256"))
                        .put("filename", params.getString("filename")).put("mime_type", params.getString("mime_type"))
                        .put("size", params.getLong("size")).put("offset", bytes.size())
                        .put("complete", bytes.size().toLong() == params.getLong("size")))
                }
                "agent.send" -> {
                    tasks += JSONObject(params.toString())
                    beforeTask?.also { beforeTask = null }?.invoke()
                    if (rejectTask) JSONObject().put("ok", false)
                        .put("error", JSONObject().put("code", "invalid_params").put("message", "文件请求未接受"))
                    else ok(JSONObject().put("conversation_id", params.getString("conversation_id"))
                        .put("message_id", AppRepository.stableMessageId(endpointId, requestId)))
                }
                else -> error("Unexpected RPC $method")
            }
        }
    }
    private fun database(context: Context) = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "synthetic-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""), true)
    }
    private fun stage(context: Context, db: CompanionDatabase, cid: String,
        bytes: ByteArray, name: String = "notes.pdf"): PendingFile = FileAttachmentCache(context)
        .store(bytes.inputStream(), cid, "desktop", name, "application/pdf", db.protectedFileIds())
        .also { db.stageFile(it) }

    @Test fun sendsExactChunkedBytesAndOnlyAttachesFileToTheOwningTurn() = runBlocking<Unit> {
        val context = context(); val db = database(context); val rpc = Rpc()
        val repo = AppRepository(context, rpc, db, Vault())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            val source = ByteArray(700_019) { ((it * 11) % 253).toByte() }
            val pending = stage(context, db, cid, source)
            assertTrue(repo.sendDesktopMessage(cid, "Read the attachment"))
            assertArrayEquals(source, rpc.bytes.toByteArray())
            assertEquals(listOf(0L, 262144L, 524288L), rpc.chunks.map { it.getLong("offset") })
            assertEquals(pending.id, rpc.tasks.single().getString("file_id"))
            assertFalse(rpc.tasks.single().has("local_file_name")); assertFalse(rpc.tasks.single().has("local_file_size"))
            assertFalse(rpc.tasks.single().has("image_id")); assertNull(db.pendingFile(cid))
            assertEquals("notes.pdf", db.messages(cid).single().fileName)
            assertEquals(source.size.toLong(), db.messages(cid).single().fileSize)
            assertTrue(repo.sendDesktopMessage(cid, "Now do something else"))
            assertFalse(rpc.tasks.last().has("file_id")); assertEquals(3, rpc.chunks.size)
            assertEquals(1, db.messages(cid).count { it.fileName != null })
        } finally { repo.close() }
    }

    @Test fun uploadFailureOrOldDesktopPreservesFileAndDoesNotSubmitTask() = runBlocking<Unit> {
        for (unsupported in listOf(false, true)) {
            val context = context(); val db = database(context); val rpc = Rpc().apply {
                supportsFiles = !unsupported; failChunk = 2
            }
            val repo = AppRepository(context, rpc, db, Vault())
            try {
                repo.reconnect(); val cid = repo.newConversation()
                val pending = stage(context, db, cid, ByteArray(400_000) { 37 })
                assertFalse(repo.sendDesktopMessage(cid, "Keep this draft"))
                assertEquals(pending, db.pendingFile(cid)); assertTrue(db.outbox().isEmpty())
                assertTrue(rpc.tasks.isEmpty()); assertEquals(if (unsupported) 0 else 2, rpc.chunks.size)
                assertFalse(repo.sendMessage(cid, "Phone chat cannot silently drop this attachment"))
                assertEquals(pending, db.pendingFile(cid))
            } finally { repo.close() }
        }
    }

    @Test fun rejectedTaskRestoresAttachmentWithoutReplacingNewerSelection() = runBlocking<Unit> {
        for (replace in listOf(false, true)) {
            val context = context(); val db = database(context); val rpc = Rpc().apply { rejectTask = true }
            val repo = AppRepository(context, rpc, db, Vault())
            try {
                repo.reconnect(); val cid = repo.newConversation()
                val original = stage(context, db, cid, byteArrayOf(1, 2, 3))
                var expected = original
                if (replace) rpc.beforeTask = { expected = stage(context, db, cid, byteArrayOf(4, 5), "new.pdf") }
                assertFalse(repo.sendDesktopMessage(cid, "Preserve the user's next choice"))
                assertEquals(expected, db.pendingFile(cid)); assertEquals("failed", db.outbox().single().status)
            } finally { repo.close() }
        }
    }

    @Test fun interruptedUploadCannotStartTaskForChangedAttachmentOrComputer() = runBlocking<Unit> {
        for (changeComputer in listOf(false, true)) {
            val context = context(); val db = database(context); val rpc = Rpc()
            val repo = AppRepository(context, rpc, db, Vault())
            try {
                repo.reconnect(); val cid = repo.newConversation()
                stage(context, db, cid, ByteArray(400_000))
                rpc.afterChunk = {
                    if (changeComputer) db.saveDevice(DeviceInfo("Other", "other", "other-ticket"), "phone")
                    else stage(context, db, cid, byteArrayOf(7), "new.pdf")
                }
                assertFalse(repo.sendDesktopMessage(cid, "Stay with the selected computer"))
                assertEquals(1, rpc.chunks.size); assertTrue(rpc.tasks.isEmpty())
                if (changeComputer) assertNull(db.pendingFile(cid)) else assertEquals("new.pdf", db.pendingFile(cid)!!.filename)
            } finally { repo.close() }
        }
    }

    @Test fun emptyFilesAreUploadedAsEmptyAndOversizeOrChangedFilesAreRejected() = runBlocking<Unit> {
        val context = context(); val db = database(context); val rpc = Rpc(); val cache = FileAttachmentCache(context)
        val repo = AppRepository(context, rpc, db, Vault())
        try {
            repo.reconnect(); val cid = repo.newConversation()
            val empty = stage(context, db, cid, byteArrayOf(), "empty.txt")
            assertEquals(0L, cache.verifiedFile(empty).length())
            assertTrue(repo.sendDesktopMessage(cid, "")); assertEquals("", rpc.chunks.single().getString("data_base64"))
            val file = stage(context, db, cid, byteArrayOf(1, 2, 3), "change.txt")
            File(file.localPath).writeBytes(byteArrayOf(3, 2, 1))
            assertFalse(repo.sendDesktopMessage(cid, "changed")); assertEquals(file, db.pendingFile(cid))
            val oversized = object : InputStream() {
                var remaining = MAX_UPLOAD_FILE_BYTES + 1
                override fun read(): Int = if (remaining-- > 0) 0 else -1
                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    if (remaining == 0L) return -1
                    val count = minOf(len.toLong(), remaining).toInt(); remaining -= count
                    b.fill(0, off, off + count); return count
                }
            }
            assertThrows(IllegalArgumentException::class.java) {
                cache.store(oversized, cid, "desktop", "too-big.bin", "application/octet-stream", db.protectedFileIds())
            }
            assertTrue(File(context.filesDir, "file-attachments").listFiles().orEmpty().none { it.name.endsWith(".part") })
        } finally { repo.close() }
    }

    @Test fun changingComputerReleasesUnreachableCacheWithoutDeletingSourceOrReusingOldAttachment() {
        val context = context(); val db = database(context)
        val cache = FileAttachmentCache(context)
        val directory = File(context.filesDir, "file-attachments").also { it.mkdirs() }
        val pending = (0..3).map { index ->
            val cid = db.createConversation(); val id = ('a' + index).toString().repeat(64)
            val cached = File(directory, id)
            RandomAccessFile(cached, "rw").use { it.setLength(MAX_UPLOAD_FILE_BYTES) }
            PendingFile(cid, id, "old-$index.pdf", "application/pdf", MAX_UPLOAD_FILE_BYTES,
                cached.absolutePath, "desktop").also { db.stageFile(it) }
        }
        try {
            val queued = db.queueAgentMessage(pending.first().conversationId, "Old computer request", pending.first())
            assertEquals(pending.map { it.id }.toSet(), db.protectedFileIds())
            assertThrows(IllegalArgumentException::class.java) {
                cache.store(byteArrayOf(9).inputStream(), pending.last().conversationId, "desktop", "new.pdf",
                    "application/pdf", db.protectedFileIds())
            }
            db.saveDevice(DeviceInfo("Other PC", "other", "synthetic-other-ticket"), "phone")
            assertTrue(db.pendingFiles().isEmpty()); assertTrue(db.protectedFileIds().isEmpty())
            assertTrue(pending.all { File(it.localPath).exists() })
            assertEquals("needs_attention", db.operation(queued)!!.item.status)
            assertThrows(IllegalArgumentException::class.java) {
                db.queueAgentMessage(pending.first().conversationId, "Cannot transfer old file", pending.first())
            }
            val cid = db.createConversation()
            val fresh = cache.store(byteArrayOf(9).inputStream(), cid, "other", "new.pdf", "application/pdf", db.protectedFileIds())
            assertArrayEquals(byteArrayOf(9), cache.verifiedFile(fresh).readBytes())
            db.stageFile(fresh)
            val current = db.queueAgentMessage(cid, "New computer request", fresh)
            assertEquals(fresh.id, db.operation(current)!!.params.getString("file_id"))
            assertEquals("other", db.operation(current)!!.authorityId)
        } finally { db.close() }
    }
}
