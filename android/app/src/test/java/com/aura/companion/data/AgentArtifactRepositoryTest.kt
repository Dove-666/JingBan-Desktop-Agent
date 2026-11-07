package com.aura.companion.data

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.security.MessageDigest
import java.io.File
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class AgentArtifactRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private class Vault : ApiConfigVault {
        override fun load(): DirectApiConfig? = null
        override fun save(config: DirectApiConfig) = Unit
        override fun clear() = Unit
    }
    private class Rpc(var data: ByteArray = ByteArray(300_000) { (it % 251).toByte() }) : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        var cid = ""
        var corrupt = false
        var wrongOffset = false
        var changedMetadata = false
        var beforeChunk: (() -> Unit)? = null
        var gets = 0
        val reference = "artifact_" + "a".repeat(64)
        fun metadata() = JSONObject().put("reference", reference).put("name", "验收文档.docx")
            .put("mime", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            .put("bytes", data.size).put("sha256", MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) })
            .put("verification_scope", "format_and_readback")
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            fun ok(value: JSONObject) = JSONObject().put("ok", true).put("result", value)
            return when(method) {
                "status" -> ok(JSONObject().put("protocol_version", 1).put("capabilities", JSONArray(listOf("artifact.list", "artifact.get"))))
                "artifact.list" -> ok(JSONObject().put("artifacts", JSONArray().also { if (params.getString("conversation_id") == cid) it.put(metadata()) }))
                "artifact.get" -> {
                    require(params.getString("conversation_id") == cid && params.getString("reference") == reference)
                    gets++; beforeChunk?.also { beforeChunk = null }?.invoke()
                    val offset = params.getInt("offset"); val end = minOf(data.size, offset + params.getInt("limit"))
                    val bytes = data.copyOfRange(offset, end)
                    if (corrupt && bytes.isNotEmpty()) bytes[0] = (bytes[0].toInt() xor 1).toByte()
                    ok(metadata().also { if (changedMetadata) it.put("name", "changed.docx") }
                        .put("offset", if (wrongOffset) offset + 1 else offset).put("total_bytes", data.size)
                        .put("next_offset", if (end < data.size) end else JSONObject.NULL)
                        .put("data_base64", Base64.encodeToString(bytes, Base64.NO_WRAP)))
                }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                else -> error("Unexpected method $method")
            }
        }
    }
    private fun database() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""), true)
    }

    @Test fun listDoesNotDownloadAndExplicitClickChecksEveryChunkAndFullHash() = runBlocking<Unit> {
        val rpc = Rpc(); val db = database(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            rpc.cid = repo.newConversation(); repo.reconnect()
            assertEquals(1, repo.state.value.agentArtifacts.size); assertEquals(0, rpc.gets)
            val result = repo.downloadArtifact(rpc.cid, rpc.reference)!!
            assertArrayEquals(rpc.data, result.file.readBytes()); assertEquals(2, rpc.gets)
            result.file.delete(); assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun zeroByteDeliveryIsValidAndNeverInventsContent() = runBlocking<Unit> {
        val rpc = Rpc(byteArrayOf()); val repo = AppRepository(context, rpc, database(), Vault())
        try {
            rpc.cid = repo.newConversation(); repo.reconnect()
            val result = repo.downloadArtifact(rpc.cid, rpc.reference)!!
            assertEquals(0L, result.file.length()); assertEquals(1, rpc.gets); result.file.delete()
        } finally { repo.close() }
    }

    @Test fun corruptDataChangedMetadataAndOutOfOrderChunkAreNeverDelivered() = runBlocking<Unit> {
        for (failure in 0..2) {
            val rpc = Rpc(); val repo = AppRepository(context, rpc, database(), Vault())
            try {
                rpc.cid = repo.newConversation(); repo.reconnect()
                val before = File(context.cacheDir, "agent-deliveries").listFiles().orEmpty().map { it.name }.toSet()
                rpc.corrupt = failure == 0; rpc.changedMetadata = failure == 1; rpc.wrongOffset = failure == 2
                assertNull(repo.downloadArtifact(rpc.cid, rpc.reference))
                assertEquals(before, File(context.cacheDir, "agent-deliveries").listFiles().orEmpty().map { it.name }.toSet())
            } finally { repo.close() }
        }
    }

    @Test fun anotherConversationOrEndpointCanNeverObtainTheSelectedDelivery() = runBlocking<Unit> {
        val rpc = Rpc(); val db = database(); val repo = AppRepository(context, rpc, db, Vault())
        try {
            rpc.cid = repo.newConversation(); repo.reconnect()
            assertNull(repo.downloadArtifact(UUID.randomUUID().toString(), rpc.reference)); assertEquals(0, rpc.gets)
            rpc.beforeChunk = { db.saveDevice(DeviceInfo("Other", "other-endpoint", "other-ticket"), "phone") }
            assertNull(repo.downloadArtifact(rpc.cid, rpc.reference)); assertEquals(1, rpc.gets)
            assertTrue(db.outbox().isEmpty())
        } finally { repo.close() }
    }

    @Test fun metadataRejectsPathsAndCoercedLengthsAndNewToolsKeepTheirPublicLabels() {
        val raw = Rpc().metadata()
        assertNotNull(parseAgentArtifact(raw))
        for (invalid in listOf("../secret.docx", "C:\\secret.docx", "bad\nname.docx"))
            assertNull(parseAgentArtifact(JSONObject(raw.toString()).put("name", invalid)))
        assertNull(parseAgentArtifact(JSONObject(raw.toString()).put("bytes", "12")))
        assertNull(parseAgentArtifact(JSONObject(raw.toString()).put("bytes", 12.5)))
        for (label in listOf("读取任务记录", "读取文档", "生成文档", "查看交付文件", "搜索网页", "等待文件授权", "验收输出结果",
            "切换网页标签", "打开网页", "操作网页控件", "查看网页标签", "读取网页结构", "渲染文档"))
            assertEquals(label, validAgentActivity(label))
        assertNull(validAgentActivity("任意推理或工具参数"))
    }
}
