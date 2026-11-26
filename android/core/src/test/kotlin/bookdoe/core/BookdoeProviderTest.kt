package bookdoe.core

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

class BookdoeProviderTest {
    private lateinit var client: MockHttpClient
    private lateinit var creds: InMemoryCredentialStore
    private lateinit var provider: BookdoeProvider
    private val server = makeServer(ServerKind.bookdoe)

    @Before
    fun setUp() {
        client = MockHttpClient()
        creds = InMemoryCredentialStore()
        provider = BookdoeProvider(server, client, creds, "1.2")
    }

    private fun capabilities(passwordLogin: Boolean = true, setupRequired: Boolean = false) = """
        {"server": "bookdoe", "version": "0.9.1", "api_version": "1", "setup_required": $setupRequired,
         "auth": {"device_pairing": true, "password_login": $passwordLogin, "auth_mode": "local", "poll_interval": 5, "code_lifetime": 900}}
    """

    @Test
    fun probeReadsCapabilities() = runTest {
        client.enqueueJson(200, capabilities())
        val info = provider.probe()
        assertEquals("bookdoe", info.name)
        assertEquals("0.9.1", info.version)
        assertTrue(info.supportsPasswordLogin)
        assertTrue(info.supportsDevicePairing)
        assertEquals("/api/player/v1/capabilities", client.last?.path)
        assertNull(client.last?.headers?.get("Authorization"))
    }

    @Test
    fun loginMintsDeviceTokenAndUpdatesServer() = runTest {
        client.enqueueJson(200, capabilities())
        client.enqueueJson(200, """{"token": "alxd_abc", "token_type": "Bearer", "device_id": "d1", "name": "Pixel",
            "user": {"id": "u1", "username": "ada", "account_type": "admin", "display_name": "Ada"}}""")
        val result = provider.login("ada", "pw", "Pixel")
        assertEquals("alxd_abc", result.credential.accessToken)
        assertEquals("u1", result.server.userId)
        assertEquals("Ada", result.displayName)
        val body = client.last?.bodyJson!!
        assertEquals("ada", body.str("username"))
        assertEquals("pw", body.str("password"))
        assertEquals("android", body.str("platform"))
        assertEquals("1.2", body.str("client_version"))
        assertEquals("/api/player/v1/auth/login", client.last?.path)
    }

    @Test
    fun loginRefusedWhenServerNeedsSetupOrHasNoPasswordLogin() = runTest {
        client.enqueueJson(200, capabilities(setupRequired = true))
        try { provider.login("k", "p", "d"); fail() } catch (e: ApiError) { assertEquals(ApiError.SetupRequired, e) }
        client.enqueueJson(200, capabilities(passwordLogin = false))
        try { provider.login("k", "p", "d"); fail() } catch (e: ApiError) { assertTrue(e is ApiError.NotSupported) }
    }

    @Test
    fun pairingFlow() = runTest {
        client.enqueueJson(201, """{"device_code": "dc", "user_code": "GHKM-7T4P", "verification_uri_complete": "https://books.example.com/app#pair/GHKM-7T4P", "interval": 5, "expires_in": 900}""")
        val pairing = provider.startPairing("Tablet")
        assertEquals("GHKM-7T4P", pairing.userCode)
        client.enqueueJson(400, """{"error": "authorization_pending"}""")
        client.enqueueJson(400, """{"error": "slow_down"}""")
        client.enqueueJson(403, """{"error": "access_denied"}""")
        client.enqueueJson(200, """{"token": "alxd_paired", "device_id": "d2", "user": {"id": "u1", "username": "ada"}}""")
        assertEquals(PairingPoll.Pending, provider.pollPairing(pairing))
        assertEquals(PairingPoll.SlowDown, provider.pollPairing(pairing))
        assertEquals(PairingPoll.Denied, provider.pollPairing(pairing))
        val poll = provider.pollPairing(pairing)
        val approved = poll as? PairingPoll.Approved ?: return@runTest fail("expected approved")
        assertEquals("alxd_paired", approved.result.credential.accessToken)
        assertEquals("dc", client.last?.bodyJson?.str("device_code"))
    }

    @Test
    fun browseSendsBearerAndMapsCompactWorks() = runTest {
        creds.save(Credential("alxd_tok"), server.id)
        client.enqueueJson(200, """{"items": [{
            "id": "w1", "title": "Dune", "subtitle": null, "authors": [{"id": "a1", "name": "Frank Herbert"}],
            "narrators": [{"id": "n1", "name": "Scott Brick"}], "series": [{"id": "s1", "name": "Dune", "position": "1"}],
            "has_cover": true, "cover_url": "/api/player/v1/library/w1/cover",
            "assets": [{"id": "as1", "kind": "audiobook", "format": "m4b", "readable": true, "duration_seconds": 75000}],
            "formats": ["audiobook"], "updated_at": 1760000000.0
        }], "total": 1, "limit": 50, "offset": 0, "server_time": 1760000001.0}""")
        val page = provider.browse("dune", MediaKind.audiobook, LibrarySort.title, 0, 50)
        assertEquals(1, page.total)
        val item = page.items.first()
        assertEquals("Dune", item.title)
        assertEquals(listOf("Frank Herbert"), item.authors)
        assertEquals(listOf("Scott Brick"), item.narrators)
        assertEquals("Dune", item.series)
        assertEquals(MediaKind.audiobook, item.kind)
        assertEquals(75000.0, item.durationSeconds)
        assertEquals("/api/player/v1/library/w1/cover", Urls.path(item.coverUrl!!))
        assertEquals("384", Urls.queryMap(item.coverUrl!!)["size"])
        assertNull(page.nextOffset)
        val req = client.last!!
        assertEquals("Bearer alxd_tok", req.headers["Authorization"])
        assertEquals("dune", req.query["q"])
        assertEquals("audiobook", req.query["kind"])
        assertEquals("50", req.query["limit"])
    }

    @Test
    fun browseWithoutCredentialIsUnauthorized() = runTest {
        try { provider.browse(null, null, LibrarySort.title, 0, 10); fail() } catch (e: ApiError) { assertEquals(ApiError.Unauthorized, e) }
        assertTrue(client.requests.isEmpty())
    }

    @Test
    fun detailBuildsTracksChaptersAndPosition() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"id": "w1", "title": "Dune", "authors": [{"id": "a", "name": "F"}], "narrators": [], "series": [], "has_cover": false, "cover_url": null,
            "assets": [{"id": "as1", "kind": "audiobook", "format": "mp3", "readable": true, "duration_seconds": null}], "formats": ["audiobook"],
            "description": "Spice.", "updated_at": 1.0, "progress": [], "favorite": false}""")
        client.enqueueJson(200, """{"work_id": "w1", "title": "Dune", "asset_id": "as1", "kind": "audiobook", "format": "mp3",
            "files": [{"id": "f1", "title": "Part 1", "part_number": 1, "size_bytes": 1000, "duration_seconds": 600, "url": "/api/v1/library/w1/files/f1"},
                      {"id": "f2", "title": "Part 2", "part_number": 2, "size_bytes": 1000, "duration_seconds": 400, "url": "/api/v1/library/w1/files/f2"}],
            "chapters": [], "duration_seconds": 1000, "download_eligible": true,
            "progress": {"file_id": "f2", "position": 100, "location": null, "percent": 0.7, "updated_at": 5.0, "device_id": "x", "client_updated_at": 4.0, "duration": 400}}""")
        val d = provider.detail("w1")
        assertEquals("as1", d.assetId)
        assertEquals(2, d.tracks.size)
        assertEquals(600.0, d.tracks[1].startOffset, 0.0)
        assertEquals("https://books.example.com/api/v1/library/w1/files/f1", d.tracks[0].url)
        assertEquals(1000.0, d.totalDuration, 0.0)
        assertEquals(listOf("Part 1", "Part 2"), d.effectiveChapters.map { it.title })
        assertEquals(1, d.position?.trackIndex)
        assertEquals(100.0, d.position?.positionInTrack)
        assertEquals(700.0, d.position?.absolutePosition)
        assertEquals("Spice.", d.description)
        assertEquals(1, d.locate(650.0).index)
        assertEquals(50.0, d.locate(650.0).offset, 0.0)
    }

    @Test
    fun ebookDetailExposesDownloadUrl() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"id": "w2", "title": "Emma", "authors": [], "narrators": [], "series": [], "has_cover": false,
            "assets": [{"id": "e1", "kind": "ebook", "format": "epub", "readable": true}], "formats": ["ebook"], "updated_at": 1.0}""")
        client.enqueueJson(200, """{"work_id": "w2", "asset_id": "e1", "kind": "ebook", "format": "epub", "files": [{"id": "f", "url": "/api/v1/library/w2/files/f"}],
            "download_url": "/api/v1/library/w2/files/f", "progress": {"location": "epubcfi(/6/4)", "percent": 0.2, "updated_at": 9.0}}""")
        val d = provider.detail("w2")
        assertEquals(MediaKind.ebook, d.item.kind)
        assertEquals("epub", d.ebookFormat)
        assertEquals("/api/v1/library/w2/files/f", Urls.path(d.ebookDownloadUrl!!))
        assertEquals("epubcfi(/6/4)", d.position?.location)
    }

    @Test
    fun progressReportSendsBatchItemAndHandlesStale() = runTest {
        creds.save(Credential("t"), server.id)
        val at = 1760000000L * 1000
        val update = ProgressUpdate(serverId = server.id, itemId = "w1", assetId = "as1", trackId = "f2", trackIndex = 1, positionInTrack = 120.0,
            absolutePosition = 720.0, duration = 400.0, percent = 0.72, clientUpdatedAt = at)
        client.enqueueJson(200, """{"items": [{"asset_id": "as1", "applied": true}], "server_time": 1.0}""")
        var r = provider.reportProgress(update, null)
        assertTrue(r.applied)
        val body = client.last?.bodyJson!!
        val items = body.arr("items")
        assertEquals("f2", items.first().str("file_id"))
        assertEquals(120.0, items.first().dbl("position"))
        assertEquals("dev-1", items.first().str("device_id"))
        assertEquals(1760000000.0, items.first().dbl("client_updated_at"))
        assertEquals(HttpMethod.PUT, client.last?.method)

        client.enqueueJson(200, """{"items": [{"asset_id": "as1", "applied": false, "file_id": "f1", "position": 30, "percent": 0.1, "updated_at": 2.0, "client_updated_at": 1760000500}]}""")
        r = provider.reportProgress(update, null)
        assertFalse(r.applied)
        assertEquals("f1", r.serverPosition?.trackId)
        assertEquals(30.0, r.serverPosition?.positionInTrack)

        client.enqueueJson(200, """{"items": [{"asset_id": "as1", "applied": false, "error": "not_found"}]}""")
        try { provider.reportProgress(update, null); fail() } catch (e: ApiError) { assertEquals(ApiError.NotFound, e) }
    }

    @Test
    fun continueListeningMapsFlatRows() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"items": [{"asset_id": "as1", "kind": "audiobook", "file_id": "f1", "position": 5, "percent": 0.4, "updated_at": 3.0,
            "work_id": "w1", "title": "Dune", "has_cover": true, "authors": "Frank Herbert, Brian Herbert"}]}""")
        val items = provider.continueListening()
        assertEquals("w1", items.first().id)
        assertEquals(listOf("Frank Herbert", "Brian Herbert"), items.first().authors)
        assertEquals(0.4, items.first().progress)
        assertNotNull(items.first().coverUrl)
    }

    @Test
    fun continueListeningInterleavesPodcastEpisodesAndTapPlaysThem() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"items": [
            {"kind": "podcast", "episode_id": "ep1", "show_id": "s1", "show_title": "The Show", "title": "Pilot",
             "duration_seconds": 1000.0, "position_seconds": 250.0, "updated_at": 5.0},
            {"asset_id": "as1", "kind": "audiobook", "work_id": "w1", "title": "Dune", "percent": 0.4, "updated_at": 3.0, "authors": "Frank Herbert"}]}""")
        val row = ContinueRow(provider.continueListening())
        assertEquals(listOf("ep1", "w1"), row.items.map { it.id })
        assertEquals("The Show", row.subtitle(row.items[0]))
        assertEquals("Frank Herbert", row.subtitle(row.items[1]))
        assertEquals(0.25, row.items[0].progress)
        val played = mutableListOf<String>()
        val opened = mutableListOf<String>()
        row.items.forEach { row.tap(it, { e -> played += e.id }, { id -> opened += id }) }
        assertEquals(listOf("ep1"), played)
        assertEquals(listOf("w1"), opened)
    }

    @Test
    fun episodeDetailLoadsManifestForPlayerAndProgressGoesToPodcastRoute() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"episode_id": "ep1", "show_id": "s1", "show_title": "The Show", "title": "Pilot", "duration_seconds": 1000.0,
            "url": "/api/player/v1/podcasts/episodes/ep1/audio", "position_seconds": 250.0}""")
        val d = provider.episodeDetail("ep1")
        assertEquals("Pilot", d.item.title)
        assertEquals("/api/player/v1/podcasts/episodes/ep1/audio", Urls.path(d.tracks.single().url))
        assertEquals(250.0, d.position?.positionInTrack)

        client.enqueueJson(200, """{"episode_id": "ep1"}""")
        provider.reportProgress(ProgressUpdate(serverId = server.id, itemId = "ep1", assetId = d.assetId, absolutePosition = 300.0, clientUpdatedAt = 1L), null)
        assertEquals("/api/player/v1/podcasts/episodes/ep1/progress", Urls.path(client.last!!.url))
        assertEquals(HttpMethod.POST, client.last?.method)
        assertEquals(300.0, client.last?.bodyJson?.dbl("position_seconds"))
    }

    @Test
    fun continueRowIsHiddenWhenFeedIsEmpty() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"items": []}""")
        assertFalse(ContinueRow(provider.continueListening()).visible)
    }

    @Test
    fun errorMapping() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueue(HttpResponse(429, mapOf("Retry-After" to "30")))
        try { provider.detail("x"); fail() } catch (e: ApiError) { assertEquals(ApiError.RateLimited(30), e) }
        client.enqueueJson(403, """{"detail": "Your account is awaiting approval"}""")
        try { provider.detail("x"); fail() } catch (e: ApiError) { assertEquals(ApiError.Forbidden("Your account is awaiting approval"), e) }
    }

    @Test
    fun logoutDeletesDeviceAndForgetsCredential() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"user": {"id": "u1"}, "device": {"id": "dev9", "name": "Pixel"}}""")
        client.enqueue(HttpResponse(204))
        provider.logout()
        assertNull(creds.credential(server.id))
        assertEquals("/api/player/v1/devices/dev9", client.last?.path)
        assertEquals(HttpMethod.DELETE, client.last?.method)
    }
}
