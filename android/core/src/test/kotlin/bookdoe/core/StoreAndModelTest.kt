package bookdoe.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.File
import java.util.UUID

class StoreAndModelTest {
    private fun tempFile(prefix: String) = File(System.getProperty("java.io.tmpdir"), "$prefix-${UUID.randomUUID()}.json")

    @Test
    fun serverStorePersistsWithoutCredentials() {
        val file = tempFile("servers")
        try {
            val store = ServerStore(file)
            val a = makeServer(ServerKind.bookdoe)
            val b = makeServer(ServerKind.jellyfin)
            store.add(a)
            store.add(b)
            assertEquals(a.id, store.active?.id)
            store.setActive(b.id)
            store.update(b.copy(name = "Living room"))
            val reloaded = ServerStore(file)
            assertEquals(listOf("Test", "Living room"), reloaded.servers.map { it.name })
            assertEquals(b.id, reloaded.activeServerId)
            reloaded.remove(b.id)
            assertEquals(a.id, reloaded.active?.id)
            val raw = file.readText()
            assertFalse(raw.contains("accessToken"))
        } finally {
            file.delete()
        }
    }

    @Test
    fun inMemoryCredentialStore() {
        val store = InMemoryCredentialStore()
        val id = UUID.randomUUID().toString()
        assertNull(store.credential(id))
        store.save(Credential("x", "y"), id)
        assertEquals("y", store.credential(id)?.refreshToken)
        store.remove(id)
        assertNull(store.credential(id))
    }

    @Test
    fun urlAppendingHandlesBasePathsAndQuery() {
        val base = "https://host.example.com/jellyfin/"
        val u = Urls.appending(base, "/Items", mapOf("userId" to "u", "limit" to "5", "skip" to null))
        assertEquals("/jellyfin/Items", Urls.path(u))
        assertEquals(mapOf("userId" to "u", "limit" to "5"), Urls.queryMap(u))
        assertEquals("https://h.example.com/api/x", Urls.appending("https://h.example.com", "api/x"))
        assertEquals("https://host.example.com/jellyfin/public/session/1/track/0", Urls.resolving(base, "/public/session/1/track/0"))
        assertEquals("https://cdn.example.com/a.mp3", Urls.resolving(base, "https://cdn.example.com/a.mp3"))
        assertEquals("http://lan-box:8795", ServerRecord.normalizeBaseUrl(" lan-box:8795/ "))
        assertNull(ServerRecord.normalizeBaseUrl("not a url at all :"))
    }

    @Test
    fun downloadPlanNamesFilesSafely() {
        val server = makeServer(ServerKind.bookdoe)
        val tracks = listOf(
            Track("f/1", 0, "One", "https://x/api/v1/library/w/files/f1", durationSeconds = 10.0, mimeType = "audio/mp4", sizeBytes = 100),
            Track("f2", 1, "Two", "https://x/a/b.mp3", durationSeconds = 20.0, sizeBytes = 50),
        )
        val detail = MediaDetail(item = MediaItem(id = "work 1", title = "T", kind = MediaKind.audiobook), tracks = tracks, assetId = "as", ebookDownloadUrl = "https://x/f/e", ebookFormat = "epub")
        val plan = DownloadPlan.from(server.id, detail, mapOf("Authorization" to "Bearer t"))
        assertEquals(3, plan.files.size)
        assertEquals("${server.id}/work_1/000-f_1.m4b", plan.files[0].localRelativePath)
        assertEquals("${server.id}/work_1/001-f2.mp3", plan.files[1].localRelativePath)
        assertEquals("${server.id}/work_1/book.epub", plan.files[2].localRelativePath)
        assertEquals(150L, plan.totalExpectedBytes)
        assertEquals("Bearer t", plan.files[0].headers["Authorization"])
    }

    @Test
    fun downloadIndexRoundTripAndOfflineDetail() {
        val file = tempFile("dl")
        try {
            val server = makeServer(ServerKind.audiobookshelf)
            val detail = MediaDetail(item = MediaItem(id = "li1", title = "Dune", authors = listOf("F"), kind = MediaKind.audiobook, durationSeconds = 30.0), chapters = listOf(Chapter("c", 0.0)), assetId = "li1")
            val item = DownloadedItem.from(server.id, detail, listOf(
                DownloadedTrack("a", "A", "s/li1/000-a.mp3", 10.0),
                DownloadedTrack("b", "B", "s/li1/001-b.mp3", 20.0),
            ), bytes = 99)
            val index = DownloadIndex(file)
            index.upsert(item)
            val reloaded = DownloadIndex(file)
            assertEquals(99L, reloaded.item(server.id, "li1")?.bytes)
            val offline = reloaded.items[0].detail(File("/tmp/dl"))
            assertEquals(2, offline.tracks.size)
            assertEquals(10.0, offline.tracks[1].startOffset, 0.0)
            assertEquals("file:/tmp/dl/s/li1/001-b.mp3", offline.tracks[1].url)
            assertEquals(1, offline.chapters.size)
            assertEquals("li1", offline.assetId)
            assertNotNull(reloaded.remove(server.id, "li1"))
            assertNull(reloaded.item(server.id, "li1"))
        } finally {
            file.delete()
        }
    }

    @Test
    fun libraryPageNextOffset() {
        assertEquals(1, LibraryPage(listOf(MediaItem(id = "1", title = "a", kind = MediaKind.ebook)), 3, 0, 1).nextOffset)
        assertNull(LibraryPage(listOf(MediaItem(id = "3", title = "c", kind = MediaKind.ebook)), 3, 2, 1).nextOffset)
        assertNull(LibraryPage(emptyList(), 3, 0, 1).nextOffset)
    }

    @Test
    fun apiErrorFromResponse() {
        assertEquals(ApiError.Unauthorized, ApiError.from(HttpResponse(401)))
        assertEquals(ApiError.Forbidden("no"), ApiError.from(jsonResponse(403, """{"detail": "no"}""")))
        assertEquals(ApiError.Server(500, "boom"), ApiError.from(jsonResponse(500, """{"message": "boom"}""")))
        assertEquals(ApiError.RateLimited(7), ApiError.from(HttpResponse(429, mapOf("retry-after" to "7"))))
    }

    @Test
    fun providerFactoryPicksByKind() {
        val f = ProviderFactory(MockHttpClient(), InMemoryCredentialStore())
        assertEquals(ServerKind.bookdoe, f.make(makeServer(ServerKind.bookdoe)).kind)
        assertEquals(ServerKind.jellyfin, f.make(makeServer(ServerKind.jellyfin)).kind)
        assertEquals(ServerKind.audiobookshelf, f.make(makeServer(ServerKind.audiobookshelf)).kind)
    }
}
