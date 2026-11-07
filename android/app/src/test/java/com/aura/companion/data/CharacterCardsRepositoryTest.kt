package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import javax.crypto.KeyGenerator

/** Fixed paired-device RPC and a recording model only; no socket or real credential is used. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class CharacterCardsRepositoryTest {
    private val context get() = ApplicationProvider.getApplicationContext<Context>()
    private val alpha = "a".repeat(32)
    private val beta = "b".repeat(32)
    private fun digest(value: String) = MessageDigest.getInstance("SHA-256").digest(value.toByteArray())
        .joinToString("") { "%02x".format(it) }

    private class Vault : ApiConfigVault {
        var api: DirectApiConfig? = null
        var persona: SyncedPersona? = null
        var characters: CharacterCatalogCache? = null
        override fun load() = api
        override fun save(config: DirectApiConfig) { api = config }
        override fun clear() { api = null }
        override fun loadPersona() = persona
        override fun savePersona(persona: SyncedPersona) { this.persona = persona }
        override fun clearPersona() { persona = null }
        override fun loadCharacters() = characters
        override fun saveCharacters(catalog: CharacterCatalogCache) { characters = catalog }
        override fun clearCharacters() { characters = null }
    }

    private class Model : DirectChatGateway {
        val calls = mutableListOf<Pair<DirectApiConfig, JSONArray>>()
        override suspend fun complete(config: DirectApiConfig, messages: JSONArray): String {
            calls += config to messages
            return "owned reply"
        }
    }

    private inner class Rpc : RpcGateway {
        override val endpointId = "phone"
        override val connectionType = "direct"
        val calls = mutableListOf<Pair<String, JSONObject>>()
        var active = alpha
        var catalogVersion = 1
        var supportsCharacters = true
        var available = true
        var authorized = true
        var loseSelectAck = false
        var conflictNextSelection = false
        var malformedCatalog = false
        var listGate: CompletableDeferred<Unit>? = null
        val listEntered = CompletableDeferred<Unit>()
        fun revision() = digest("$catalogVersion:$active")
        fun name(id: String) = when (id) { alpha -> "Alpha"; beta -> "Beta"; else -> "鲸鱼娘" }
        fun persona(id: String) = JSONObject().put("version", 1).put("name", name(id))
            .put("revision", digest("persona:$id")).put("system_prompt", "role ${name(id)}")
        fun cardDigest(id: String) = if (id == "default") persona(id).getString("revision") else digest("card:$id")
        fun entry(id: String) = JSONObject().put("id", id).put("label", name(id)).put("name", name(id)).put("digest", cardDigest(id))
        fun active() = JSONObject().put("id", active).put("name", name(active)).put("digest", cardDigest(active)).put("persona", persona(active))
        fun page(offset: Int, limit: Int): JSONObject {
            val entries = listOf(entry(alpha), entry(beta))
            val end = minOf(offset + limit, entries.size)
            return JSONObject().put("version", 1).put("revision", revision()).put("total", entries.size)
                .put("items", JSONArray(entries.subList(offset, end))).put("next_offset", if (end == entries.size) JSONObject.NULL else end)
                .put("active", active())
        }
        override suspend fun connect(ticket: String) = Unit
        override suspend fun close() = Unit
        override suspend fun request(method: String, params: JSONObject, requestId: String): JSONObject {
            calls += method to JSONObject(params.toString())
            fun ok(value: JSONObject) = JSONObject().put("ok", true).put("result", value)
            fun fail(code: String) = JSONObject().put("ok", false).put("error", JSONObject().put("code", code).put("message", "PRIVATE SERVER TEXT"))
            if (!authorized) return fail("unauthorized")
            return when (method) {
                "status" -> ok(JSONObject().put("protocol_version", 1).put("capabilities", JSONArray(
                    listOf("chat.config", "chat.import", "chat.image") +
                        if (supportsCharacters) listOf("character.list", "character.get", "character.select") else emptyList<String>())))
                "chat.config" -> ok(JSONObject().put("version", 1).put("available", available)
                    .put("revision", "c".repeat(64)).put("persona", persona(active)).apply {
                        if (supportsCharacters) put("character_library", JSONObject().put("version", 1)
                            .put("revision", revision()).put("active_id", active))
                        if (available) put("base_url", "https://pc.example/v1").put("model", "pc-model")
                            .put("api_key", "SYNTHETIC_PC_ROLE_SECRET").put("system_prompt", "role ${name(active)}")
                            .put("reasoning_effort", "default").put("timeout_seconds", 30)
                    })
                "character.list" -> {
                    listGate?.let { listGate = null; listEntered.complete(Unit); it.await() }
                    if (params.has("revision") && params.getString("revision") != revision()) fail("character_conflict")
                    else ok(page(params.optInt("offset", 0), params.optInt("limit", 50)).apply {
                        if (malformedCatalog) remove("active")
                    })
                }
                "character.get" -> {
                    val id = params.getString("id")
                    ok(entry(id).put("version", 1).put("persona", persona(id)))
                }
                "character.select" -> {
                    if (conflictNextSelection) { conflictNextSelection = false; active = "default"; return fail("character_conflict") }
                    if (params.getString("expected_revision") != revision()) return fail("character_conflict")
                    val id = params.getString("id")
                    assertEquals(cardDigest(id), params.getString("digest"))
                    active = id
                    if (loseSelectAck) { loseSelectAck = false; throw IOException("PRIVATE LOST ACK") }
                    ok(JSONObject().put("version", 1).put("applied", true).put("active_id", active).put("revision", revision()))
                }
                "sync.snapshot" -> ok(JSONObject().put("items", JSONArray()).put("watermark", 0).put("has_more", false).put("next_cursor", ""))
                "sync.pull" -> ok(JSONObject().put("changes", JSONArray()).put("next_since", params.getLong("since")).put("has_more", false))
                "image.list" -> ok(JSONObject().put("items", JSONArray()).put("has_more", false).put("next_offset", 0))
                else -> error("Unexpected local fixture RPC: $method")
            }
        }
    }

    private fun db() = CompanionDatabase(context, null).apply {
        saveDevice(DeviceInfo("PC", "desktop", "public-ticket"), "phone")
        stageSnapshotPage(JSONObject().put("items", JSONArray()).put("watermark", 0)
            .put("has_more", false).put("next_cursor", ""), true)
    }
    private suspend fun await(check: () -> Boolean) = withTimeout(5000) { while (!check()) delay(10) }

    @Test fun phoneSelectionActuallyChangesDesktopRoleAndPreservesPhoneApiIncludingOfflineChat() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val model = Model()
        val repo = AppRepository(context, rpc, db(), vault, model)
        try {
            repo.reconnect()
            assertEquals(listOf(alpha, beta), repo.state.value.characterCards.map { it.id })
            assertEquals(alpha, repo.state.value.activeCharacterId)
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "SYNTHETIC_PHONE_ROLE_SECRET", 25, "default"))
            repo.selectCharacter(beta)
            assertEquals(beta, rpc.active); assertEquals(beta, repo.state.value.activeCharacterId)
            assertEquals("Beta", repo.state.value.activeCharacterName); assertEquals("role Beta", vault.api!!.systemPrompt)
            assertEquals("phone-model", vault.api!!.model); assertEquals("https://phone.example/v1", vault.api!!.baseUrl)
            assertEquals("SYNTHETIC_PHONE_ROLE_SECRET", vault.api!!.apiKey)
            assertTrue(model.calls.isEmpty())
            repo.disconnect()
            repo.sendMessage(repo.newConversation(), "offline role question")
            await { !repo.state.value.directChatBusy }
            assertEquals("role Beta", model.calls.single().second.getJSONObject(0).getString("content"))
            val before = rpc.calls.size
            repo.selectCharacter(alpha)
            assertEquals(before, rpc.calls.size); assertEquals(beta, repo.state.value.activeCharacterId)
            assertEquals("连接电脑后切换角色", repo.state.value.characterSyncError)
        } finally { repo.close() }
    }

    @Test fun defaultAndConcurrentDesktopChangesUseFreshReadbackWithoutAutomaticWriteReplay() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val repo = AppRepository(context, rpc, db(), vault, Model())
        try {
            repo.reconnect(); repo.selectCharacter("default")
            assertEquals("default", rpc.active); assertEquals("default", repo.state.value.activeCharacterId)
            rpc.conflictNextSelection = true
            repo.selectCharacter(beta)
            assertEquals("default", rpc.active); assertEquals("default", repo.state.value.activeCharacterId)
            assertEquals("电脑角色或角色库已更新，请重新选择", repo.state.value.characterSyncError)
            assertEquals(2, rpc.calls.count { it.first == "character.select" })
            assertFalse(repo.state.value.characterSyncError!!.contains("PRIVATE"))
        } finally { repo.close() }
    }

    @Test fun unchangedCatalogIsNotReloadedOnIdlePollAndRoleSyncDoesNotNeedDesktopApi() = runBlocking {
        var now = 1_000L
        val rpc = Rpc().apply { available = false }; val vault = Vault()
        val repo = AppRepository(context, rpc, db(), vault, Model(), monotonicMillis = { now })
        try {
            repo.reconnect(); val first = rpc.calls.count { it.first == "character.list" }
            assertEquals("Alpha", repo.state.value.activeCharacterName); assertNull(vault.api)
            now += 30_001; repo.poll()
            assertEquals(first, rpc.calls.count { it.first == "character.list" })
            rpc.active = beta; now += 30_001; repo.poll()
            assertEquals(first + 1, rpc.calls.count { it.first == "character.list" })
            assertEquals(beta, repo.state.value.activeCharacterId); assertEquals("role Beta", vault.persona!!.systemPrompt)
            assertNull(vault.api)
        } finally { repo.close() }
    }

    @Test fun malformedOptionalCatalogPreservesValidApiAndLastConfirmedRole() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val repo = AppRepository(context, rpc, db(), vault, Model())
        try {
            repo.reconnect(); rpc.malformedCatalog = true; rpc.catalogVersion++
            repo.refreshCharacters(); repo.refreshApiConfig()
            assertEquals(ConnectionState.CONNECTED, repo.state.value.connectionState)
            assertTrue(repo.state.value.directApi.available)
            assertEquals(alpha, repo.state.value.activeCharacterId)
            assertEquals("角色卡同步数据无效，请刷新后重试", repo.state.value.characterSyncError)
            assertEquals("role Alpha", vault.persona!!.systemPrompt)
            assertFalse(repo.state.value.characterSyncBusy)
        } finally { repo.close() }
    }

    @Test fun lostSelectionAckIsNotReplayedAndReconnectReadsActualDesktopOutcome() = runBlocking {
        val rpc = Rpc(); val vault = Vault(); val repo = AppRepository(context, rpc, db(), vault, Model())
        try {
            repo.reconnect(); rpc.loseSelectAck = true; repo.selectCharacter(beta)
            assertEquals(beta, rpc.active); assertEquals(alpha, repo.state.value.activeCharacterId)
            assertEquals(1, rpc.calls.count { it.first == "character.select" })
            repo.reconnect()
            assertEquals(beta, repo.state.value.activeCharacterId)
            assertEquals(1, rpc.calls.count { it.first == "character.select" })
            repo.selectCharacter(beta)
            assertEquals(1, rpc.calls.count { it.first == "character.select" })
        } finally { repo.close() }
    }

    @Test fun aLateCatalogFromThePreviousComputerCannotApplyOrEnterTheUi() = runBlocking {
        val database = db(); val rpc = Rpc(); val vault = Vault()
        val repo = AppRepository(context, rpc, database, vault, Model())
        val gate = CompletableDeferred<Unit>()
        try {
            repo.reconnect(); rpc.active = beta; rpc.listGate = gate
            val refresh = launch { repo.refreshCharacters() }
            withTimeout(5000) { rpc.listEntered.await() }
            database.saveDevice(DeviceInfo("Other PC", "other-desktop", "other-public-ticket"), "phone")
            gate.complete(Unit); refresh.join()
            assertEquals(alpha, vault.characters!!.active.id)
            assertEquals("role Alpha", vault.persona!!.systemPrompt)
            assertTrue(repo.state.value.characterCards.isEmpty())
            assertEquals("鲸鱼娘", repo.state.value.activeCharacterName)
            assertFalse(repo.state.value.characterLibrarySupported)
        } finally { gate.complete(Unit); repo.close() }
    }

    @Test fun encryptedRoleCacheBoundToAnotherPairingIsClearedAtStartup() = runBlocking {
        val rpc = Rpc(); val page = parseCharacterPage(rpc.page(0, 50), "old-desktop", 0, emptyList())
        val vault = Vault().apply {
            persona = page.active.persona
            characters = CharacterCatalogCache("old-desktop", page.revision, page.items, page.active)
        }
        val repo = AppRepository(context, rpc, db(), vault, Model())
        try {
            await { vault.characters == null }
            assertNull(vault.persona)
            assertTrue(repo.state.value.characterCards.isEmpty())
            assertEquals("鲸鱼娘", repo.state.value.activeCharacterName)
        } finally { repo.close() }
    }

    @Test fun oldDesktopAndRevocationDoNotBlockIndependentChatOrLeakPrivateRoleData() = runBlocking {
        val rpc = Rpc().apply { supportsCharacters = false }; val vault = Vault()
        val repo = AppRepository(context, rpc, db(), vault, Model())
        try {
            repo.reconnect(); assertFalse(repo.state.value.characterLibrarySupported)
            assertEquals("Alpha", repo.state.value.activeCharacterName)
            assertTrue(rpc.calls.none { it.first.startsWith("character.") })
            assertTrue(repo.saveDirectApiConfig("https://phone.example/v1", "phone-model", "SYNTHETIC_PHONE_ROLE_SECRET", 30, "default"))
            repo.refreshCharacters()
            assertTrue(repo.state.value.directApi.available)
            rpc.supportsCharacters = true; repo.reconnect()
            assertNotNull(vault.characters)
            rpc.authorized = false; repo.refreshCharacters()
            assertEquals(ConnectionState.UNAUTHORIZED, repo.state.value.connectionState)
            assertTrue(repo.state.value.characterCards.isEmpty()); assertNull(vault.characters)
            assertEquals("鲸鱼娘", repo.state.value.activeCharacterName)
            assertEquals("SYNTHETIC_PHONE_ROLE_SECRET", vault.api!!.apiKey)
            assertFalse(repo.state.value.toString().contains("role Alpha"))
        } finally { repo.close() }
    }

    @Test fun encryptedCatalogRestoresRoleMetadataAndCannotBeSwappedWithApiCiphertext() {
        val name = "owned-characters-${UUID.randomUUID()}.enc"
        val key = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val vault = EncryptedApiConfigVault(context, name) { key }
        val rpc = Rpc()
        val page = parseCharacterPage(rpc.page(0, 50), "desktop", 0, emptyList())
        val cache = CharacterCatalogCache("desktop", page.revision, page.items, page.active)
        val file = File(context.noBackupFilesDir, "$name.characters")
        try {
            vault.saveCharacters(cache)
            assertFalse(file.readBytes().toString(Charsets.UTF_8).contains("role Alpha"))
            val restored = EncryptedApiConfigVault(context, name) { key }.loadCharacters()!!
            assertEquals(alpha, restored.active.id); assertEquals(page.items, restored.items)
            assertEquals("role Alpha", restored.active.persona.systemPrompt)
            vault.save(DirectApiConfig("desktop", "r", "https://fixture.example/v1", "fixture", "PRIVATE", "prompt", 30, "default", 1))
            file.writeBytes(File(context.noBackupFilesDir, name).readBytes())
            assertNull(vault.loadCharacters()); assertFalse(file.exists()); assertNotNull(vault.load())
        } finally { vault.clearCharacters(); vault.clear() }
    }

    @Test fun paginationAndSecretBoundariesRejectCorruptIdentityWithoutExposingPrompt() {
        val rpc = Rpc(); val page = rpc.page(0, 1)
        assertEquals(1, parseCharacterPage(page, "desktop", 0, emptyList()).nextOffset)
        val secret = "SYNTHETIC_ROLE_SECRET"
        val metadata = rpc.page(0, 50)
        metadata.getJSONArray("items").getJSONObject(0).put("label", "role $secret")
        val safe = parseCharacterPage(metadata, "desktop", 0, listOf(secret))
        assertFalse(safe.items.first().label.contains(secret))
        assertFalse(safe.toString().contains("role Alpha"))
        page.put("next_offset", 0)
        try { parseCharacterPage(page, "desktop", 0, emptyList()); fail("stale cursor accepted") }
        catch (expected: IllegalArgumentException) { assertFalse(expected.message!!.contains(secret)) }
    }
}
