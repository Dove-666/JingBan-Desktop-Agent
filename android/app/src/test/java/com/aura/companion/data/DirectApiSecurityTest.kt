package com.aura.companion.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.io.IOException
import java.security.KeyStoreException
import java.net.ServerSocket
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.crypto.spec.SecretKeySpec

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], manifest = Config.NONE)
class DirectApiSecurityTest {
    private fun config(base: String = "https://example.test/v1") = DirectApiConfig("desktop", "a".repeat(64), base, "model", "sensitive-key-value",
        "private system", 2, "default", 123)

    @Test fun vaultPersistsOnlyAuthenticatedCiphertextAndTamperClearsRatherThanLoadingSecrets() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val key = SecretKeySpec(ByteArray(32) { (it + 1).toByte() }, "AES")
        val vault = EncryptedApiConfigVault(context) { key }
        val file = File(context.noBackupFilesDir, "direct-api-v1.enc")
        try {
            vault.clear(); vault.save(config())
            val first = file.readBytes()
            assertFalse(first.toString(Charsets.ISO_8859_1).contains("sensitive-key-value"))
            assertFalse(first.toString(Charsets.ISO_8859_1).contains("example.test"))
            assertEquals("sensitive-key-value", vault.load()!!.apiKey)
            vault.save(config()); assertFalse(first.contentEquals(file.readBytes()))
            val changed = file.readBytes(); changed[changed.lastIndex] = (changed.last().toInt() xor 1).toByte(); file.writeBytes(changed)
            assertNull(vault.load()); assertFalse(file.exists())
            assertFalse(config().toString().contains("sensitive-key-value"))
        } finally { vault.clear() }
    }

    @Test fun httpClientDoesOneRequestWithoutFollowingCredentialLeakingRedirect() = runBlocking {
        val server = ServerSocket(0); val worker = Executors.newSingleThreadExecutor()
        var requests = 0
        val future = worker.submit {
            server.accept().use { socket ->
                requests++
                val input = socket.getInputStream().bufferedReader()
                var line = input.readLine(); var length = 0
                while (line.isNotEmpty()) { if (line.startsWith("Content-Length:", true)) length = line.substringAfter(':').trim().toInt(); line = input.readLine() }
                val body = CharArray(length); var received = 0
                while (received < length) received += input.read(body, received, length - received)
                assertEquals("model", JSONObject(String(body)).getString("model"))
                socket.getOutputStream().write("HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/leak\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".toByteArray())
            }
        }
        try {
            try { HttpDirectChatGateway().complete(config("http://127.0.0.1:${server.localPort}/v1"), JSONArray()); fail("Redirect must fail") }
            catch (failure: DirectApiFailure) { assertTrue(failure.safeMessage.contains("重定向")); assertFalse(failure.safeMessage.contains("sensitive-key-value")) }
            future.get(5, TimeUnit.SECONDS); assertEquals(1, requests)
        } finally { server.close(); worker.shutdownNow() }
    }

    @Test fun oldCiphertextDefaultsToDesktopAndManualBackupRoundTripsWithoutPublishingKeys() {
        val old = JSONObject(config().encoded().toString(Charsets.UTF_8)).apply { remove("source"); remove("desktop_cache") }
        assertEquals(ApiConfigSource.DESKTOP, DirectApiConfig.decode(old.toString().toByteArray()).source)
        val context = ApplicationProvider.getApplicationContext<Context>()
        val vault = EncryptedApiConfigVault(context, "manual-backup-test.enc") { SecretKeySpec(ByteArray(32) { 7 }, "AES") }
        val manual = DirectApiConfig(LOCAL_PHONE_AUTHORITY, "manual", "https://phone.example/v1", "phone-model", "PHONE_SYNTHETIC_SECRET",
            "prompt", 30, "low", 123, ApiConfigSource.LOCAL, config())
        try {
            vault.save(manual); val loaded = vault.load()!!
            assertEquals(ApiConfigSource.LOCAL, loaded.source); assertEquals("PHONE_SYNTHETIC_SECRET", loaded.apiKey)
            assertEquals(ApiConfigSource.DESKTOP, loaded.desktopCache!!.source)
            assertEquals("sensitive-key-value", loaded.desktopCache!!.apiKey)
            assertFalse(loaded.publicInfo().toString().contains("PHONE_SYNTHETIC_SECRET"))
            assertFalse(loaded.publicInfo().toString().contains("sensitive-key-value"))
        } finally { vault.clear() }
    }

    @Test fun atomicBackupIsRecoveredWhenProcessStoppedBeforeReplacingBaseFile() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "atomic-backup-test.enc"; val file = File(context.noBackupFilesDir, name)
        val vault = EncryptedApiConfigVault(context, name) { SecretKeySpec(ByteArray(32) { 5 }, "AES") }
        try {
            vault.save(config()); val ciphertext = file.readBytes()
            File(file.path + ".bak").writeBytes(ciphertext)
            assertTrue(file.delete()); assertFalse(file.exists())
            assertEquals("sensitive-key-value", vault.load()!!.apiKey)
            assertTrue(file.isFile); assertArrayEquals(ciphertext, file.readBytes())
        } finally { vault.clear() }
    }

    @Test fun temporaryKeystoreOrIoFailureRetainsCiphertextForSuccessfulRetry() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "transient-vault-test.enc"; val file = File(context.noBackupFilesDir, name)
        val key = SecretKeySpec(ByteArray(32) { 6 }, "AES"); var failure: String? = null
        val vault = EncryptedApiConfigVault(context, name) {
            when (failure) { "io" -> throw IOException("temporary provider I/O"); "keystore" -> throw KeyStoreException("temporarily locked") }
            key
        }
        try {
            vault.save(config()); val ciphertext = file.readBytes()
            for (kind in listOf("io", "keystore")) {
                failure = kind; assertNull(vault.load()); assertTrue(file.isFile); assertArrayEquals(ciphertext, file.readBytes())
                failure = null; assertEquals("sensitive-key-value", vault.load()!!.apiKey)
            }
        } finally { vault.clear() }
    }

    @Test fun personaCacheIsEncryptedIndependentOfApiAndCannotBeSwappedWithCredentialCiphertext() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val name = "shared-role-test.enc"
        val key = SecretKeySpec(ByteArray(32) { 8 }, "AES")
        val vault = EncryptedApiConfigVault(context, name) { key }
        val file = File(context.noBackupFilesDir, "$name.persona")
        try {
            vault.savePersona(SyncedPersona("desktop", "role-v1", "Luna", "PRIVATE_ROLE_PROMPT"))
            val bytes = file.readBytes()
            assertFalse(bytes.toString(Charsets.ISO_8859_1).contains("PRIVATE_ROLE_PROMPT"))
            assertNull(vault.load())
            val reopened = EncryptedApiConfigVault(context, name) { key }
            assertEquals("PRIVATE_ROLE_PROMPT", reopened.loadPersona()!!.systemPrompt)
            vault.save(config()); vault.clear()
            assertEquals("Luna", vault.loadPersona()!!.name)
            vault.save(config()); file.writeBytes(File(context.noBackupFilesDir, name).readBytes())
            assertNull(vault.loadPersona()); assertFalse(file.exists())
            assertEquals("sensitive-key-value", vault.load()!!.apiKey)
        } finally { vault.clear(); vault.clearPersona() }
    }
}
