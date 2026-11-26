package bookdoe.core

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/** In-memory storage: path to bytes. */
class FakeEpisodeStorage : EpisodeStorage {
    val files = HashMap<String, ByteArray>()
    override fun exists(relativePath: String) = files.containsKey(relativePath)
    override fun size(relativePath: String) = files[relativePath]?.size?.toLong() ?: 0L
    override fun delete(relativePath: String) = files.remove(relativePath) != null
    override fun uri(relativePath: String) = "fake://downloads/$relativePath"
    fun read(uri: String): ByteArray? = files[uri.removePrefix("fake://downloads/")]
}

/** Fake network: serves [body] into the storage while [online]; otherwise throws. [gate] holds a fetch open. */
class FakeEpisodeNetwork(private val storage: FakeEpisodeStorage) : EpisodeFetcher {
    var online = true
    var body = ByteArray(1234) { it.toByte() }
    var gate: CompletableDeferred<Unit>? = null
    val fetched = ArrayList<String>()
    var leavePartialOnFailure = false

    override suspend fun fetch(url: String, headers: Map<String, String>, relativePath: String): Long {
        fetched.add(url)
        gate?.await()
        if (!online) {
            if (leavePartialOnFailure) storage.files[relativePath] = ByteArray(10)
            throw IOException("offline")
        }
        storage.files[relativePath] = body
        return body.size.toLong()
    }
}

private fun episodeDetail(id: String = "ep1") = MediaDetail(
    item = MediaItem(id = id, title = "Pilot", authors = listOf("The Show"), kind = MediaKind.audiobook, durationSeconds = 1000.0),
    tracks = listOf(Track(id = id, index = 0, title = "Pilot", url = "https://books.example.com/api/player/v1/podcasts/episodes/$id/audio", durationSeconds = 1000.0)),
    assetId = BookdoeProvider.PODCAST_ASSET_PREFIX + id,
)

class PodcastDownloadStateMachineTest {
    private val storage = FakeEpisodeStorage()
    private val network = FakeEpisodeNetwork(storage)
    private val index = DownloadIndex()
    private val downloads = PodcastDownloads("srv", storage, network, index)
    private val headers = mapOf("Authorization" to "Bearer t")

    @Test
    fun startsNotDownloadedAndEndsDownloaded() = runTest {
        assertEquals(EpisodeDownloadState.NotDownloaded, downloads.state("ep1"))
        val end = downloads.download(episodeDetail(), headers)
        assertEquals(EpisodeDownloadState.Downloaded(1234), end)
        assertEquals(EpisodeDownloadState.Downloaded(1234), downloads.state("ep1"))
        assertEquals(1, storage.files.size)
        assertEquals(1234L, downloads.usedBytes)
    }

    @Test
    fun isDownloadingWhileTheFetchIsOpen() = runTest {
        val gate = CompletableDeferred<Unit>()
        network.gate = gate
        val job = launch(Dispatchers.Unconfined) { downloads.download(episodeDetail(), headers) }
        assertEquals(EpisodeDownloadState.Downloading, downloads.state("ep1"))
        // A second tap while downloading does not start a second fetch.
        downloads.download(episodeDetail(), headers)
        assertEquals(1, network.fetched.size)
        gate.complete(Unit)
        job.join()
        assertEquals(EpisodeDownloadState.Downloaded(1234), downloads.state("ep1"))
    }

    @Test
    fun failureRemovesThePartialFileAndCanBeRetried() = runTest {
        network.online = false
        network.leavePartialOnFailure = true
        val failed = downloads.download(episodeDetail(), headers)
        assertEquals(EpisodeDownloadState.Failed("offline"), failed)
        assertTrue(storage.files.isEmpty())
        assertNull(index.item("srv", "ep1"))

        network.online = true
        assertEquals(EpisodeDownloadState.Downloaded(1234), downloads.download(episodeDetail(), headers))
    }

    @Test
    fun alreadyDownloadedIsNotFetchedAgain() = runTest {
        downloads.download(episodeDetail(), headers)
        downloads.download(episodeDetail(), headers)
        assertEquals(1, network.fetched.size)
    }

    @Test
    fun deleteRemovesTheFileAndFreesItsStorage() = runTest {
        downloads.download(episodeDetail("ep1"), headers)
        downloads.download(episodeDetail("ep2"), headers)
        assertEquals(2468L, downloads.usedBytes)
        assertEquals(1234L, downloads.delete("ep1"))
        assertEquals(EpisodeDownloadState.NotDownloaded, downloads.state("ep1"))
        assertEquals(1, storage.files.size)
        assertFalse(storage.files.keys.any { it.contains("ep1") })
        assertEquals(1234L, downloads.usedBytes)
        assertEquals("deleting again frees nothing", 0L, downloads.delete("ep1"))
    }
}

class PodcastOfflinePlaybackTest {
    private val storage = FakeEpisodeStorage()
    private val network = FakeEpisodeNetwork(storage)
    private val downloads = PodcastDownloads("srv", storage, network, DownloadIndex())

    /** A player that reads the track from the URL it is given, as the real one does. */
    private class ReadingPlayer(val storage: FakeEpisodeStorage) : EpisodePlayer {
        var loaded: MediaDetail? = null
        var bytesRead: ByteArray? = null
        override suspend fun load(detail: MediaDetail) {
            loaded = detail
            bytesRead = storage.read(detail.tracks.single().url)
        }
    }

    @Test
    fun downloadedEpisodePlaysFromTheLocalFileWithTheNetworkOff() = runTest {
        downloads.download(episodeDetail(), emptyMap())
        network.online = false
        val fetchesBefore = network.fetched.size
        val player = ReadingPlayer(storage)
        assertTrue(downloads.playOffline("ep1", 250.0, player))
        assertTrue(player.loaded!!.tracks.single().url.startsWith("fake://downloads/"))
        assertEquals(network.body.toList(), player.bytesRead!!.toList())
        assertEquals(250.0, player.loaded!!.position!!.positionInTrack, 0.0)
        assertEquals("podcast:ep1", player.loaded!!.assetId)
        assertEquals("no request was made", fetchesBefore, network.fetched.size)
    }

    @Test
    fun notDownloadedOrDeletedEpisodeDoesNotPlayOffline() = runTest {
        val player = ReadingPlayer(storage)
        assertFalse(downloads.playOffline("ep1", 0.0, player))
        downloads.download(episodeDetail(), emptyMap())
        downloads.delete("ep1")
        assertFalse(downloads.playOffline("ep1", 0.0, player))
        assertNull(player.loaded)
    }
}

class PodcastResumeAfterReconnectTest {
    private val server = makeServer(ServerKind.bookdoe)
    private var online = false
    private val client = MockHttpClient { req ->
        if (!online) throw ApiError.Network("offline")
        jsonResponse(200, """{"applied": true, "progress": {"position_seconds": 480.0}}""")
    }
    private val provider = BookdoeProvider(server, client, InMemoryCredentialStore().also { it.save(Credential("t"), server.id) }, "1.2")

    @Test
    fun offlinePositionIsWrittenToTheProgressEndpointOnceBackOnline() = runTest {
        val queue = ProgressQueue()
        PodcastOfflineProgress.record(queue, server.id, "ep1", 300.0, 1000.0, at = 1_000)
        PodcastOfflineProgress.record(queue, server.id, "ep1", 480.0, 1000.0, at = 2_000)

        val whileOffline = queue.flush(server.id, provider)
        assertEquals(0, whileOffline.sent)
        assertEquals("kept for later", 1, queue.pending.size)

        online = true
        val back = queue.flush(server.id, provider)
        assertEquals(1, back.sent)
        assertTrue(queue.pending.isEmpty())
        val put = client.last!!
        assertEquals(HttpMethod.PUT, put.method)
        assertEquals("/api/player/v1/podcasts/episodes/ep1/progress", Urls.path(put.url))
        assertEquals(480.0, put.bodyJson?.dbl("position_seconds"))
        assertEquals(2.0, put.bodyJson?.dbl("client_updated_at"))
    }
}
