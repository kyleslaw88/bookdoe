package bookdoe.core

/**
 * One abstraction over bookdoe, Jellyfin and Audiobookshelf. Each provider holds
 * a [ServerRecord], an [HttpClient] and a [CredentialStore]; it never caches a
 * token outside the store so a credential change is visible everywhere at once.
 */
interface MediaProvider {
    val server: ServerRecord
    val kind: ServerKind

    /** Unauthenticated facts: name, version, which sign-in methods exist. */
    suspend fun probe(): ServerInfo

    /**
     * Username and password sign-in. Returns the credential and an updated server
     * record (user id, default library). The caller persists both.
     */
    suspend fun login(username: String, password: String, deviceName: String): AuthResult

    /** Libraries to choose from (Jellyfin views, Audiobookshelf libraries). bookdoe returns one entry. */
    suspend fun libraries(): List<LibraryRef>

    suspend fun browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int): LibraryPage

    /** In-progress items, most recent first. */
    suspend fun continueListening(): List<MediaItem>

    suspend fun detail(id: String): MediaDetail

    /** Tell the server playback starts; the returned session is passed to every progress report until [stopPlayback]. */
    suspend fun startPlayback(detail: MediaDetail): PlaybackSession

    suspend fun reportProgress(update: ProgressUpdate, session: PlaybackSession?): ProgressReport

    suspend fun stopPlayback(update: ProgressUpdate, session: PlaybackSession?)

    /** Headers a player or download task must attach to media URLs. */
    fun mediaHeaders(): Map<String, String>

    /** Sign out server-side where supported and forget the credential. */
    suspend fun logout()
}

/** Builds the right provider for a saved server. */
class ProviderFactory(
    val client: HttpClient,
    val credentials: CredentialStore,
    val appVersion: String = "1.0",
) {
    fun make(server: ServerRecord): MediaProvider = when (server.kind) {
        ServerKind.bookdoe -> BookdoeProvider(server, client, credentials, appVersion)
        ServerKind.jellyfin -> JellyfinProvider(server, client, credentials, appVersion)
        ServerKind.audiobookshelf -> AudiobookshelfProvider(server, client, credentials, appVersion)
    }
}

/** Shared plumbing for the three providers. */
abstract class BaseProvider(
    server: ServerRecord,
    internal val client: HttpClient,
    internal val credentials: CredentialStore,
    internal val appVersion: String,
) : MediaProvider {
    @Volatile
    final override var server: ServerRecord = server
        private set

    @Synchronized
    internal fun updateServer(change: (ServerRecord) -> ServerRecord) {
        server = change(server)
    }

    internal fun credential(): Credential = credentials.credential(server.id) ?: throw ApiError.Unauthorized

    internal fun url(path: String, query: Map<String, String?> = emptyMap()): String = Urls.appending(server.baseUrl, path, query)

    /** Send and map non-2xx to [ApiError] (404 included, so callers see NotFound). */
    internal suspend fun send(request: HttpRequest, allow: Set<Int> = emptySet()): HttpResponse {
        val resp = client.send(request)
        if (resp.isSuccess || resp.status in allow) return resp
        throw ApiError.from(resp)
    }
}
