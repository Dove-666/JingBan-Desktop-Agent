package com.aura.companion.transport

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Stores only AES-GCM ciphertext in app-private preferences; the AES key stays
 * in AndroidKeyStore. The application must disable Android backup globally.
 * A corrupt/restored identity fails closed instead of silently changing peers.
 */
class DeviceIdentityStore(context: Context) {
    private val app = context.applicationContext
    private val preferences by lazy {
        app.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
    }
    private val associatedData = "${app.packageName}:iroh-device-identity:v1".toByteArray(Charsets.UTF_8)

    /** Blocking disk/Keystore work: called only on the client's IO dispatcher. */
    fun loadOrCreate(): ByteArray = synchronized(lock) {
        try {
            val stored = preferences.getString(ENTRY, null)
            if (stored != null) return@synchronized decrypt(stored)
            val secret = ByteArray(32).also { SecureRandom().nextBytes(it) }
            try {
                val cipher = Cipher.getInstance("AES/GCM/NoPadding")
                cipher.init(Cipher.ENCRYPT_MODE, aesKey(create = true))
                cipher.updateAAD(associatedData)
                val iv = cipher.iv
                val ciphertext = cipher.doFinal(secret)
                val envelope = byteArrayOf(1, iv.size.toByte()) + iv + ciphertext
                if (!preferences.edit().putString(ENTRY, Base64.encodeToString(envelope, Base64.NO_WRAP)).commit()) {
                    throw IllegalStateException()
                }
                return@synchronized secret
            } catch (error: Exception) {
                secret.fill(0)
                throw error
            }
        } catch (_: Exception) {
            throw RpcTransportException("identity_unavailable", "无法读取安全设备密钥，请检查手机安全存储后重试。")
        }
    }

    private fun decrypt(value: String): ByteArray {
        if (value.length > 256) throw IllegalStateException()
        val envelope = Base64.decode(value, Base64.NO_WRAP)
        if (envelope.size != 62 || envelope[0] != 1.toByte() || envelope[1] != 12.toByte()) {
            throw IllegalStateException()
        }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, aesKey(create = false),
            GCMParameterSpec(128, envelope.copyOfRange(2, 14)))
        cipher.updateAAD(associatedData)
        val secret = cipher.doFinal(envelope.copyOfRange(14, envelope.size))
        if (secret.size != 32) {
            secret.fill(0)
            throw IllegalStateException()
        }
        return secret
    }

    private fun aesKey(create: Boolean): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        if (!create) throw IllegalStateException()
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(KeyGenParameterSpec.Builder(ALIAS,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .setRandomizedEncryptionRequired(true)
            .setUserAuthenticationRequired(false)
            .build())
        return generator.generateKey()
    }

    companion object {
        const val PREFERENCES = "aura_remote_identity"
        private const val ENTRY = "encrypted_secret_v1"
        private const val ALIAS = "com.aura.companion.iroh.identity.aes.v1"
        private val lock = Any()
    }
}
