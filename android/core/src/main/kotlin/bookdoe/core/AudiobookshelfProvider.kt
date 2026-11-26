package bookdoe.core

import bookdoe.core.JsonHelpers as J
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject

/**
 * Audiobookshelf 2.26+ (routes read from the server source and the official app in
 * docs/COMPAT_PLAYERS.md section 2.2). Login is `POST /login` with
 * `x-return-tokens: true`; the access token goes in `Authorization: Bearer`, the
 * refresh token in `x-refresh-token` on `POST /auth/refresh`. Playback opens a
 * session (`POST /api/items/:id/play`) whose track URLs are `/public/session/...`
 * (no auth needed), progress is `POST /api/session/:id/sync`.
 */
class AudiobookshelfProvider(
    server: ServerRecord,
    client: HttpClient,
    credentials: CredentialStore,
    appVersion: String,
) : BaseProvider(server, client, credentials, appVersion) {
    override val kind: ServerKind get() = ServerKind.audiobookshelf

    private fun authed(req: HttpRequest): HttpRequest = req.withHeader("Authorization", "Bearer ${credential().accessToken}")

    /** Send with one transparent token refresh on 401. */
    private suspend fun sendAuthed(req: HttpRequest, allow: Set<Int> = emptySet()): HttpResponse {
        val first = client.send(authed(req))
        if (first.status != 401) {
            if (first.isSuccess || first.status in allow) return first
            throw ApiError.from(first)
        }
        refresh()
        return send(authed(req), allow)
    }

    private suspend fun get(path: String, query: Map<String, String?> = emptyMap()): JsonObject =
        sendAuthed(HttpRequest(url = url(path, query))).jsonObject()

    // probe and auth

    override suspend fun probe(): ServerInfo {
        val obj = send(HttpRequest(url = url("/status"))).jsonObject()
        val methods = J.stringArray(obj, "authMethods") ?: listOf("local")
        return ServerInfo(
            name = "Audiobookshelf", version = J.string(obj, "serverVersion"),
            supportsPasswordLogin = methods.contains("local") || methods.isEmpty(), setupRequired = J.bool(obj, "isInit") == false,
        )
    }

    private fun apply(user: JsonObject, username: String?): AuthResult {
        val token = J.string(user, "accessToken") ?: J.string(user, "token") ?: throw ApiError.Decoding("no accessToken")
        val cred = Credential(accessToken = token, refreshToken = J.string(user, "refreshToken"))
        updateServer { s -> s.copy(userId = J.string(user, "id"), username = J.string(user, "username") ?: username ?: s.username) }
        credentials.save(cred, server.id)
        return AuthResult(cred, server, J.string(user, "username"))
    }

    override suspend fun login(username: String, password: String, deviceName: String): AuthResult {
        val body = buildJsonObject { put("username", username); put("password", password) }
        val req = HttpRequest.json(HttpMethod.POST, url("/login"), body).withHeader("x-return-tokens", "true")
        val obj = send(req).jsonObject()
        val user = J.obj(obj, "user") ?: throw ApiError.Decoding("login answer")
        val result = apply(user, username)
        if (server.libraryId == null) {
            var libId = J.string(obj, "userDefaultLibraryId")
            if (libId == null) libId = runCatching { libraries().firstOrNull()?.id }.getOrNull()
            updateServer { it.copy(libraryId = libId) }
        }
        return AuthResult(result.credential, server, result.displayName)
    }

    suspend fun refresh() {
        val rt = credential().refreshToken ?: throw ApiError.Unauthorized
        val req = HttpRequest(HttpMethod.POST, url("/auth/refresh")).withHeader("x-refresh-token", rt)
        val resp = client.send(req)
        val user = if (resp.isSuccess) runCatching { resp.jsonObject() }.getOrNull()?.let { J.obj(it, "user") } else null
        if (user == null) throw ApiError.Unauthorized
        apply(user, null)
    }

    override suspend fun logout() {
        runCatching { credential() }.getOrNull()?.let { c ->
            var req = HttpRequest(HttpMethod.POST, url("/logout")).withHeader("Authorization", "Bearer ${c.accessToken}")
            c.refreshToken?.let { req = req.withHeader("x-refresh-token", it) }
            runCatching { client.send(req) }
        }
        credentials.remove(server.id)
    }

    override fun mediaHeaders(): Map<String, String> = mapOf("Authorization" to "Bearer ${credential().accessToken}")

    // library

    override suspend fun libraries(): List<LibraryRef> {
        val obj = get("/api/libraries")
        return J.array(obj, "libraries").mapNotNull { l ->
            val id = J.string(l, "id") ?: return@mapNotNull null
            LibraryRef(id, J.string(l, "name") ?: id, if (J.string(l, "mediaType") == "book") MediaKind.audiobook else null)
        }
    }

    private fun libraryId(): String = server.libraryId ?: throw ApiError.NotSupported("Choose an Audiobookshelf library first.")

    private fun coverUrl(id: String): String = url("/api/items/$id/cover", mapOf("width" to "384"))

    private fun item(o: JsonObject): MediaItem {
        val media = J.obj(o, "media") ?: JsonObject(emptyMap())
        val meta = J.obj(media, "metadata") ?: JsonObject(emptyMap())
        // minified listings carry authorName/narratorName strings; expanded ones arrays.
        val authors = J.string(meta, "authorName")?.let { listOf(it) } ?: J.array(meta, "authors").mapNotNull { J.string(it, "name") }
        val narrators = J.string(meta, "narratorName")?.let { listOf(it) } ?: (J.stringArray(meta, "narrators") ?: emptyList())
        val series = J.string(meta, "seriesName") ?: J.array(meta, "series").firstOrNull()?.let { J.string(it, "name") }
        val seq = J.array(meta, "series").firstOrNull()?.let { J.string(it, "sequence") }
        val hasAudio = (J.int(media, "numAudioFiles") ?: 0) > 0 || J.array(media, "audioFiles").isNotEmpty() ||
            J.array(media, "tracks").isNotEmpty() || J.double(media, "duration") != null
        val id = J.string(o, "id") ?: ""
        val progress = J.obj(o, "userMediaProgress")?.let { J.double(it, "progress") }
        val updated = J.double(o, "updatedAt")?.toLong()
        return MediaItem(
            id = id, title = J.string(meta, "title") ?: "", subtitle = J.string(meta, "subtitle"), authors = authors, narrators = narrators,
            series = series, seriesSequence = seq, kind = if (hasAudio || J.string(o, "mediaType") == "book") MediaKind.audiobook else MediaKind.ebook,
            durationSeconds = J.double(media, "duration"), coverUrl = if (J.string(media, "coverPath") != null) coverUrl(id) else null,
            progress = progress, updatedAt = updated,
        )
    }

    override suspend fun browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int): LibraryPage {
        val lib = libraryId()
        if (!query.isNullOrEmpty()) {
            val obj = get("/api/libraries/$lib/search", mapOf("q" to query, "limit" to limit.toString()))
            val rows = J.array(obj, "book").mapNotNull { J.obj(it, "libraryItem") }
            return LibraryPage(rows.map(::item), rows.size, 0, limit)
        }
        val sortKey = when (sort) {
            LibrarySort.title -> "media.metadata.title"
            LibrarySort.added -> "addedAt"
            LibrarySort.updated -> "updatedAt"
        }
        val page = if (limit > 0) offset / limit else 0
        val obj = get(
            "/api/libraries/$lib/items",
            mapOf(
                "limit" to limit.toString(), "page" to page.toString(), "sort" to sortKey, "desc" to (if (sort == LibrarySort.title) "0" else "1"),
                "minified" to "1", "include" to "progress",
            ),
        )
        return LibraryPage(J.array(obj, "results").map(::item), J.int(obj, "total") ?: 0, offset, limit)
    }

    override suspend fun continueListening(): List<MediaItem> {
        val arr = sendAuthed(HttpRequest(url = url("/api/libraries/${libraryId()}/personalized", mapOf("include" to "progress")))).jsonArray()
        for (shelf in J.objects(arr)) {
            if (J.string(shelf, "id") == "continue-listening") return J.array(shelf, "entities").map(::item)
        }
        return emptyList()
    }

    override suspend fun detail(id: String): MediaDetail {
        val o = get("/api/items/$id", mapOf("expanded" to "1", "include" to "progress"))
        var m = item(o)
        val media = J.obj(o, "media") ?: JsonObject(emptyMap())
        val tracks = ArrayList<Track>()
        val trackRows = J.array(media, "tracks")
        val rows = trackRows.ifEmpty { J.array(media, "audioFiles") }
        var offset = 0.0
        for ((i, f) in rows.withIndex()) {
            val d = J.double(f, "duration")
            val ino = J.string(f, "ino")
            // Direct file route (Range + Bearer), which Plappa also uses; the play
            // session below replaces these URLs with /public/session ones.
            val u = if (ino != null) url("/api/items/$id/file/$ino") else url("/api/items/$id/file/$i")
            val title = J.string(f, "title") ?: J.obj(f, "metadata")?.let { J.string(it, "filename") } ?: "Part ${i + 1}"
            tracks.add(Track(id = ino ?: i.toString(), index = i, title = title, url = u, durationSeconds = d, startOffset = J.double(f, "startOffset") ?: offset, mimeType = J.string(f, "mimeType")))
            offset += d ?: 0.0
        }
        val chapters = J.array(media, "chapters").map { Chapter(J.string(it, "title") ?: "", J.double(it, "start") ?: 0.0, J.double(it, "end")) }
        m = m.copy(durationSeconds = J.double(media, "duration") ?: (if (offset > 0) offset else null))
        var detail = MediaDetail(item = m, description = J.obj(media, "metadata")?.let { J.string(it, "description") }, tracks = tracks, chapters = chapters, assetId = id)
        val p = J.obj(o, "userMediaProgress")
        val cur = p?.let { J.double(it, "currentTime") }
        if (p != null && cur != null) {
            val loc = detail.locate(cur)
            detail = detail.copy(
                position = PlaybackPosition(
                    trackId = detail.track(loc.index)?.id, trackIndex = loc.index, positionInTrack = loc.offset,
                    absolutePosition = cur, percent = J.double(p, "progress"), updatedAt = J.double(p, "lastUpdate")?.toLong(),
                ),
            )
        }
        return detail
    }

    // playback sessions

    override suspend fun startPlayback(detail: MediaDetail): PlaybackSession {
        val body = buildJsonObject {
            putJsonObject("deviceInfo") {
                put("deviceId", server.deviceId); put("clientName", "bookdoe"); put("clientVersion", appVersion)
                put("manufacturer", "Android"); put("model", "Android")
            }
            put("mediaPlayer", "ExoPlayer"); put("forceDirectPlay", "1"); put("forceTranscode", "")
        }
        val obj = sendAuthed(HttpRequest.json(HttpMethod.POST, url("/api/items/${detail.assetId}/play"), body)).jsonObject()
        return PlaybackSession(J.string(obj, "id"))
    }

    /**
     * Tracks as the play session serves them (`/public/session/:id/track/:index`);
     * call after [startPlayback] to replace `detail.tracks` when the server returned them.
     */
    fun sessionTracks(sessionAnswer: JsonObject): List<Track> =
        J.array(sessionAnswer, "audioTracks").withIndex().mapNotNull { (i, t) ->
            val rel = J.string(t, "contentUrl") ?: return@mapNotNull null
            val u = Urls.resolving(server.baseUrl, rel) ?: return@mapNotNull null
            val idx = J.int(t, "index") ?: i
            Track(id = idx.toString(), index = idx, title = J.string(t, "title") ?: "Part ${i + 1}", url = u, durationSeconds = J.double(t, "duration"), startOffset = J.double(t, "startOffset") ?: 0.0, mimeType = J.string(t, "mimeType"))
        }

    override suspend fun reportProgress(update: ProgressUpdate, session: PlaybackSession?): ProgressReport {
        val duration = update.duration ?: 0.0
        val sid = session?.sessionId ?: update.sessionId
        if (sid != null) {
            val body = buildJsonObject { put("currentTime", update.absolutePosition); put("duration", duration); put("timeListened", update.listened) }
            sendAuthed(HttpRequest.json(HttpMethod.POST, url("/api/session/$sid/sync"), body))
        } else {
            val body = buildJsonObject {
                put("currentTime", update.absolutePosition); put("isFinished", update.isFinished)
                if (duration > 0) { put("duration", duration); put("progress", update.absolutePosition / duration) }
            }
            sendAuthed(HttpRequest.json(HttpMethod.PATCH, url("/api/me/progress/${update.assetId}"), body))
        }
        return ProgressReport(applied = true)
    }

    override suspend fun stopPlayback(update: ProgressUpdate, session: PlaybackSession?) {
        reportProgress(update, session)
        val sid = session?.sessionId ?: update.sessionId
        if (sid != null) {
            val body = buildJsonObject { put("currentTime", update.absolutePosition); put("duration", update.duration ?: 0.0); put("timeListened", 0.0) }
            runCatching { sendAuthed(HttpRequest.json(HttpMethod.POST, url("/api/session/$sid/close"), body)) }
        }
    }
}
