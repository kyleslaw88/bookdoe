package bookdoe.core

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** The #514 show response: seasons 1, 2, 10 and Extras, in the server's order (same fixture as the iOS test). */
private const val FIXTURE = """
{"id": "show-1", "title": "Alpha Show", "cover_url": "/api/v1/podcasts/show-1/cover", "seasons": [
  {"id": "s1", "number": 1, "is_extras": false, "episodes": [
    {"id": "e1", "title": "One", "release_date": "2024-01-01", "duration_seconds": 600.0, "description": "<p>First &amp; best</p>",
     "missing": false, "played": true, "position_seconds": 600.0}]},
  {"id": "s2", "number": 2, "is_extras": false, "episodes": [
    {"id": "e2", "title": "Two", "release_date": "2024-03-01", "duration_seconds": 600.0, "description": "",
     "missing": false, "played": false, "position_seconds": 0.0}]},
  {"id": "s10", "number": 10, "is_extras": false, "episodes": []},
  {"id": "sx", "number": null, "is_extras": true, "episodes": [
    {"id": "e3", "title": "Recap", "release_date": "2024-05-01", "duration_seconds": 300.0, "description": "Bonus",
     "missing": true, "played": false, "position_seconds": 0.0}]}
]}
"""

class PodcastShowViewModelTest {
    private fun model() = PodcastShowViewModel(parsePodcastShow(parse(FIXTURE) as kotlinx.serialization.json.JsonObject, "https://books.example.com")!!)

    @Test
    fun coverIsTheDeviceReachableRoute() {
        assertEquals("https://books.example.com/api/player/v1/podcasts/shows/show-1/cover", model().coverUrl)
    }

    @Test
    fun seasonsAreNumberedInOrderWithExtrasLast() {
        assertEquals(listOf("Season 1", "Season 2", "Season 10", "Extras"), model().seasonTitles.map { it.second })
    }

    @Test
    fun rowsShowTitleDateDescriptionAndPlayedState() {
        var vm = model()
        assertEquals(listOf(PodcastEpisodeRow("e1", "One", "2024-01-01", "First & best", played = true, playable = true)), vm.rows)
        vm = vm.select("s2")
        assertFalse(vm.rows.first().played)
    }

    @Test
    fun extrasRowsAndMissingEpisodeNotPlayable() {
        val vm = model().select("sx")
        assertEquals(listOf("Recap"), vm.rows.map { it.title })
        assertFalse(vm.rows.first().playable)
    }

    @Test
    fun selectingUnknownSeasonKeepsSelection() {
        assertEquals("s1", model().select("nope").selectedSeasonId)
    }
}

class PodcastPlaybackTest {
    private lateinit var client: MockHttpClient
    private lateinit var provider: BookdoeProvider
    private val loaded = ArrayList<MediaDetail>()
    private val fakePlayer = EpisodePlayer { loaded.add(it) }

    @Before
    fun setUp() {
        client = MockHttpClient()
        val server = makeServer(ServerKind.bookdoe)
        val creds = InMemoryCredentialStore()
        creds.save(Credential("alxd_tok"), server.id)
        provider = BookdoeProvider(server, client, creds, "1.0")
    }

    private fun show() = parsePodcastShow(parse(FIXTURE) as kotlinx.serialization.json.JsonObject, "https://books.example.com")!!

    @Test
    fun showIsFetchedFromThePlayerRouteWithTheDeviceToken() = runTest {
        client.enqueueJson(200, FIXTURE)
        val s = provider.podcastShow("show-1")
        assertEquals("/api/player/v1/podcasts/shows/show-1", client.last?.path)
        assertEquals("Bearer alxd_tok", client.last?.headers?.get("Authorization"))
        assertEquals(4, s.seasons.size)
    }

    @Test
    fun showListUsesThePlayerRoute() = runTest {
        client.enqueueJson(200, """{"shows": [{"id": "show-1", "title": "Alpha Show", "cover_url": "/x", "season_count": 3, "has_extras": true}]}""")
        assertEquals(listOf(PodcastShowRef("show-1", "Alpha Show")), provider.podcastShows())
        assertEquals("/api/player/v1/podcasts", client.last?.path)
    }

    @Test
    fun tappingEpisodeLoadsItIntoPlayerAtStoredPositionWithShowName() = runTest {
        client.enqueueJson(200, """{"episode_id": "e2", "progress": {"position_seconds": 123.5, "client_updated_at": 1.0, "played": false}}""")
        val s = show()
        PodcastPlayback.play(s.seasons[1].episodes[0], s, provider, fakePlayer)
        assertEquals("/api/player/v1/podcasts/episodes/e2/progress", client.last?.path)
        val d = loaded.single()
        assertEquals(123.5, d.position?.absolutePosition ?: -1.0, 0.0)
        assertEquals(123.5, d.position?.positionInTrack ?: -1.0, 0.0)
        assertEquals("https://books.example.com/api/player/v1/podcasts/episodes/e2/audio", d.tracks.first().url)
        assertEquals("Two", d.item.title)              // lock-screen title
        assertEquals("Alpha Show", d.item.authorLine)  // lock-screen artist
    }

    @Test
    fun noSavedPositionStartsAtZero() = runTest {
        client.enqueueJson(200, """{"episode_id": "e2", "progress": null}""")
        val s = show()
        PodcastPlayback.play(s.seasons[1].episodes[0], s, provider, fakePlayer)
        assertEquals(0.0, loaded.single().position?.absolutePosition ?: -1.0, 0.0)
    }

    @Test
    fun positionChangeIsWrittenToThePodcastProgressEndpoint() = runTest {
        client.enqueueJson(200, """{"episode_id": "e2", "progress": null}""")
        val s = show()
        PodcastPlayback.play(s.seasons[1].episodes[0], s, provider, fakePlayer)
        val detail = loaded.single()
        client.enqueueJson(200, """{"episode_id": "e2", "applied": true, "progress": {"position_seconds": 42.0, "client_updated_at": 1700000000, "played": false}}""")
        val update = ProgressUpdate(serverId = provider.server.id, itemId = detail.item.id, assetId = detail.assetId, trackId = "e2",
            positionInTrack = 42.0, absolutePosition = 42.0, clientUpdatedAt = 1_700_000_000_000L)
        assertTrue(provider.reportProgress(update, null).applied)
        val req = client.last!!
        assertEquals(HttpMethod.PUT, req.method)
        assertEquals("/api/player/v1/podcasts/episodes/e2/progress", req.path)
        assertEquals(42.0, req.bodyJson!!.dbl("position_seconds")!!, 0.0)
        assertEquals(1_700_000_000.0, req.bodyJson!!.dbl("client_updated_at")!!, 0.0)
    }

    @Test
    fun staleWriteReturnsServerPosition() = runTest {
        client.enqueueJson(200, """{"episode_id": "e2", "applied": false, "progress": {"position_seconds": 900.0, "client_updated_at": 5.0, "played": false}}""")
        val report = provider.reportProgress(ProgressUpdate(serverId = "x", itemId = "e2", assetId = "podcast:e2", absolutePosition = 10.0), null)
        assertFalse(report.applied)
        assertNotNull(report.serverPosition)
        assertEquals(900.0, report.serverPosition!!.absolutePosition, 0.0)
    }
}
