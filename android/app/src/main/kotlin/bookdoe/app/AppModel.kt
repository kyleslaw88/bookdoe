package bookdoe.app

import bookdoe.core.BookdoeProvider
import bookdoe.core.ApiError
import bookdoe.core.AuthResult
import bookdoe.core.CredentialStore
import bookdoe.core.DevicePairing
import bookdoe.core.HttpClient
import bookdoe.core.MediaProvider
import bookdoe.core.PlaybackPosition
import bookdoe.core.PlaybackSession
import bookdoe.core.ProgressQueue
import bookdoe.core.ProgressUpdate
import bookdoe.core.ProviderFactory
import bookdoe.core.ServerKind
import bookdoe.core.ServerRecord
import bookdoe.core.ServerStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/**
 * App-wide state: saved servers, the active provider and the progress queue. Pure
 * Kotlin so it is unit-testable; Android pieces (player, downloads) hook in through
 * the callbacks.
 */
class AppModel(
    val serverStore: ServerStore,
    val credentials: CredentialStore,
    client: HttpClient,
    val progressQueue: ProgressQueue,
    val appVersion: String,
    val deviceName: String,
    scope: CoroutineScope,
) {
    val factory = ProviderFactory(client, credentials, appVersion)

    val servers: StateFlow<List<ServerRecord>> = serverStore.state.map { it.servers }.stateIn(scope, SharingStarted.Eagerly, serverStore.servers)
    val activeServer: StateFlow<ServerRecord?> = serverStore.state.map { s -> s.servers.firstOrNull { it.id == s.activeServerId } }
        .stateIn(scope, SharingStarted.Eagerly, serverStore.active)
    val lastError = MutableStateFlow<String?>(null)

    /** Called when a flush returns a server position the player should adopt. */
    var onConflict: ((ProgressUpdate, PlaybackPosition) -> Unit)? = null

    /** Called before a server is forgotten so the player and downloads can clean up. */
    var onServerRemoved: (suspend (String) -> Unit)? = null

    private var cachedProvider: Pair<ServerRecord, MediaProvider>? = null

    /**
     * Provider for the active server (rebuilt when the record changes). Reads the
     * store synchronously so it never lags behind a just-saved server.
     */
    val provider: MediaProvider?
        get() {
            val server = serverStore.active ?: return null
            cachedProvider?.let { (rec, p) -> if (rec == server) return p }
            val p = factory.make(server)
            cachedProvider = server to p
            return p
        }

    fun providerFor(server: ServerRecord): MediaProvider = if (server == serverStore.active) provider ?: factory.make(server) else factory.make(server)

    // servers

    fun setActive(server: ServerRecord) = serverStore.setActive(server.id)

    /** Probe, sign in and save a server. Returns the saved record. */
    suspend fun addServer(kind: ServerKind, name: String, url: String, username: String, password: String): ServerRecord {
        val record = ServerRecord(name = name.ifBlank { kind.displayName }, kind = kind, baseUrl = url, username = username)
        val p = factory.make(record)
        val result = p.login(username, password, deviceName)
        credentials.save(result.credential, record.id)
        serverStore.add(result.server)
        serverStore.setActive(record.id)
        return result.server
    }

    /** bookdoe pairing: start, then poll until the person approves. */
    suspend fun startPairing(name: String, url: String): Triple<ServerRecord, BookdoeProvider, DevicePairing> {
        val record = ServerRecord(name = name.ifBlank { "bookdoe" }, kind = ServerKind.bookdoe, baseUrl = url, username = "")
        val p = factory.make(record) as? BookdoeProvider ?: throw ApiError.NotSupported("pairing is bookdoe only")
        return Triple(record, p, p.startPairing(deviceName))
    }

    fun finishPairing(record: ServerRecord, result: AuthResult) {
        credentials.save(result.credential, record.id)
        serverStore.add(result.server)
        serverStore.setActive(record.id)
    }

    suspend fun removeServer(server: ServerRecord) {
        onServerRemoved?.invoke(server.id)
        runCatching { providerFor(server).logout() }
        runCatching { credentials.remove(server.id) }
        progressQueue.clear(server.id)
        serverStore.remove(server.id)
        cachedProvider = null
    }

    fun updateServer(server: ServerRecord) {
        serverStore.update(server)
        cachedProvider = null
    }

    // progress

    suspend fun enqueueAndFlush(update: ProgressUpdate, session: PlaybackSession?) {
        progressQueue.enqueue(update)
        flushProgress(session)
    }

    suspend fun flushProgress(session: PlaybackSession?): ProgressQueue.FlushResult? {
        val p = provider ?: return null
        val server = serverStore.active ?: return null
        val result = progressQueue.flush(server.id, p, session)
        for ((update, pos) in result.conflicts) onConflict?.invoke(update, pos)
        return result
    }

    fun report(error: Throwable) {
        lastError.value = (error as? ApiError)?.message ?: error.message ?: error.javaClass.simpleName
    }
}
