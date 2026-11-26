package bookdoe.core

import bookdoe.core.JsonHelpers as J
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.UUID

/**
 * Jellyfin 10.9+ (routes from docs/COMPAT_PLAYERS.md section 2.4; the ones marked
 * "capture" there were verified against a 12.x demo server, the audiobook item
 * shapes are assumptions, see docs/IOS_APP.md and docs/ANDROID_APP.md).
 *
 * Positions are 100 ns ticks. Audiobook libraries (`CollectionType == "books"`)
 * hold `AudioBook` items, one per file; a multi-file book appears as a folder
 * (`Folder`/`MusicAlbum`) whose children carry `IndexNumber`. Both shapes map onto
 * one [MediaDetail] with ordered tracks.
 */
class JellyfinProvider(
    server: ServerRecord,
    client: HttpClient,
    credentials: CredentialStore,
    appVersion: String,
) : BaseProvider(server, client, credentials, appVersion) {
    override val kind: ServerKind get() = ServerKind.jellyfin

    companion object {
        const val TICKS_PER_SECOND = 10_000_000.0
        private const val ITEM_TYPES = "AudioBook,Audio,MusicAlbum,Folder,Book"
        private const val FIELDS = "MediaSources,Overview,ChildCount,DateCreated,SeriesName,ParentId"
        private val FOLDER_TYPES = setOf("Folder", "MusicAlbum")
    }

    private val clientName = "bookdoe"

    private fun authHeader(token: String?): String {
        val parts = mutableListOf("Client=\"$clientName\"", "Device=\"Android\"", "DeviceId=\"${server.deviceId}\"", "Version=\"$appVersion\"")
        if (token != null) parts.add("Token=\"$token\"")
        return "MediaBrowser " + parts.joinToString(", ")
    }

    private fun authed(req: HttpRequest): HttpRequest = req.withHeader("Authorization", authHeader(credential().accessToken))

    private suspend fun get(path: String, query: Map<String, String?> = emptyMap()): JsonObject =
        send(authed(HttpRequest(url = url(path, query)))).jsonObject()

    private fun userId(): String = server.userId ?: throw ApiError.Unauthorized

    // probe and auth

    override suspend fun probe(): ServerInfo {
        val obj = send(HttpRequest(url = url("/System/Info/Public"))).jsonObject()
        return ServerInfo(J.string(obj, "ServerName") ?: "Jellyfin", J.string(obj, "Version"), supportsPasswordLogin = true)
    }

    override suspend fun login(username: String, password: String, deviceName: String): AuthResult {
        val body = buildJsonObject { put("Username", username); put("Pw", password) }
        val req = HttpRequest.json(HttpMethod.POST, url("/Users/AuthenticateByName"), body).withHeader("Authorization", authHeader(null))
        val obj = send(req).jsonObject()
        val token = J.string(obj, "AccessToken")
        val user = J.obj(obj, "User")
        val uid = user?.let { J.string(it, "Id") }
        if (token == null || user == null || uid == null) throw ApiError.Decoding("AuthenticateByName answer")
        updateServer { s -> s.copy(userId = uid, username = J.string(user, "Name") ?: username) }
        val cred = Credential(accessToken = token)
        credentials.save(cred, server.id)
        // Pick the first books library when none is chosen yet.
        if (server.libraryId == null) {
            runCatching { libraries().firstOrNull() }.getOrNull()?.let { first -> updateServer { it.copy(libraryId = first.id) } }
        }
        return AuthResult(cred, server, J.string(user, "Name"))
    }

    override suspend fun logout() {
        runCatching { credential() }.getOrNull()?.let { c ->
            val req = HttpRequest(HttpMethod.POST, url("/Sessions/Logout")).withHeader("Authorization", authHeader(c.accessToken))
            runCatching { client.send(req) }
        }
        credentials.remove(server.id)
    }

    override fun mediaHeaders(): Map<String, String> = mapOf("Authorization" to authHeader(credential().accessToken))

    // library

    override suspend fun libraries(): List<LibraryRef> {
        val obj = get("/UserViews", mapOf("userId" to userId()))
        val views = J.array(obj, "Items")
        val books = views.filter { J.string(it, "CollectionType") == "books" }
        return books.ifEmpty { views }.mapNotNull { v ->
            val id = J.string(v, "Id") ?: return@mapNotNull null
            LibraryRef(id, J.string(v, "Name") ?: id, if (J.string(v, "CollectionType") == "books") MediaKind.audiobook else null)
        }
    }

    private fun coverUrl(obj: JsonObject): String? {
        val id = J.string(obj, "Id") ?: return null
        val tags = J.obj(obj, "ImageTags") ?: JsonObject(emptyMap())
        if (J.element(tags, "Primary") == null && J.string(obj, "AlbumPrimaryImageTag") == null) return null
        return url("/Items/$id/Images/Primary", mapOf("maxWidth" to "384", "tag" to J.string(tags, "Primary")))
    }

    private fun item(o: JsonObject): MediaItem {
        val type = J.string(o, "Type") ?: ""
        val kind = if (type == "Book") MediaKind.ebook else MediaKind.audiobook
        val artists = J.stringArray(o, "Artists") ?: J.array(o, "AlbumArtists").mapNotNull { J.string(it, "Name") }
        val ticks = J.double(o, "RunTimeTicks")
        val userData = J.obj(o, "UserData") ?: JsonObject(emptyMap())
        var progress: Double? = null
        val pos = J.double(userData, "PlaybackPositionTicks")
        if (ticks != null && ticks > 0 && pos != null && pos > 0) progress = pos / ticks
        if (J.bool(userData, "Played") == true) progress = 1.0
        return MediaItem(
            id = J.string(o, "Id") ?: "", title = J.string(o, "Name") ?: "", subtitle = J.string(o, "Album"),
            authors = artists, narrators = emptyList(), series = J.string(o, "SeriesName"), seriesSequence = J.int(o, "IndexNumber")?.toString(),
            kind = kind, durationSeconds = ticks?.let { it / TICKS_PER_SECOND }, coverUrl = coverUrl(o), progress = progress,
            updatedAt = J.millisFromIso(J.string(o, "DateLastSaved") ?: J.string(o, "DateCreated")),
        )
    }

    override suspend fun browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int): LibraryPage {
        val sortBy = when (sort) {
            LibrarySort.title -> "SortName"
            LibrarySort.added -> "DateCreated"
            LibrarySort.updated -> "DateLastSaved"
        }
        val q = HashMap<String, String?>(
            mapOf(
                "userId" to userId(), "parentId" to server.libraryId, "recursive" to "true",
                "includeItemTypes" to (if (kind == MediaKind.ebook) "Book" else ITEM_TYPES), "fields" to FIELDS,
                "sortBy" to sortBy, "sortOrder" to (if (sort == LibrarySort.title) "Ascending" else "Descending"),
                "limit" to limit.toString(), "startIndex" to offset.toString(),
            ),
        )
        if (!query.isNullOrEmpty()) q["searchTerm"] = query
        val obj = get("/Items", q)
        // A multi-file book is listed by its folder; hide the per-file children whose
        // parent is also in the page (they come back from `detail`).
        val rows = J.array(obj, "Items")
        val folderIds = rows.filter { (J.string(it, "Type") ?: "") in FOLDER_TYPES }.mapNotNull { J.string(it, "Id") }.toSet()
        val visible = rows.filter { J.string(it, "ParentId")?.let { p -> p in folderIds } != true }
        return LibraryPage(visible.map(::item), J.int(obj, "TotalRecordCount") ?: visible.size, offset, limit)
    }

    override suspend fun continueListening(): List<MediaItem> {
        val obj = get("/UserItems/Resume", mapOf("userId" to userId(), "parentId" to server.libraryId, "mediaTypes" to "Audio", "fields" to FIELDS, "limit" to "20"))
        return J.array(obj, "Items").map(::item)
    }

    private fun streamUrl(itemId: String): String = url(
        "/Audio/$itemId/universal",
        mapOf(
            "userId" to server.userId, "deviceId" to server.deviceId,
            "container" to "m4b,m4a,mp3,aac,flac,ogg,opus,wav", "audioCodec" to "aac",
            "transcodingContainer" to "ts", "transcodingProtocol" to "hls",
            "api_key" to runCatching { credential().accessToken }.getOrNull(),
        ),
    )

    override suspend fun detail(id: String): MediaDetail {
        val o = get("/Items/$id", mapOf("userId" to userId(), "fields" to FIELDS))
        var m = item(o)
        var fileItems: List<JsonObject> = listOf(o)
        if ((J.string(o, "Type") ?: "") in FOLDER_TYPES) {
            val kids = get(
                "/Items",
                mapOf(
                    "userId" to userId(), "parentId" to id, "includeItemTypes" to "AudioBook,Audio", "fields" to FIELDS,
                    "sortBy" to "ParentIndexNumber,IndexNumber,SortName", "limit" to "500",
                ),
            )
            fileItems = J.array(kids, "Items")
        }
        val tracks = ArrayList<Track>()
        var offset = 0.0
        for ((i, f) in fileItems.withIndex()) {
            val fid = J.string(f, "Id") ?: continue
            val d = J.double(f, "RunTimeTicks")?.let { it / TICKS_PER_SECOND }
            val container = J.array(f, "MediaSources").firstOrNull()?.let { J.string(it, "Container") }
            tracks.add(
                Track(
                    id = fid, index = i, title = J.string(f, "Name") ?: "Part ${i + 1}", url = streamUrl(fid),
                    durationSeconds = d, startOffset = offset, mimeType = container?.let { "audio/$it" },
                ),
            )
            offset += d ?: 0.0
        }
        if (m.durationSeconds == null || fileItems.size > 1) m = m.copy(durationSeconds = if (offset > 0) offset else m.durationSeconds)
        val chapters = J.array(o, "Chapters").map { Chapter(J.string(it, "Name") ?: "", (J.double(it, "StartPositionTicks") ?: 0.0) / TICKS_PER_SECOND) }
        var detail = MediaDetail(item = m, description = J.string(o, "Overview"), tracks = tracks, chapters = chapters, assetId = id)
        // Position: a single item carries UserData; a folder's position is the first
        // child with a non-zero position (Jellyfin stores it per file).
        position(detail, fileItems)?.let { detail = detail.copy(position = it) }
        return detail
    }

    private fun position(detail: MediaDetail, items: List<JsonObject>): PlaybackPosition? {
        for ((i, f) in items.withIndex()) {
            val ud = J.obj(f, "UserData") ?: JsonObject(emptyMap())
            val ticks = J.double(ud, "PlaybackPositionTicks")
            val t = detail.track(i)
            if (ticks != null && ticks > 0 && t != null) {
                val inTrack = ticks / TICKS_PER_SECOND
                val total = detail.totalDuration
                return PlaybackPosition(
                    trackId = t.id, trackIndex = i, positionInTrack = inTrack, absolutePosition = t.startOffset + inTrack,
                    percent = if (total > 0) (t.startOffset + inTrack) / total else null,
                    updatedAt = J.millisFromIso(J.string(ud, "LastPlayedDate")),
                )
            }
        }
        return null
    }

    // playback reporting

    override suspend fun startPlayback(detail: MediaDetail): PlaybackSession {
        val sessionId = UUID.randomUUID().toString().replace("-", "")
        val track = detail.track(detail.position?.trackIndex ?: 0) ?: detail.tracks.firstOrNull()
        val body = buildJsonObject {
            put("ItemId", track?.id ?: detail.assetId); put("PlaySessionId", sessionId); put("CanSeek", true)
            put("PlayMethod", "DirectPlay"); put("IsPaused", false)
            put("PositionTicks", ((detail.position?.positionInTrack ?: 0.0) * TICKS_PER_SECOND).toLong())
        }
        send(authed(HttpRequest.json(HttpMethod.POST, url("/Sessions/Playing"), body)))
        return PlaybackSession(sessionId)
    }

    private fun reportBody(u: ProgressUpdate, session: PlaybackSession?, paused: Boolean): JsonObject = buildJsonObject {
        put("ItemId", u.trackId ?: u.assetId); put("PlaySessionId", session?.sessionId ?: u.sessionId ?: "")
        put("PositionTicks", (u.positionInTrack * TICKS_PER_SECOND).toLong())
        put("IsPaused", paused); put("PlayMethod", "DirectPlay"); put("CanSeek", true)
    }

    override suspend fun reportProgress(update: ProgressUpdate, session: PlaybackSession?): ProgressReport {
        send(authed(HttpRequest.json(HttpMethod.POST, url("/Sessions/Playing/Progress"), reportBody(update, session, false))))
        if (update.isFinished) {
            runCatching { send(authed(HttpRequest(HttpMethod.POST, url("/UserPlayedItems/${update.trackId ?: update.assetId}", mapOf("userId" to server.userId))))) }
        }
        return ProgressReport(applied = true)
    }

    override suspend fun stopPlayback(update: ProgressUpdate, session: PlaybackSession?) {
        send(authed(HttpRequest.json(HttpMethod.POST, url("/Sessions/Playing/Stopped"), reportBody(update, session, true))))
    }
}
