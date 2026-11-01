package com.aura.companion.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.security.KeyStore
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.math.ceil
import javax.crypto.Cipher
import javax.crypto.AEADBadTagException
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import org.json.JSONException

/** Deliberately not a data class: neither toString nor generated copy methods expose credentials. */
internal class DirectApiConfig(
    val authorityId: String, val revision: String, val baseUrl: String, val model: String,
    val apiKey: String, val systemPrompt: String, val timeoutSeconds: Int,
    val reasoningEffort: String, val syncedAt: Long,
    val source: ApiConfigSource = ApiConfigSource.DESKTOP,
    val desktopCache: DirectApiConfig? = null,
) {
    override fun toString() = "DirectApiConfig([private])"
    fun publicInfo() = ApiConnectionInfo(true, baseUrl, model, ApiConfigSyncState.READY, syncedAt,
        if (source == ApiConfigSource.LOCAL) "手机独立模型，无需连接电脑即可对话" else "手机直接连接模型；电脑离线时仍可聊天，记录稍后同步",
        source = source, keySaved = true, timeoutSeconds = timeoutSeconds, reasoningEffort = reasoningEffort)

    fun withDesktopCache(config: DirectApiConfig?): DirectApiConfig = DirectApiConfig(authorityId, revision, baseUrl, model,
        apiKey, systemPrompt, timeoutSeconds, reasoningEffort, syncedAt, source, config)

    fun encoded(): ByteArray = JSONObject().put("authority_id", authorityId).put("revision", revision)
        .put("base_url", baseUrl).put("model", model).put("api_key", apiKey).put("system_prompt", systemPrompt)
        .put("timeout_seconds", timeoutSeconds).put("reasoning_effort", reasoningEffort).put("synced_at", syncedAt)
        .put("source", source.name).apply { desktopCache?.let { put("desktop_cache", JSONObject(it.encoded().toString(Charsets.UTF_8))) } }
        .toString().toByteArray(Charsets.UTF_8)

    companion object {
        fun fromRpc(value: JSONObject, authority: String, allowHttp: Boolean): DirectApiConfig {
            require(value.optInt("version") == 1) { "电脑 API 配置版本不兼容，请更新两端应用" }
            fun text(key: String, maximum: Int): String {
                val raw = value.opt(key)
                require(raw is String && raw.isNotBlank() && raw.length <= maximum && '\u0000' !in raw) { "电脑 API 配置无效，请在电脑检查后重新同步" }
                return raw
            }
            val base = text("base_url", 2048).trim().trimEnd('/')
            val uri = try { URI(base) } catch (_: Exception) { throw IllegalArgumentException("电脑 API 地址格式无效") }
            require(uri.scheme == "https" || (allowHttp && uri.scheme == "http")) { "手机直连只支持 HTTPS，请在电脑将 API 地址改为 HTTPS 后重新同步" }
            require(!uri.host.isNullOrBlank() && uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null &&
                uri.port in -1..65535 && base.none { it.code < 32 }) { "电脑 API 地址无效：不能包含账号、查询参数或片段" }
            val key = text("api_key", 16384)
            require(key.none { it.code < 32 || it.code == 127 }) { "电脑 API 密钥格式无效" }
            val model = text("model", 200)
            require(!base.contains(key) && !model.contains(key)) { "API 地址和模型名不能包含密钥" }
            val effort = value.optString("reasoning_effort", "default")
            require(effort in setOf("default", "none", "minimal", "low", "medium", "high", "xhigh", "max")) { "电脑 API 推理配置无效" }
            val timeout = value.optDouble("timeout_seconds", 30.0)
            require(timeout.isFinite() && timeout in 1.0..120.0) { "电脑 API 超时配置无效" }
            return DirectApiConfig(authority, text("revision", 128), base, model, key,
                text("system_prompt", 32768), ceil(timeout).toInt(), effort, System.currentTimeMillis())
        }

        fun decode(bytes: ByteArray): DirectApiConfig {
            val json = JSONObject(bytes.toString(Charsets.UTF_8))
            // Stored data was validated on receipt and is authenticated by GCM.
            return DirectApiConfig(json.getString("authority_id"), json.getString("revision"), json.getString("base_url"),
                json.getString("model"), json.getString("api_key"), json.getString("system_prompt"), json.getInt("timeout_seconds"),
                json.getString("reasoning_effort"), json.getLong("synced_at"),
                if (json.optString("source") == "LOCAL") ApiConfigSource.LOCAL else ApiConfigSource.DESKTOP,
                json.optJSONObject("desktop_cache")?.let { decode(it.toString().toByteArray(Charsets.UTF_8)) })
        }
    }
}

internal interface ApiConfigVault {
    fun load(): DirectApiConfig?
    fun save(config: DirectApiConfig)
    fun clear()
}

/** Only ciphertext is written. AES key remains non-exportable in Android Keystore. */
internal class EncryptedApiConfigVault(context: Context, storageName: String = "direct-api-v1.enc",
    private val keyProvider: (() -> SecretKey)? = null) : ApiConfigVault {
    private val file = AtomicFile(File(context.noBackupFilesDir, storageName))
    private val aad = "aura.direct-api.v1".toByteArray(Charsets.UTF_8)
    private fun key(): SecretKey = keyProvider?.invoke() ?: synchronized(EncryptedApiConfigVault::class.java) {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey) ?: KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).setRandomizedEncryptionRequired(true).build())
            generateKey()
        }
    }
    @Synchronized override fun load(): DirectApiConfig? {
        // openRead performs AtomicFile's .bak recovery before checking size.
        // Checking baseFile first can hide a valid cache after a process crash.
        val encoded = try {
            file.openRead().use { input ->
                if (input.channel.size() !in 29..524288) throw InvalidCacheFormat()
                input.readBytes()
            }
        } catch (_: InvalidCacheFormat) { file.delete(); return null }
        catch (_: Exception) { return null } // Temporary I/O is not proof of corruption.
        if (encoded.size !in 29..524288 || encoded[0] != 1.toByte()) { file.delete(); return null }
        val plaintext = try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, encoded.copyOfRange(1, 13)))
            cipher.updateAAD(aad)
            cipher.doFinal(encoded.copyOfRange(13, encoded.size))
        } catch (_: AEADBadTagException) { file.delete(); return null }
        catch (_: Exception) { return null } // Locked/unavailable Keystore: retain ciphertext.
        return try { DirectApiConfig.decode(plaintext) }
        catch (_: JSONException) { file.delete(); null }
        catch (_: IllegalArgumentException) { file.delete(); null }
        finally { plaintext.fill(0) }
    }
    @Synchronized override fun save(config: DirectApiConfig) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key()); cipher.updateAAD(aad)
        val plaintext = config.encoded()
        val ciphertext = try { cipher.doFinal(plaintext) } finally { plaintext.fill(0) }
        val stream = file.startWrite()
        try { stream.write(byteArrayOf(1) + cipher.iv + ciphertext); file.finishWrite(stream) }
        catch (failure: Exception) { file.failWrite(stream); throw failure }
    }
    @Synchronized override fun clear() { file.delete() }
    companion object { private const val KEY_ALIAS = "aura-direct-api-aes-v1" }
    private class InvalidCacheFormat : Exception()
}

internal class DirectApiFailure(val safeMessage: String) : Exception(safeMessage)
internal interface DirectChatGateway {
    suspend fun complete(config: DirectApiConfig, messages: JSONArray): String
}

/** One HTTP request, no SDK retry, redirects or fallback that could duplicate billing. */
internal class HttpDirectChatGateway : DirectChatGateway {
    override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("model", config.model).put("messages", messages).put("stream", false)
        if (config.reasoningEffort != "default") payload.put("reasoning_effort", config.reasoningEffort)
        val model = config.model.substringAfterLast('/').lowercase()
        val modern = URI(config.baseUrl).host == "api.openai.com" || Regex("^(o1|o3|o4)(-|$)|^gpt-(5|6)([.-]|$)").containsMatchIn(model)
        payload.put(if (modern) "max_completion_tokens" else "max_tokens", 6000)
        val bytes = payload.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 8 * 1024 * 1024) { "模型请求超过大小限制，请减少历史或图片" }
        val connection = URI(config.baseUrl.trimEnd('/') + "/chat/completions").toURL().openConnection() as HttpURLConnection
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(config.timeoutSeconds.toLong())
        val watchdog = deadlines.schedule({ connection.disconnect() }, config.timeoutSeconds.toLong(), TimeUnit.SECONDS)
        try {
            connection.requestMethod = "POST"; connection.doOutput = true; connection.instanceFollowRedirects = false
            connection.connectTimeout = config.timeoutSeconds * 1000; connection.readTimeout = config.timeoutSeconds * 1000
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.setRequestProperty("Authorization", "Bearer ${config.apiKey}")
            connection.setFixedLengthStreamingMode(bytes.size)
            connection.outputStream.use { it.write(bytes) }
            if (connection.responseCode !in 200..299) throw DirectApiFailure(when (connection.responseCode) {
                401, 403 -> "模型 API 拒绝了密钥或权限，请检查手机模型配置中的密钥与权限"
                429 -> "模型 API 配额或频率受限，请稍后确认再发送"
                in 300..399 -> "模型 API 要求重定向，为保护密钥已停止，请检查手机模型配置中的 API 地址"
                else -> "模型 API 返回错误；未转交电脑重试，请确认后重新发送"
            })
            val raw = connection.inputStream.use { input ->
                val out = ByteArrayOutputStream(); val buffer = ByteArray(8192)
                while (true) {
                    val remaining = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime())
                    if (remaining <= 0) throw DirectApiFailure("模型请求超时，可能已计费；不会自动重试，请确认后重新发送")
                    connection.readTimeout = remaining.coerceAtMost(config.timeoutSeconds * 1000L).toInt().coerceAtLeast(1)
                    val n = input.read(buffer); if (n < 0) break
                    if (out.size() + n > 512 * 1024) throw DirectApiFailure("模型回复过大，未自动重试")
                    out.write(buffer, 0, n)
                }; out.toByteArray().toString(Charsets.UTF_8)
            }
            val content = JSONObject(raw).getJSONArray("choices").getJSONObject(0).getJSONObject("message").getString("content").trim()
            val candidate = content.removePrefix("```json").removePrefix("```").removeSuffix("```").trim()
            val reply = runCatching { JSONObject(candidate).optString("reply").takeIf { it.isNotBlank() } }.getOrNull() ?: content
            if (reply.isBlank() || reply.length > 24000) throw DirectApiFailure("模型回复为空或超过 24000 字，未自动重试")
            reply.replace(config.apiKey, "[已隐藏密钥]")
        } catch (safe: DirectApiFailure) { throw safe }
        catch (_: Exception) { throw DirectApiFailure("模型请求未完成，可能已计费；未自动重试或转交电脑，请确认后重新发送") }
        finally { watchdog.cancel(false); bytes.fill(0); connection.disconnect() }
    }
    companion object {
        private val deadlines = Executors.newSingleThreadScheduledExecutor { task -> Thread(task, "aura-api-deadline").apply { isDaemon = true } }
    }
}

internal fun directMessages(config: DirectApiConfig, history: List<Message>, text: String, image: Pair<String, ByteArray>?,
    loadHistoricalImage: (String) -> Pair<String, ByteArray>? = { null }): JSONArray {
    val result = JSONArray().put(JSONObject().put("role", "system").put("content", config.systemPrompt.replace(config.apiKey, "[已隐藏密钥]")))
    var remaining = 48000
    val safe = history.filter { it.role in setOf("user", "assistant") && it.localState !in setOf("generating", "failed", "unknown", "needs_attention") }
        .takeLast(20).asReversed().takeWhile { remaining -= it.content.length; remaining >= 0 }.asReversed()
    // A reference may include one bounded historical image at its original
    // user turn. It must never become the current turn's attachment. Unrelated
    // text and a newly attached image do not resend older images.
    val historical = if (image == null && referencesHistoricalImage(text)) safe.lastOrNull {
        it.role == "user" && it.attachmentId != null
    } else null
    val historicalImage = historical?.attachmentId?.let(loadHistoricalImage)
    fun messageContent(value: String, attachment: Pair<String, ByteArray>?): Any {
        val safeText = value.replace(config.apiKey, "[已隐藏密钥]")
        return if (attachment == null) safeText else JSONArray()
            .put(JSONObject().put("type", "text").put("text", safeText))
            .put(JSONObject().put("type", "image_url").put("image_url", JSONObject().put("url",
                "data:${attachment.first};base64," + Base64.encodeToString(attachment.second, Base64.NO_WRAP))))
    }
    for (message in safe) result.put(JSONObject().put("role", message.role)
        .put("content", messageContent(message.content, if (message.id == historical?.id) historicalImage else null)))
    return result.put(JSONObject().put("role", "user").put("content", messageContent(text, image)))
}

internal fun referencesHistoricalImage(text: String): Boolean = Regex(
    "图片|照片|图像|截图|图中|图里|上图|这张图|那张图|上张图|上一张|附件|\\b(?:image|photo|picture|screenshot|attachment)s?\\b",
    RegexOption.IGNORE_CASE).containsMatchIn(text)
