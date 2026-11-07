package com.aura.companion.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import android.util.Base64
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.security.KeyStore
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.math.ceil
import javax.crypto.Cipher
import javax.crypto.AEADBadTagException
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import org.json.JSONException

/** Role identity is independent of the selected phone/desktop model provider. */
internal class SyncedPersona(val authorityId: String, val revision: String, val name: String,
    val systemPrompt: String) {
    override fun toString() = "SyncedPersona([private])"
    fun encoded(): ByteArray = JSONObject().put("version", 1).put("authority_id", authorityId)
        .put("revision", revision).put("name", name).put("system_prompt", systemPrompt)
        .toString().toByteArray(Charsets.UTF_8)
    companion object {
        fun fromRpc(value: JSONObject, authority: String, secrets: List<String> = emptyList()): SyncedPersona {
            require(value.optInt("version") == 1) { "电脑角色版本不兼容，请更新两端应用" }
            fun text(key: String, limit: Int): String {
                val raw = value.opt(key)
                require(raw is String && raw.isNotBlank() && raw.length <= limit && '\u0000' !in raw) { "电脑角色信息无效" }
                return redactKnownText(raw, secrets)
            }
            // Desktop card names allow 128 Unicode code points (up to 256 UTF-16 units).
            return SyncedPersona(authority, text("revision", 128), text("name", 256), text("system_prompt", 32768))
        }
        fun decode(bytes: ByteArray): SyncedPersona = JSONObject(bytes.toString(Charsets.UTF_8)).let {
            fromRpc(it, it.getString("authority_id"))
        }
    }
}

/** Deliberately not a data class: neither toString nor generated copy methods expose credentials. */
internal class DirectApiConfig(
    val authorityId: String, val revision: String, val baseUrl: String, val model: String,
    val apiKey: String, val systemPrompt: String, val timeoutSeconds: Int,
    val reasoningEffort: String, val syncedAt: Long,
    val source: ApiConfigSource = ApiConfigSource.DESKTOP,
    val desktopCache: DirectApiConfig? = null,
    private val redactionSecrets: List<String> = emptyList(),
    val reasoningProfile: String = if (reasoningEffort == "default") "auto" else "legacy",
    val reasoningBudgetTokens: Int = 0,
    val reasoningMetadata: JSONObject? = null,
) {
    override fun toString() = "DirectApiConfig([private])"
    fun knownSecrets(): List<String> = (listOf(apiKey) + desktopCache?.knownSecrets().orEmpty() + redactionSecrets).filter { it.isNotBlank() }.distinct()
    fun redact(value: String): String = redactKnownText(value, knownSecrets())
    /** Per-request masks live in RAM only, never in encoded configuration. */
    fun withRedactionSecrets(secrets: List<String>): DirectApiConfig = DirectApiConfig(authorityId, revision, baseUrl, model,
        apiKey, systemPrompt, timeoutSeconds, reasoningEffort, syncedAt, source, desktopCache, secrets,
        reasoningProfile, reasoningBudgetTokens, reasoningMetadata)
    private fun publicSettingText(): List<String> = listOf(baseUrl, model, reasoningEffort, reasoningProfile) +
        reasoningMetadata?.let { value -> buildList {
            fun collect(raw: Any?) {
                when (raw) {
                    is String -> add(raw)
                    is JSONObject -> for (name in raw.keys()) collect(raw.opt(name))
                    is JSONArray -> for (index in 0 until raw.length()) collect(raw.opt(index))
                }
            }
            collect(value)
        } }.orEmpty()

    fun requireSafePublicSettings(secrets: List<String> = knownSecrets()) {
        val fields = publicSettingText()
        require((knownSecrets() + secrets).filter { it.isNotBlank() }.none { key -> fields.any { it.contains(key) } }) {
            "模型公开参数不能包含已保存的 API 密钥，请重新配置。"
        }
    }

    fun publicInfo(secrets: List<String> = knownSecrets()): ApiConnectionInfo {
        val privateValues = (knownSecrets() + secrets).filter { it.isNotBlank() }.distinct()
        val safe = runCatching { requireSafePublicSettings(privateValues) }.isSuccess
        return ApiConnectionInfo(safe, redactKnownText(baseUrl, privateValues), redactKnownText(model, privateValues),
            if (safe) ApiConfigSyncState.READY else ApiConfigSyncState.ERROR, syncedAt,
            if (!safe) "模型参数中含已保存密钥，请重新配置模型。" else if (source == ApiConfigSource.LOCAL)
                "手机独立模型，无需连接电脑即可对话" else "手机直接连接模型；电脑离线时仍可聊天，记录稍后同步",
            source = source, keySaved = true, timeoutSeconds = timeoutSeconds,
            reasoningEffort = if (safe) reasoningEffort else "default", reasoningProfile = if (safe) reasoningProfile else "auto",
            reasoningBudgetTokens = if (safe) reasoningBudgetTokens else 0,
            reasoningMetadata = if (safe) ModelReasoning.sanitizedMetadata(baseUrl, model, reasoningMetadata)?.toString() else null)
    }

    fun withDesktopCache(config: DirectApiConfig?): DirectApiConfig = DirectApiConfig(authorityId, revision, baseUrl, model,
        apiKey, redactKnownText(systemPrompt, knownSecrets() + config?.knownSecrets().orEmpty()), timeoutSeconds, reasoningEffort, syncedAt, source, config,
        reasoningProfile = reasoningProfile, reasoningBudgetTokens = reasoningBudgetTokens, reasoningMetadata = reasoningMetadata)

    fun withPersona(persona: SyncedPersona): DirectApiConfig = DirectApiConfig(authorityId, revision, baseUrl, model,
        apiKey, redact(persona.systemPrompt), timeoutSeconds, reasoningEffort, syncedAt, source, desktopCache, redactionSecrets,
        reasoningProfile, reasoningBudgetTokens, reasoningMetadata)

    fun reasoningSettings(): JSONObject = ModelReasoning.selection(reasoningProfile, reasoningEffort,
        reasoningBudgetTokens, reasoningMetadata)

    fun encoded(): ByteArray = JSONObject().put("authority_id", authorityId).put("revision", revision)
        .put("base_url", baseUrl).put("model", model).put("api_key", apiKey).put("system_prompt", systemPrompt)
        .put("timeout_seconds", timeoutSeconds).put("reasoning_effort", reasoningEffort).put("synced_at", syncedAt)
        .put("source", source.name).put("reasoning", reasoningSettings())
        .apply { desktopCache?.let { put("desktop_cache", JSONObject(it.encoded().toString(Charsets.UTF_8))) } }
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
            val reasoning = ModelReasoning.parseSelection(value.opt("reasoning"), value.optString("reasoning_effort", "default"), base, model)
            val timeout = value.optDouble("timeout_seconds", 30.0)
            require(timeout.isFinite() && timeout in 1.0..120.0) { "电脑 API 超时配置无效" }
            return DirectApiConfig(authority, text("revision", 128), base, model, key,
                redactKnownText(text("system_prompt", 32768), listOf(key)), ceil(timeout).toInt(), reasoning.getString("value"), System.currentTimeMillis(),
                reasoningProfile = reasoning.getString("profile"), reasoningBudgetTokens = reasoning.getInt("budget_tokens"),
                reasoningMetadata = reasoning.optJSONObject("metadata")).also { it.requireSafePublicSettings() }
        }

        fun decode(bytes: ByteArray): DirectApiConfig {
            val json = JSONObject(bytes.toString(Charsets.UTF_8))
            // Stored data was validated on receipt and is authenticated by GCM.
            val reasoning = ModelReasoning.parseSelection(json.opt("reasoning"), json.getString("reasoning_effort"),
                json.getString("base_url"), json.getString("model"))
            return DirectApiConfig(json.getString("authority_id"), json.getString("revision"), json.getString("base_url"),
                json.getString("model"), json.getString("api_key"), json.getString("system_prompt"), json.getInt("timeout_seconds"),
                reasoning.getString("value"), json.getLong("synced_at"),
                if (json.optString("source") == "LOCAL") ApiConfigSource.LOCAL else ApiConfigSource.DESKTOP,
                json.optJSONObject("desktop_cache")?.let { decode(it.toString().toByteArray(Charsets.UTF_8)) },
                reasoningProfile = reasoning.getString("profile"), reasoningBudgetTokens = reasoning.getInt("budget_tokens"),
                reasoningMetadata = reasoning.optJSONObject("metadata"))
        }
    }
}

internal interface ApiConfigVault {
    fun load(): DirectApiConfig?
    fun save(config: DirectApiConfig)
    fun clear()
    fun loadPersona(): SyncedPersona? = null
    fun savePersona(persona: SyncedPersona) = Unit
    fun clearPersona() = Unit
    fun loadCharacters(): CharacterCatalogCache? = null
    fun saveCharacters(catalog: CharacterCatalogCache) = Unit
    fun clearCharacters() = Unit
}

/** Only ciphertext is written. AES key remains non-exportable in Android Keystore. */
internal class EncryptedApiConfigVault(context: Context, storageName: String = "direct-api-v1.enc",
    private val keyProvider: (() -> SecretKey)? = null) : ApiConfigVault {
    private val file = AtomicFile(File(context.noBackupFilesDir, storageName))
    private val aad = "aura.direct-api.v1".toByteArray(Charsets.UTF_8)
    private val personaFile = AtomicFile(File(context.noBackupFilesDir, "$storageName.persona"))
    private val personaAad = "aura.synced-persona.v1".toByteArray(Charsets.UTF_8)
    private val characterFile = AtomicFile(File(context.noBackupFilesDir, "$storageName.characters"))
    private val characterAad = "aura.synced-character-catalog.v1".toByteArray(Charsets.UTF_8)
    private fun key(): SecretKey = keyProvider?.invoke() ?: synchronized(EncryptedApiConfigVault::class.java) {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey) ?: KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).setRandomizedEncryptionRequired(true).build())
            generateKey()
        }
    }
    @Synchronized override fun load(): DirectApiConfig? = readEncrypted(file, aad, DirectApiConfig::decode)
    @Synchronized override fun loadPersona(): SyncedPersona? = readEncrypted(personaFile, personaAad, SyncedPersona::decode)
    @Synchronized override fun loadCharacters(): CharacterCatalogCache? =
        readEncrypted(characterFile, characterAad, CharacterCatalogCache::decode, MAX_CHARACTER_CACHE_BYTES + 29)

    private fun <T> readEncrypted(file: AtomicFile, aad: ByteArray, decode: (ByteArray) -> T, maximumBytes: Int = 524288): T? {
        // openRead performs AtomicFile's .bak recovery before checking size.
        // Checking baseFile first can hide a valid cache after a process crash.
        val encoded = try {
            file.openRead().use { input ->
                if (input.channel.size() !in 29..maximumBytes.toLong()) throw InvalidCacheFormat()
                input.readBytes()
            }
        } catch (_: InvalidCacheFormat) { file.delete(); return null }
        catch (_: Exception) { return null } // Temporary I/O is not proof of corruption.
        if (encoded.size !in 29..maximumBytes || encoded[0] != 1.toByte()) { file.delete(); return null }
        val plaintext = try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, encoded.copyOfRange(1, 13)))
            cipher.updateAAD(aad)
            cipher.doFinal(encoded.copyOfRange(13, encoded.size))
        } catch (_: AEADBadTagException) { file.delete(); return null }
        catch (_: Exception) { return null } // Locked/unavailable Keystore: retain ciphertext.
        return try { decode(plaintext) }
        catch (_: JSONException) { file.delete(); null }
        catch (_: IllegalArgumentException) { file.delete(); null }
        finally { plaintext.fill(0) }
    }
    @Synchronized override fun save(config: DirectApiConfig) = writeEncrypted(file, aad, config.encoded())
    @Synchronized override fun savePersona(persona: SyncedPersona) = writeEncrypted(personaFile, personaAad, persona.encoded())
    @Synchronized override fun saveCharacters(catalog: CharacterCatalogCache) =
        writeEncrypted(characterFile, characterAad, catalog.encoded())

    private fun writeEncrypted(file: AtomicFile, aad: ByteArray, plaintext: ByteArray) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key()); cipher.updateAAD(aad)
        val ciphertext = try { cipher.doFinal(plaintext) } finally { plaintext.fill(0) }
        val stream = file.startWrite()
        try { stream.write(byteArrayOf(1) + cipher.iv + ciphertext); file.finishWrite(stream) }
        catch (failure: Exception) { file.failWrite(stream); throw failure }
    }
    @Synchronized override fun clear() { file.delete() }
    @Synchronized override fun clearPersona() { personaFile.delete() }
    @Synchronized override fun clearCharacters() { characterFile.delete() }
    companion object { private const val KEY_ALIAS = "aura-direct-api-aes-v1" }
    private class InvalidCacheFormat : Exception()
}

internal class DirectApiFailure(val safeMessage: String) : Exception(safeMessage)
internal interface DirectChatGateway {
    suspend fun complete(config: DirectApiConfig, messages: JSONArray): String
    suspend fun stream(config: DirectApiConfig, messages: JSONArray, onUpdate: (DirectChatUpdate) -> Unit): String =
        complete(config, messages).also { onUpdate(DirectChatUpdate(it)) }
}

/** One HTTP request, no SDK retry, redirects or fallback that could duplicate billing. */
internal class HttpDirectChatGateway : DirectChatGateway {
    override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String = stream(config, messages) {}

    override suspend fun stream(config: DirectApiConfig, messages: JSONArray,
        onUpdate: (DirectChatUpdate) -> Unit): String = suspendCancellableCoroutine { continuation ->
        val activeConnection = AtomicReference<HttpURLConnection?>()
        val future = requests.submit {
            try {
                val reply = request(config, messages, { update -> if (continuation.isActive) onUpdate(update) }, activeConnection)
                if (continuation.isActive) continuation.resume(reply)
            } catch (failure: Exception) {
                if (continuation.isActive) continuation.resumeWithException(if (failure is DirectApiFailure) failure else
                    DirectApiFailure("模型请求未完成，可能已计费；未自动重试或转交电脑，请确认后重新发送"))
            }
        }
        continuation.invokeOnCancellation {
            // Closing the socket also interrupts a blocked read. Coroutine
            // cancellation alone cannot interrupt HttpURLConnection I/O.
            activeConnection.get()?.disconnect()
            future.cancel(true)
        }
    }

    private fun request(config: DirectApiConfig, messages: JSONArray, onUpdate: (DirectChatUpdate) -> Unit,
        activeConnection: AtomicReference<HttpURLConnection?>): String {
        val payload = JSONObject().put("model", config.model).put("messages", messages).put("stream", true)
        config.requireSafePublicSettings()
        val reasoning = ModelReasoning.body(config.baseUrl, config.model, config.reasoningEffort,
            config.reasoningProfile, config.reasoningBudgetTokens, config.reasoningMetadata)
        for (name in reasoning.keys()) payload.put(name, reasoning.get(name))
        // No invented provider-wide output limit. The selected model/provider
        // owns its capacity; an explicit shared policy may set one separately.
        val bytes = payload.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 8 * 1024 * 1024) { "模型请求超过大小限制，请减少历史或图片" }
        val connection = URI(config.baseUrl.trimEnd('/') + "/chat/completions").toURL().openConnection() as HttpURLConnection
        activeConnection.set(connection)
        if (Thread.currentThread().isInterrupted) { connection.disconnect(); throw DirectApiFailure("模型请求已停止，未自动重试") }
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(config.timeoutSeconds.toLong())
        val watchdog = deadlines.schedule({ connection.disconnect() }, config.timeoutSeconds.toLong(), TimeUnit.SECONDS)
        try {
            connection.requestMethod = "POST"; connection.doOutput = true; connection.instanceFollowRedirects = false
            connection.connectTimeout = config.timeoutSeconds * 1000; connection.readTimeout = config.timeoutSeconds * 1000
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.setRequestProperty("Accept", "text/event-stream")
            connection.setRequestProperty("Authorization", "Bearer ${config.apiKey}")
            connection.setFixedLengthStreamingMode(bytes.size)
            connection.outputStream.use { it.write(bytes) }
            if (connection.responseCode !in 200..299) throw DirectApiFailure(when (connection.responseCode) {
                401, 403 -> "模型 API 拒绝了密钥或权限，请检查手机模型配置中的密钥与权限"
                429 -> "模型 API 配额或频率受限，请稍后确认再发送"
                in 300..399 -> "模型 API 要求重定向，为保护密钥已停止，请检查手机模型配置中的 API 地址"
                else -> "模型 API 返回错误；未转交电脑重试，请确认后重新发送"
            })
            fun checkDeadline() {
                val remaining = TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime())
                if (remaining <= 0 || Thread.currentThread().isInterrupted) throw DirectApiFailure("模型请求超时或已停止，可能已计费；不会自动重试，请确认后重新发送")
                connection.readTimeout = remaining.coerceAtMost(config.timeoutSeconds * 1000L).toInt().coerceAtLeast(1)
            }
            if (connection.contentType?.substringBefore(';')?.trim()?.lowercase() == "text/event-stream") {
                return connection.inputStream.bufferedReader(Charsets.UTF_8).use { reader ->
                    readDirectEvents(reader, config.knownSecrets(), onUpdate, ::checkDeadline)
                }
            }
            // Some compatible servers ignore stream=true. Accept their one
            // complete JSON response, without retrying a second request.
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
            val choice = JSONObject(raw).getJSONArray("choices").getJSONObject(0)
            if (choice.opt("finish_reason") != "stop") throw DirectApiFailure("模型回复未正常完成；未自动重试")
            val content = choice.getJSONObject("message").getString("content")
            return DirectVisibleText(config.knownSecrets(), onUpdate).run { append(content); finish() }
        } catch (safe: DirectApiFailure) { throw safe }
        catch (_: Exception) { throw DirectApiFailure("模型请求未完成，可能已计费；未自动重试或转交电脑，请确认后重新发送") }
        finally { watchdog.cancel(false); bytes.fill(0); activeConnection.compareAndSet(connection, null); connection.disconnect() }
    }
    companion object {
        private val deadlines = Executors.newSingleThreadScheduledExecutor { task -> Thread(task, "aura-api-deadline").apply { isDaemon = true } }
        private val requests = Executors.newCachedThreadPool { task -> Thread(task, "aura-api-stream").apply { isDaemon = true } }
    }
}

internal fun directMessages(config: DirectApiConfig, history: List<Message>, text: String, image: Pair<String, ByteArray>?,
    sharedContext: ContextEnvelope? = null,
    loadHistoricalImage: (String) -> Pair<String, ByteArray>? = { null }): JSONArray {
    val result = JSONArray().put(JSONObject().put("role", "system").put("content", config.redact(config.systemPrompt)))
    val cid = history.firstOrNull()?.conversationId ?: sharedContext?.conversationId ?: "phone-new-conversation"
    val envelope = ContextEnvelope.local(cid, history, text)
    val safe = history.filter { it.id in envelope.recentIds }
    val localReference = envelope.json.getJSONObject("summary").getString("text").isNotBlank()
    if (localReference || sharedContext != null) {
        // These are history extracts in a user reference message, never new
        // system instructions or authorization to use computer tools.
        val references = listOfNotNull(sharedContext?.referenceText(), envelope.referenceText().takeIf { localReference })
            .joinToString("\n").let(config::redact)
        result.put(JSONObject().put("role", "user").put("content", references))
    }
    // A reference may include one bounded historical image at its original
    // user turn. It must never become the current turn's attachment. Unrelated
    // text and an ordinary new image do not resend older images. An explicit
    // two-image comparison keeps one old image at its original user turn.
    val comparingImages = comparesWithHistoricalImage(text)
    val requiredHistory = if (comparingImages && image == null) 2 else 1
    val historical = if (comparingImages || image == null && referencesHistoricalImage(text)) safe.filter {
        it.role == "user" && it.attachmentId != null
    }.takeLast(requiredHistory) else emptyList()
    val historicalImages = historical.associate { it.id to it.attachmentId?.let(loadHistoricalImage) }
    // Validate before the repository consumes the draft image or creates a
    // turn. A two-image request with missing pixels cannot be fulfilled.
    require(!comparingImages || historical.size == requiredHistory && historicalImages.values.all { it != null }) {
        "无法读取上一张图片，请重新选择图片后再比较。"
    }
    fun messageContent(value: String, attachment: Pair<String, ByteArray>?): Any {
        val safeText = config.redact(value)
        return if (attachment == null) safeText else JSONArray()
            .put(JSONObject().put("type", "text").put("text", safeText))
            .put(JSONObject().put("type", "image_url").put("image_url", JSONObject().put("url",
                "data:${attachment.first};base64," + Base64.encodeToString(attachment.second, Base64.NO_WRAP))))
    }
    for (message in safe) result.put(JSONObject().put("role", message.role)
        .put("content", messageContent(message.content, historicalImages[message.id])))
    return result.put(JSONObject().put("role", "user").put("content", messageContent(text, image)))
}

internal fun referencesHistoricalImage(text: String): Boolean = Regex(
    "上图|图[中里]|(?:上(?:面)?(?:那|一)?张|[这那]张)(?:图(?:片|像)?|照片|截图)|" +
        "\\b(?:(?:this|that|previous|last|above)\\s+|(?:in|on)\\s+the\\s+)(?:image|photo|picture|screenshot|attachment)s?\\b",
    RegexOption.IGNORE_CASE).containsMatchIn(text)

internal fun comparesWithHistoricalImage(text: String): Boolean {
    val previous = Regex(
        "上图|前图|(?:上(?:面)?|前(?:面)?|之前|刚才)(?:的|那|一)?张|" +
            "(?:这|那)?两(?:张|幅)(?:图片|图像|图(?!表|纸)|照片|截图)|" +
            "\\b(?:previous|last|earlier|above)\\s+(?:image|photo|picture|screenshot|attachment|one)s?\\b|" +
            "\\b(?:(?:these|the)\\s+)?(?:two|2)\\s+(?:image|photo|picture|screenshot|attachment)s\\b|" +
            "\\bboth\\s+(?:images|photos|pictures|screenshots|attachments)\\b", RegexOption.IGNORE_CASE)
    val comparison = Regex(
        "对比|比较|相比|比起|比一比|不同|区别|差异|变化|一样|相同|哪个.{0,6}(?:更|好|贵|便宜|清晰|清楚|合适)|" +
            "\\b(?:compare(?:d|s)?|comparison|versus|vs|differ(?:ent|ences?|s)?|change(?:d|s)?|same|similar)\\b",
        RegexOption.IGNORE_CASE)
    return previous.containsMatchIn(text) && comparison.containsMatchIn(text)
}
