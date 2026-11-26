package bookdoe.app.data

import bookdoe.core.Credential
import bookdoe.core.CredentialStore
import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.serialization.json.Json

/**
 * Per-server credentials in EncryptedSharedPreferences: values are AES-256-GCM
 * encrypted with a key that lives in the Android Keystore, so no plaintext token is
 * ever written to disk. One entry per server id; the server list itself (plain
 * JSON) holds no secret.
 */
class EncryptedCredentialStore(context: Context, fileName: String = "bookdoe-credentials") : CredentialStore {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val prefs: SharedPreferences by lazy {
        val key = MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build()
        EncryptedSharedPreferences.create(
            context, fileName, key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    override fun credential(serverId: String): Credential? =
        prefs.getString(serverId, null)?.let { runCatching { json.decodeFromString(Credential.serializer(), it) }.getOrNull() }

    override fun save(credential: Credential, serverId: String) {
        prefs.edit().putString(serverId, json.encodeToString(Credential.serializer(), credential)).apply()
    }

    override fun remove(serverId: String) {
        prefs.edit().remove(serverId).apply()
    }
}
