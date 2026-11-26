package bookdoe.core

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class JellyfinProviderTest {
    private lateinit var client: MockHttpClient
    private lateinit var creds: InMemoryCredentialStore
    private lateinit var provider: JellyfinProvider
    private val server = makeServer(ServerKind.jellyfin, base = "https://jf.example.com/jellyfin", libraryId = "lib1", userId = "user1")

    @Before
    fun setUp() {
        client = MockHttpClient()
        creds = InMemoryCredentialStore()
        provider = JellyfinProvider(server, client, creds, "1.0")
    }

    @Test
    fun probe() = runTest {
        client.enqueueJson(200, """{"ServerName": "Stable Demo", "Version": "12.2.0", "Id": "srv"}""")
        val info = provider.probe()
        assertEquals("Stable Demo", info.name)
        assertEquals("12.2.0", info.version)
        assertEquals("https://jf.example.com/jellyfin/System/Info/Public", client.last?.url)
    }

    @Test
    fun loginUsesPwFieldAndMediaBrowserHeader() = runTest {
        val fresh = JellyfinProvider(makeServer(ServerKind.jellyfin, base = "https://jf.example.com"), client, creds, "1.0")
        client.enqueueJson(200, """{"AccessToken": "jftok", "ServerId": "s", "User": {"Id": "u9", "Name": "demo"}, "SessionInfo": {}}""")
        client.enqueueJson(200, """{"Items": [{"Id": "v1", "Name": "Movies", "CollectionType": "movies"}, {"Id": "v2", "Name": "Audiobooks", "CollectionType": "books"}]}""")
        val r = fresh.login("demo", "secret", "Pixel")
        assertEquals("jftok", r.credential.accessToken)
        assertEquals("u9", r.server.userId)
        assertEquals("the books view is picked by default", "v2", r.server.libraryId)
        val login = client.request("/Users/AuthenticateByName")!!
        assertEquals("secret", login.bodyJson?.str("Pw"))
        assertEquals("demo", login.bodyJson?.str("Username"))
        val auth = login.headers["Authorization"]!!
        assertTrue(auth.startsWith("MediaBrowser "))
        assertTrue(auth.contains("Client=\"bookdoe\"") && auth.contains("DeviceId=\"dev-1\""))
        assertFalse(auth.contains("Token="))
        assertEquals("jftok", creds.credential(fresh.server.id)?.accessToken)
        val views = client.request("/UserViews")!!
        assertTrue(views.headers["Authorization"]?.contains("Token=\"jftok\"") ?: false)
    }

    @Test
    fun browseHidesChildrenOfListedFolders() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"Items": [
            {"Id": "folder1", "Name": "Dune", "Type": "Folder", "ChildCount": 2},
            {"Id": "file1", "Name": "Dune Part 1", "Type": "AudioBook", "ParentId": "folder1", "RunTimeTicks": 6000000000},
            {"Id": "single", "Name": "Emma", "Type": "AudioBook", "ParentId": "lib1", "RunTimeTicks": 36000000000, "Artists": ["Jane Austen"],
             "UserData": {"PlaybackPositionTicks": 18000000000, "Played": false}, "ImageTags": {"Primary": "abc"}}
        ], "TotalRecordCount": 3}""")
        val page = provider.browse(null, null, LibrarySort.added, 0, 50)
        assertEquals(listOf("folder1", "single"), page.items.map { it.id })
        val emma = page.items[1]
        assertEquals(3600.0, emma.durationSeconds)
        assertEquals(0.5, emma.progress)
        assertEquals(listOf("Jane Austen"), emma.authors)
        assertEquals("/jellyfin/Items/single/Images/Primary", Urls.path(emma.coverUrl!!))
        val q = client.last!!.query
        assertEquals("lib1", q["parentId"])
        assertEquals("user1", q["userId"])
        assertEquals("DateCreated", q["sortBy"])
        assertEquals("0", q["startIndex"])
        assertEquals("true", q["recursive"])
    }

    @Test
    fun detailOfFolderOrdersChildrenAndFindsPosition() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueueJson(200, """{"Id": "folder1", "Name": "Dune", "Type": "Folder", "Overview": "Spice"}""")
        client.enqueueJson(200, """{"Items": [
            {"Id": "c1", "Name": "Part 1", "Type": "AudioBook", "IndexNumber": 1, "RunTimeTicks": 6000000000, "MediaSources": [{"Id": "m", "Container": "mp3"}], "UserData": {"PlaybackPositionTicks": 0}},
            {"Id": "c2", "Name": "Part 2", "Type": "AudioBook", "IndexNumber": 2, "RunTimeTicks": 4000000000, "UserData": {"PlaybackPositionTicks": 1000000000, "LastPlayedDate": "2026-10-08T10:00:00Z"}}
        ]}""")
        val d = provider.detail("folder1")
        assertEquals(2, d.tracks.size)
        assertEquals(600.0, d.tracks[1].startOffset, 0.0)
        assertEquals(1000.0, d.totalDuration, 0.0)
        assertEquals("audio/mp3", d.tracks[0].mimeType)
        assertEquals("/jellyfin/Audio/c1/universal", Urls.path(d.tracks[0].url))
        assertEquals("t", Urls.queryMap(d.tracks[0].url)["api_key"])
        assertEquals(1, d.position?.trackIndex)
        assertEquals(100.0, d.position?.positionInTrack)
        assertEquals(700.0, d.position?.absolutePosition)
        assertEquals(0.7, d.position?.percent!!, 1e-9)
        assertNotNull(d.position?.updatedAt)
        assertEquals("Spice", d.description)
    }

    @Test
    fun playbackReportsUseTicksAndSession() = runTest {
        creds.save(Credential("t"), server.id)
        val detail = MediaDetail(item = MediaItem(id = "single", title = "Emma", kind = MediaKind.audiobook),
            tracks = listOf(Track("single", 0, "Emma", "https://x/y", durationSeconds = 100.0)), assetId = "single")
        client.enqueue(HttpResponse(204))
        val session = provider.startPlayback(detail)
        assertNotNull(session.sessionId)
        assertEquals("/jellyfin/Sessions/Playing", client.last?.path)
        assertEquals("single", client.last?.bodyJson?.str("ItemId"))

        val update = ProgressUpdate(serverId = server.id, itemId = "single", assetId = "single", trackId = "single", positionInTrack = 12.5, absolutePosition = 12.5, duration = 100.0)
        client.enqueue(HttpResponse(204))
        val r = provider.reportProgress(update, session)
        assertTrue(r.applied)
        assertEquals("/jellyfin/Sessions/Playing/Progress", client.last?.path)
        assertEquals(125_000_000L, client.last?.bodyJson?.lng("PositionTicks"))
        assertEquals(session.sessionId, client.last?.bodyJson?.str("PlaySessionId"))

        client.enqueue(HttpResponse(204))
        provider.stopPlayback(update, session)
        assertEquals("/jellyfin/Sessions/Playing/Stopped", client.last?.path)

        val finished = update.copy(isFinished = true)
        client.enqueue(HttpResponse(204))
        client.enqueueJson(200, """{"Played": true}""")
        provider.reportProgress(finished, session)
        assertEquals("/jellyfin/UserPlayedItems/single", client.last?.path)
    }

    @Test
    fun logoutRemovesCredentialEvenIfServerFails() = runTest {
        creds.save(Credential("t"), server.id)
        client.enqueue(HttpResponse(500))
        provider.logout()
        assertNull(creds.credential(server.id))
        assertEquals("/jellyfin/Sessions/Logout", client.last?.path)
    }
}
