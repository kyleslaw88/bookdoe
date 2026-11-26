package bookdoe.core

import bookdoe.core.JsonHelpers as J
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * bookdoe Player API v1 (docs/PLAYER_API.md). Device tokens (`alxd_...`) are
 * minted by password login (`POST /auth/login`, servers 0.9.1+) or by device-code
 * pairing; both reach the Player API and the media byte routes and nothing else.
 */
class BookdoeProvider(
    server: ServerRecord,
    client: HttpClient,
    credentials: CredentialStore,
    appVersion: String,
) : BaseProvider(server, client, credentials, appVersion) {
    override val kind: ServerKind get() = ServerKind.bookdoe
    private val api = "/api/player/v1"

    private fun authed(req: HttpRequest): HttpRequest = req.withHeader("Authorization", "Bearer ${credential().accessToken}")

    private suspend fun get(path: String, query: Map<String, String?> = emptyMap()): JsonObject =
        send(authed(HttpRequest(url = url(api + path, query)))).jsonObject()

    // probe and auth

    override suspend fun probe(): ServerInfo {
        val caps = send(HttpRequest(url = url("$api/capabilities"))).jsonObject()
        val auth = J.obj(caps, "auth") ?: JsonObject(emptyMap())
        return ServerInfo(
            name = J.string(caps, "server") ?: "bookdoe",
            version = J.string(caps, "version"),
            supportsPasswordLogin = J.bool(auth, "password_login") ?: false,
            supportsDevicePairing = J.bool(auth, "device_pairing") ?: true,
            setupRequired = J.bool(caps, "setup_required") ?: false,
        )
    }

    override suspend fun login(username: String, password: String, deviceName: String): AuthResult {
        val info = probe()
        if (info.setupRequired) throw ApiError.SetupRequired
        if (!info.supportsPasswordLogin) {
            throw ApiError.NotSupported("This server signs devices in by pairing code (its auth mode has no password login).")
        }
        val body = buildJsonObject {
            put("username", username); put("password", password); put("device_name", deviceName)
            put("platform", "android"); put("client_version", appVersion)
        }
        val resp = send(HttpRequest.json(HttpMethod.POST, url("$api/auth/login"), body))
        return authResult(resp.jsonObject(), username)
    }

    private fun authResult(obj: JsonObject, username: String?): AuthResult {
        val token = J.string(obj, "token") ?: throw ApiError.Decoding("no token in login answer")
        val user = J.obj(obj, "user") ?: JsonObject(emptyMap())
        updateServer { s ->
            s.copy(userId = J.string(user, "id"), username = J.string(user, "username") ?: username ?: s.username)
        }
        return AuthResult(Credential(accessToken = token), server, J.string(user, "display_name") ?: J.string(user, "username"))
    }

    /** Pairing step 1: ask for a code to show the person (RFC 8628 shaped). */
    suspend fun startPairing(deviceName: String): DevicePairing {
        val body = buildJsonObject { put("name", deviceName); put("platform", "android"); put("client_version", appVersion) }
        val obj = send(HttpRequest.json(HttpMethod.POST, url("$api/device/code"), body)).jsonObject()
        val dc = J.string(obj, "device_code")
        val uc = J.string(obj, "user_code")
        if (dc == null || uc == null) throw ApiError.Decoding("pairing answer")
        return DevicePairing(dc, uc, J.string(obj, "verification_uri_complete"), J.int(obj, "interval") ?: 5, J.int(obj, "expires_in") ?: 900)
    }

    /** Pairing step 3: poll until approved. */
    suspend fun pollPairing(pairing: DevicePairing): PairingPoll {
        val body = buildJsonObject { put("device_code", pairing.deviceCode) }
        val resp = send(HttpRequest.json(HttpMethod.POST, url("$api/device/token"), body), allow = setOf(400, 403))
        val obj = resp.jsonObject()
        if (resp.isSuccess) return PairingPoll.Approved(authResult(obj, null))
        return when (J.string(obj, "error")) {
            "authorization_pending" -> PairingPoll.Pending
            "slow_down" -> PairingPoll.SlowDown
            "access_denied" -> PairingPoll.Denied
            else -> PairingPoll.Expired
        }
    }

    override suspend fun logout() {
        val c = runCatching { credential() }.getOrNull()
        if (c != null) {
            val me = runCatching { get("/me") }.getOrNull()
            val devId = me?.let { J.obj(it, "device") }?.let { J.string(it, "id") }
            if (devId != null) {
                val req = HttpRequest(HttpMethod.DELETE, url("$api/devices/$devId")).withHeader("Authorization", "Bearer ${c.accessToken}")
                runCatching { client.send(req) }
            }
        }
        credentials.remove(server.id)
    }

    override fun mediaHeaders(): Map<String, String> = mapOf("Authorization" to "Bearer ${credential().accessToken}")

    // library

    override suspend fun libraries(): List<LibraryRef> = listOf(LibraryRef("library", "Library"))

    private fun item(w: JsonObject): MediaItem {
        val authors = J.array(w, "authors").mapNotNull { J.string(it, "name") }
        val narrators = J.array(w, "narrators").mapNotNull { J.string(it, "name") }
        val series = J.array(w, "series").firstOrNull()
        val assets = J.array(w, "assets")
        val audio = assets.firstOrNull { J.string(it, "kind") == "audiobook" }
        val kind = if (audio != null) MediaKind.audiobook else MediaKind.ebook
        val duration = audio?.let { J.double(it, "duration_seconds") }
        val cover = J.string(w, "cover_url")?.let { Urls.appending(server.baseUrl, it, mapOf("size" to "384")) }
        return MediaItem(
            id = J.string(w, "id") ?: "", title = J.string(w, "title") ?: "", subtitle = J.string(w, "subtitle"),
            authors = authors, narrators = narrators,
            series = series?.let { J.string(it, "name") }, seriesSequence = series?.let { J.string(it, "position") },
            kind = kind, durationSeconds = duration, coverUrl = cover, progress = null,
            updatedAt = J.millisFromSeconds(J.double(w, "updated_at")),
        )
    }

    override suspend fun browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int): LibraryPage {
        val obj = get(
            "/library",
            mapOf(
                "q" to query?.takeIf { it.isNotEmpty() }, "kind" to kind?.name, "sort" to sort.name,
                "limit" to limit.toString(), "offset" to offset.toString(),
            ),
        )
        return LibraryPage(J.array(obj, "items").map(::item), J.int(obj, "total") ?: 0, offset, limit)
    }

    override suspend fun continueListening(): List<MediaItem> {
        // /continue returns one flat row per in-progress asset (work_id, title, authors
        // as a comma string, percent, has_cover), not the compact work shape.
        val obj = get("/continue")
        return J.array(obj, "items").mapNotNull { row ->
            if (J.string(row, "kind") == "podcast") {
                val episodeId = J.string(row, "episode_id") ?: return@mapNotNull null
                val duration = J.double(row, "duration_seconds")
                val position = J.double(row, "position_seconds")
                return@mapNotNull MediaItem(
                    id = episodeId, title = J.string(row, "title") ?: "", kind = MediaKind.audiobook,
                    isEpisode = true, showTitle = J.string(row, "show_title"), durationSeconds = duration,
                    progress = if (duration != null && duration > 0 && position != null) (position / duration).coerceIn(0.0, 1.0) else null,
                    updatedAt = J.millisFromSeconds(J.double(row, "updated_at")),
                )
            }
            val workId = J.string(row, "work_id") ?: return@mapNotNull null
            val authors = (J.string(row, "authors") ?: "").split(',').map { it.trim() }.filter { it.isNotEmpty() }
            val kind = if (J.string(row, "kind") == "ebook") MediaKind.ebook else MediaKind.audiobook
            val cover = if (J.bool(row, "has_cover") == true) Urls.appending(server.baseUrl, "/api/player/v1/library/$workId/cover", mapOf("size" to "384")) else null
            MediaItem(
                id = workId, title = J.string(row, "title") ?: "", authors = authors, kind = kind, coverUrl = cover,
                progress = J.double(row, "percent"), updatedAt = J.millisFromSeconds(J.double(row, "updated_at")),
            )
        }
    }

    override suspend fun detail(id: String): MediaDetail {
        val w = get("/library/$id")
        var m = item(w)
        val assets = J.array(w, "assets")
        val progressRows = J.array(w, "progress")
        val audio = assets.firstOrNull { J.string(it, "kind") == "audiobook" }
        val audioAssetId = audio?.let { J.string(it, "id") }
        if (audio != null && audioAssetId != null) {
            val man = get("/audio/$id/$audioAssetId")
            val tracks = ArrayList<Track>()
            var offset = 0.0
            for ((i, f) in J.array(man, "files").withIndex()) {
                val d = J.double(f, "duration_seconds")
                val rel = J.string(f, "url") ?: continue
                val u = Urls.resolving(server.baseUrl, rel) ?: continue
                tracks.add(
                    Track(
                        id = J.string(f, "id") ?: i.toString(), index = i, title = J.string(f, "title") ?: "Part ${i + 1}", url = u,
                        durationSeconds = d, startOffset = offset, sizeBytes = J.long(f, "size_bytes"),
                    ),
                )
                offset += d ?: 0.0
            }
            val chapters = J.array(man, "chapters").map { Chapter(J.string(it, "title") ?: "", J.double(it, "start") ?: 0.0, J.double(it, "end")) }
            m = m.copy(durationSeconds = J.double(man, "duration_seconds") ?: (if (offset > 0) offset else null))
            var detail = MediaDetail(item = m, description = J.string(w, "description"), tracks = tracks, chapters = chapters, assetId = audioAssetId)
            val p = J.obj(man, "progress") ?: progressRows.firstOrNull { J.string(it, "asset_id") == audioAssetId }
            if (p != null) detail = detail.copy(position = position(p, detail))
            return detail
        }
        val ebook = assets.firstOrNull { J.string(it, "kind") == "ebook" }
        val ebookAssetId = ebook?.let { J.string(it, "id") }
        if (ebook != null && ebookAssetId != null) {
            val man = get("/ebook/$id/$ebookAssetId")
            var detail = MediaDetail(
                item = m, description = J.string(w, "description"), assetId = ebookAssetId,
                ebookDownloadUrl = J.string(man, "download_url")?.let { Urls.resolving(server.baseUrl, it) },
                ebookFormat = J.string(man, "format"),
            )
            J.obj(man, "progress")?.let { p ->
                detail = detail.copy(position = PlaybackPosition(percent = J.double(p, "percent"), updatedAt = J.millisFromSeconds(J.double(p, "updated_at")), location = J.string(p, "location")))
            }
            return detail
        }
        return MediaDetail(item = m, description = J.string(w, "description"), assetId = id)
    }

    /** A podcast episode as a one-track [MediaDetail] the player can load; progress goes to the podcast route (see [reportProgress]). */
    suspend fun episodeDetail(episodeId: String): MediaDetail {
        val m = get("/podcasts/episodes/$episodeId")
        val url = Urls.resolving(server.baseUrl, J.string(m, "url") ?: throw ApiError.Decoding("episode url")) ?: throw ApiError.Decoding("episode url")
        val duration = J.double(m, "duration_seconds")
        val start = J.double(m, "position_seconds") ?: 0.0
        val item = MediaItem(
            id = episodeId, title = J.string(m, "title") ?: "", kind = MediaKind.audiobook, durationSeconds = duration,
            isEpisode = true, showTitle = J.string(m, "show_title"), authors = listOfNotNull(J.string(m, "show_title")),
        )
        val track = Track(id = episodeId, index = 0, title = item.title, url = url, durationSeconds = duration)
        return MediaDetail(
            item = item, tracks = listOf(track), assetId = EPISODE_PREFIX + episodeId,
            position = PlaybackPosition(trackId = episodeId, positionInTrack = start, absolutePosition = start),
        )
    }

    private fun position(p: JsonObject, detail: MediaDetail): PlaybackPosition {
        val fileId = J.string(p, "file_id")
        val inTrack = J.double(p, "position") ?: 0.0
        val track = detail.tracks.firstOrNull { it.id == fileId } ?: detail.tracks.firstOrNull()
        return PlaybackPosition(
            trackId = fileId, trackIndex = track?.index ?: 0, positionInTrack = inTrack,
            absolutePosition = (track?.startOffset ?: 0.0) + inTrack, percent = J.double(p, "percent"),
            updatedAt = J.millisFromSeconds(J.double(p, "updated_at")), location = J.string(p, "location"),
        )
    }

    // playback and progress

    override suspend fun startPlayback(detail: MediaDetail): PlaybackSession = PlaybackSession(sessionId = null)

    override suspend fun reportProgress(update: ProgressUpdate, session: PlaybackSession?): ProgressReport {
        podcastEpisodeId(update.assetId)?.let { return reportPodcastProgress(update, it) }
        if (update.assetId.startsWith(EPISODE_PREFIX)) {
            val id = update.assetId.removePrefix(EPISODE_PREFIX)
            val body = buildJsonObject { put("position_seconds", update.absolutePosition.coerceAtLeast(0.0)) }
            send(authed(HttpRequest.json(HttpMethod.POST, url("$api/podcasts/episodes/$id/progress"), body)))
            return ProgressReport(true, null)
        }
        val item = buildJsonObject {
            put("asset_id", update.assetId); put("device_id", server.deviceId)
            put("client_updated_at", update.clientUpdatedAt / 1000.0)
            update.trackId?.let { put("file_id", it); put("position", update.positionInTrack) }
            update.duration?.let { put("duration", it) }
            update.percent?.let { put("percent", it.coerceIn(0.0, 1.0)) }
            update.location?.let { put("location", it) }
        }
        val body = buildJsonObject { put("items", buildJsonArray { add(item) }) }
        val obj = send(authed(HttpRequest.json(HttpMethod.PUT, url("$api/progress"), body))).jsonObject()
        val first = J.array(obj, "items").firstOrNull() ?: throw ApiError.Decoding("progress answer")
        J.string(first, "error")?.let { err ->
            if (err == "not_found") throw ApiError.NotFound
            throw ApiError.Server(400, err)
        }
        val applied = J.bool(first, "applied") ?: true
        var serverPos: PlaybackPosition? = null
        if (!applied) {
            val stored = J.obj(first, "stored") ?: first
            serverPos = PlaybackPosition(
                trackId = J.string(stored, "file_id"), positionInTrack = J.double(stored, "position") ?: 0.0,
                absolutePosition = J.double(stored, "position") ?: 0.0, percent = J.double(stored, "percent"),
                updatedAt = J.millisFromSeconds(J.double(stored, "updated_at")), location = J.string(stored, "location"),
            )
        }
        return ProgressReport(applied, serverPos)
    }

    override suspend fun stopPlayback(update: ProgressUpdate, session: PlaybackSession?) {
        reportProgress(update, session)
    }

    // podcasts (issues #514, #516)

    suspend fun podcastShows(): List<PodcastShowRef> =
        J.array(get("/podcasts"), "shows").mapNotNull { s -> J.string(s, "id")?.let { PodcastShowRef(it, J.string(s, "title") ?: "") } }

    suspend fun podcastShow(id: String): PodcastShow =
        parsePodcastShow(get("/podcasts/shows/$id"), server.baseUrl) ?: throw ApiError.Decoding("podcast show answer")

    /**
     * A one-track detail for the player: the episode title, the show as "author" (lock screen artist) and the position
     * this account last saved for the episode.
     */
    suspend fun podcastEpisodeDetail(episode: PodcastEpisode, show: PodcastShow): MediaDetail {
        val saved = get("/podcasts/episodes/${episode.id}/progress")
        val seconds = J.obj(saved, "progress")?.let { J.double(it, "position_seconds") } ?: 0.0
        val audio = Urls.appending(server.baseUrl, "$api/podcasts/episodes/${episode.id}/audio")
        val item = MediaItem(
            id = episode.id, title = episode.title, authors = listOf(show.title), kind = MediaKind.audiobook,
            durationSeconds = episode.durationSeconds, coverUrl = show.coverUrl,
        )
        val track = Track(id = episode.id, index = 0, title = episode.title, url = audio, durationSeconds = episode.durationSeconds)
        return MediaDetail(
            item = item, description = episode.description, tracks = listOf(track),
            position = PlaybackPosition(trackId = episode.id, trackIndex = 0, positionInTrack = seconds, absolutePosition = seconds),
            assetId = PODCAST_ASSET_PREFIX + episode.id,
        )
    }

    private suspend fun reportPodcastProgress(update: ProgressUpdate, episodeId: String): ProgressReport {
        val body = buildJsonObject {
            put("position_seconds", maxOf(0.0, update.absolutePosition)); put("client_updated_at", update.clientUpdatedAt / 1000.0)
        }
        val obj = send(authed(HttpRequest.json(HttpMethod.PUT, url("$api/podcasts/episodes/$episodeId/progress"), body))).jsonObject()
        val applied = J.bool(obj, "applied") ?: true
        val stored = J.obj(obj, "progress")
        val serverPos = if (!applied && stored != null) {
            val p = J.double(stored, "position_seconds") ?: 0.0
            PlaybackPosition(trackId = episodeId, positionInTrack = p, absolutePosition = p, updatedAt = J.millisFromSeconds(J.double(stored, "client_updated_at")))
        } else null
        return ProgressReport(applied, serverPos)
    }

    companion object {
        /** Podcast episodes ride the audiobook player under an asset id of this form, so the progress queue can route them. */
        const val PODCAST_ASSET_PREFIX = "podcast:"

        fun podcastEpisodeId(assetId: String): String? = if (assetId.startsWith(PODCAST_ASSET_PREFIX)) assetId.removePrefix(PODCAST_ASSET_PREFIX) else null

        /** Marks a [ProgressUpdate.assetId] as a podcast episode id (Continue Listening, #522) rather than a book asset. */
        const val EPISODE_PREFIX = "episode:"
    }
}
