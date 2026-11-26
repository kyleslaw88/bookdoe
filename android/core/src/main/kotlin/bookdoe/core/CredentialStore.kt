package bookdoe.core

/** Where per-server credentials live. The app uses Keystore-backed encrypted storage; tests use memory. */
interface CredentialStore {
    fun credential(serverId: String): Credential?
    fun save(credential: Credential, serverId: String)
    fun remove(serverId: String)
}

class InMemoryCredentialStore : CredentialStore {
    private val items = HashMap<String, Credential>()

    @Synchronized override fun credential(serverId: String): Credential? = items[serverId]

    @Synchronized override fun save(credential: Credential, serverId: String) {
        items[serverId] = credential
    }

    @Synchronized override fun remove(serverId: String) {
        items.remove(serverId)
    }
}
