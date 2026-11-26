package bookdoe.core

import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class AudiobookshelfProviderTest {
    private lateinit var client: MockHttpClient
    private lateinit var creds: InMemoryCredentialStore
    private lateinit var provider: AudiobookshelfProvider
    private val server = makeServer(ServerKind.audiobookshelf, base = "https://abs.example.com", libraryId = "lib1", userId = "u1")

    @Before
    fun setUp() {
        client = MockHttpClient()
        creds = InMemoryCredentialStore()
        provider = AudiobookshelfProvider(server, client, creds, "1.0")
    }

    @Test
    fun probeReadsStatus() = runTest {
        client.enqueueJson(200, """{"serverVersion": "2.37.1", "isInit": true, "authMethods": ["local", "openid"]}""")
        val info = provider.probe()
        assertEquals("2.37.1", info.version)
        assertTrue(info.supportsPasswordLogin)
        assertFalse(info.setupRequired)
        assertEquals("/status", client.last?.path)
    }

    @Test
    fun loginRequestsTokensInBodyAndStoresRefreshToken() = runTest {
        val fresh = AudiobookshelfProvider(makeServer(ServerKind.audiobookshelf, base = "https://abs.example.com"), client, creds, "1.0")
        client.enqueueJson(200, """{"user": {"id": "u1", "username": "ada", "accessToken": "acc", "refreshToken": "ref", "mediaProgress": []},
            "userDefaultLibraryId": "lib9", "serverSettings": {}}""")
        val r = fresh.login("ada", "pw", "Pixel")
        assertEquals(Credential("acc", "ref"), r.credential)
        assertEquals("u1", r.server.userId)
        assertEquals("lib9", r.server.libraryId)
        val req = client.last!!
        assertEquals("/login", req.path)
        assertEquals("true", req.headers["x-return-tokens"])
        assertEquals("pw", req.bodyJson?.str("password"))
        assertEquals("ref", creds.credential(fresh.server.id)?.refreshToken)
    }

    @Test
    fun expiredAccessTokenIsRefreshedOnce() = runTest {
        creds.save(Credential("old", "ref"), server.id)
        client.router = { req ->
            when (req.path) {
                "/auth/refresh" -> {
                    assertEquals("ref", req.headers["x-refresh-token"])
                    jsonResponse(200, """{"user": {"id": "u1", "username": "ada", "accessToken": "new", "refreshToken": "ref2"}}""")
                }
                "/api/libraries" -> if (req.headers["Authorization"] == "Bearer new") jsonResponse(200, """{"libraries": [{"id": "lib1", "name": "Audiobooks", "mediaType": "book"}]}""")
                else jsonResponse(401, """{"message": "expired"}""")
                else -> null
            }
        }
        val libs = provider.libraries()
        assertEquals(listOf("lib1"), libs.map { it.id })
        assertEquals(MediaKind.audiobook, libs.first().kind)
        assertEquals(listOf("/api/libraries", "/auth/refresh", "/api/libraries"), client.requests.map { it.path })
        assertEquals(Credential("new", "ref2"), creds.credential(server.id))
    }

    @Test
    fun refreshFailureIsUnauthorized() = runTest {
        creds.save(Credential("old", "ref"), server.id)
        client.router = { req -> if (req.path == "/auth/refresh") HttpResponse(401) else jsonResponse(401, """{"message": "expired"}""") }
        try { provider.libraries(); fail() } catch (e: ApiError) { assertEquals(ApiError.Unauthorized, e) }
    }

    @Test
    fun browseUsesPagesAndMinifiedShape() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"results": [{"id": "li1", "mediaType": "book", "updatedAt": 1760000000000,
            "media": {"metadata": {"title": "Dune", "authorName": "Frank Herbert", "narratorName": "Scott Brick", "seriesName": "Dune #1"},
                      "duration": 75000, "numAudioFiles": 3, "coverPath": "/x.jpg"},
            "userMediaProgress": {"progress": 0.25}}],
            "total": 120, "limit": 50, "page": 2}""")
        val page = provider.browse(null, null, LibrarySort.title, 100, 50)
        assertEquals(120, page.total)
        val item = page.items.first()
        assertEquals("Dune", item.title)
        assertEquals(listOf("Frank Herbert"), item.authors)
        assertEquals(listOf("Scott Brick"), item.narrators)
        assertEquals(0.25, item.progress)
        assertEquals("/api/items/li1/cover", Urls.path(item.coverUrl!!))
        assertEquals(1760000000000L, item.updatedAt)
        val q = client.last!!.query
        assertEquals("2", q["page"])
        assertEquals("50", q["limit"])
        assertEquals("media.metadata.title", q["sort"])
        assertEquals("/api/libraries/lib1/items", client.last?.path)
    }

    @Test
    fun searchUsesTheSearchRoute() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"book": [{"libraryItem": {"id": "li2", "mediaType": "book", "media": {"metadata": {"title": "Emma"}, "duration": 10}}}], "authors": []}""")
        val page = provider.browse("emma", null, LibrarySort.title, 0, 20)
        assertEquals("Emma", page.items.first().title)
        assertEquals("/api/libraries/lib1/search", client.last?.path)
        assertEquals("emma", client.last?.query?.get("q"))
    }

    @Test
    fun detailTracksChaptersAndCurrentTime() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"id": "li1", "mediaType": "book", "media": {
            "metadata": {"title": "Dune", "authors": [{"id": "a", "name": "Frank Herbert"}], "narrators": ["Scott Brick"], "description": "Spice", "series": [{"id": "s", "name": "Dune", "sequence": "1"}]},
            "duration": 1000, "coverPath": "/c.jpg",
            "tracks": [{"index": 1, "startOffset": 0, "duration": 600, "title": "01.mp3", "contentUrl": "/x/1", "mimeType": "audio/mpeg", "ino": "i1"},
                       {"index": 2, "startOffset": 600, "duration": 400, "title": "02.mp3", "contentUrl": "/x/2", "mimeType": "audio/mpeg", "ino": "i2"}],
            "chapters": [{"id": 0, "start": 0, "end": 500, "title": "One"}, {"id": 1, "start": 500, "end": 1000, "title": "Two"}]
        }, "userMediaProgress": {"currentTime": 650, "progress": 0.65, "lastUpdate": 1760000000000}}""")
        val d = provider.detail("li1")
        assertEquals(listOf("Frank Herbert"), d.item.authors)
        assertEquals(listOf("Scott Brick"), d.item.narrators)
        assertEquals("1", d.item.seriesSequence)
        assertEquals(listOf("i1", "i2"), d.tracks.map { it.id })
        assertEquals("/api/items/li1/file/i2", Urls.path(d.tracks[1].url))
        assertEquals(listOf("One", "Two"), d.chapters.map { it.title })
        assertEquals(650.0, d.position?.absolutePosition)
        assertEquals(1, d.position?.trackIndex)
        assertEquals(50.0, d.position?.positionInTrack)
        assertEquals("Spice", d.description)
        assertEquals("1", client.last?.query?.get("expanded"))
    }

    @Test
    fun playSessionSyncAndClose() = runTest {
        creds.save(Credential("t"), server.id)
        val detail = MediaDetail(item = MediaItem(id = "li1", title = "Dune", kind = MediaKind.audiobook, durationSeconds = 1000.0), assetId = "li1")
        client.enqueueJson(200, """{"id": "sess1", "audioTracks": [{"index": 0, "startOffset": 0, "duration": 1000, "title": "t", "contentUrl": "/public/session/sess1/track/0", "mimeType": "audio/mp4"}]}""")
        val session = provider.startPlayback(detail)
        assertEquals("sess1", session.sessionId)
        val play = client.last!!
        assertEquals("/api/items/li1/play", play.path)
        assertEquals("ExoPlayer", play.bodyJson?.str("mediaPlayer"))
        assertEquals("dev-1", play.bodyJson?.obj("deviceInfo")?.str("deviceId"))
        val tracks = provider.sessionTracks(parse("""{"audioTracks": [{"index": 0, "startOffset": 0, "duration": 1000, "contentUrl": "/public/session/sess1/track/0"}]}""") as JsonObject)
        assertEquals("https://abs.example.com/public/session/sess1/track/0", tracks.first().url)

        val update = ProgressUpdate(serverId = server.id, itemId = "li1", assetId = "li1", absolutePosition = 321.0, duration = 1000.0, listened = 15.0)
        client.enqueue(HttpResponse(200))
        provider.reportProgress(update, session)
        assertEquals("/api/session/sess1/sync", client.last?.path)
        assertEquals(321.0, client.last?.bodyJson?.dbl("currentTime"))
        assertEquals(15.0, client.last?.bodyJson?.dbl("timeListened"))

        client.enqueue(HttpResponse(200))
        client.enqueue(HttpResponse(200))
        provider.stopPlayback(update, session)
        assertEquals("/api/session/sess1/close", client.last?.path)
    }

    @Test
    fun progressWithoutSessionPatchesMeProgress() = runTest {
        creds.save(Credential("t"), server.id)
        val update = ProgressUpdate(serverId = server.id, itemId = "li1", assetId = "li1", absolutePosition = 500.0, duration = 1000.0, isFinished = false)
        client.enqueue(HttpResponse(200))
        provider.reportProgress(update, null)
        assertEquals(HttpMethod.PATCH, client.last?.method)
        assertEquals("/api/me/progress/li1", client.last?.path)
        assertEquals(0.5, client.last?.bodyJson?.dbl("progress"))
    }

    @Test
    fun continueListeningShelf() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """[{"id": "continue-listening", "label": "Continue Listening", "type": "book",
            "entities": [{"id": "li3", "mediaType": "book", "media": {"metadata": {"title": "Emma"}, "duration": 5}, "userMediaProgress": {"progress": 0.5}}]},
            {"id": "recently-added", "entities": []}]""")
        val items = provider.continueListening()
        assertEquals(listOf("li3"), items.map { it.id })
        assertEquals(0.5, items.first().progress)
    }
}
