package bookdoe.core

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.UUID

/** A provider stub that records progress reports and answers from a script. */
class StubProvider(override val server: ServerRecord) : MediaProvider {
    override val kind: ServerKind get() = server.kind
    val reports = ArrayList<ProgressUpdate>()
    val script = ArrayDeque<(ProgressUpdate) -> ProgressReport>()

    override suspend fun probe() = ServerInfo("stub", supportsPasswordLogin = true)
    override suspend fun login(username: String, password: String, deviceName: String): AuthResult = throw ApiError.NotSupported("stub")
    override suspend fun libraries(): List<LibraryRef> = emptyList()
    override suspend fun browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int) = LibraryPage(emptyList(), 0, 0, limit)
    override suspend fun continueListening(): List<MediaItem> = emptyList()
    override suspend fun detail(id: String): MediaDetail = throw ApiError.NotFound
    override suspend fun startPlayback(detail: MediaDetail) = PlaybackSession(null)
    override suspend fun reportProgress(update: ProgressUpdate, session: PlaybackSession?): ProgressReport {
        reports.add(update)
        val step = script.removeFirstOrNull() ?: return ProgressReport(applied = true)
        return step(update)
    }
    override suspend fun stopPlayback(update: ProgressUpdate, session: PlaybackSession?) {}
    override fun mediaHeaders(): Map<String, String> = emptyMap()
    override suspend fun logout() {}
}

class ProgressQueueTest {
    private val server = makeServer(ServerKind.bookdoe)

    private fun update(asset: String, at: Long, listened: Double = 0.0) =
        ProgressUpdate(serverId = server.id, itemId = "w", assetId = asset, absolutePosition = at.toDouble(), clientUpdatedAt = at * 1000, listened = listened)

    @Test
    fun enqueueCoalescesPerAssetAndSumsListened() = runTest {
        val q = ProgressQueue()
        q.enqueue(update("a", 1, listened = 10.0))
        q.enqueue(update("a", 2, listened = 5.0))
        q.enqueue(update("b", 3))
        assertEquals(2, q.pending.size)
        assertEquals(15.0, q.pending.first { it.assetId == "a" }.listened, 0.0)
        assertEquals(2.0, q.pending.first { it.assetId == "a" }.absolutePosition, 0.0)
    }

    @Test
    fun flushSendsInOrderAndReportsConflicts() = runTest {
        val q = ProgressQueue()
        val p = StubProvider(server)
        q.enqueue(update("b", 5))
        q.enqueue(update("a", 1))
        p.script.addLast { ProgressReport(applied = false, serverPosition = PlaybackPosition(absolutePosition = 99.0)) }
        p.script.addLast { ProgressReport(applied = true) }
        val result = q.flush(server.id, p)
        assertEquals("oldest client timestamp first", listOf("a", "b"), p.reports.map { it.assetId })
        assertEquals(2, result.sent)
        assertEquals(1, result.conflicts.size)
        assertEquals(99.0, result.conflicts.first().second.absolutePosition, 0.0)
        assertTrue(q.pending.isEmpty())
    }

    @Test
    fun networkFailureKeepsItemsAndStopsTheBatch() = runTest {
        val q = ProgressQueue()
        val p = StubProvider(server)
        q.enqueue(update("a", 1))
        q.enqueue(update("b", 2))
        p.script.addLast { throw ApiError.Network("offline") }
        val result = q.flush(server.id, p)
        assertEquals(0, result.sent)
        assertEquals(1, result.failed)
        assertEquals(1, p.reports.size)
        assertEquals(2, q.pending.size)
    }

    @Test
    fun notFoundDropsTheItem() = runTest {
        val q = ProgressQueue()
        val p = StubProvider(server)
        q.enqueue(update("gone", 1))
        p.script.addLast { throw ApiError.NotFound }
        val result = q.flush(server.id, p)
        assertEquals(1, result.failed)
        assertTrue(q.pending.isEmpty())
    }

    @Test
    fun persistenceRoundTrip() = runTest {
        val file = File(System.getProperty("java.io.tmpdir"), "pq-${UUID.randomUUID()}.json")
        try {
            val q = ProgressQueue(file)
            q.enqueue(update("a", 1))
            val reloaded = ProgressQueue(file)
            assertEquals(listOf("a"), reloaded.pending.map { it.assetId })
        } finally {
            file.delete()
        }
    }

    @Test
    fun otherServersAreUntouched() = runTest {
        val q = ProgressQueue()
        val other = makeServer(ServerKind.jellyfin)
        q.enqueue(update("a", 1))
        q.enqueue(ProgressUpdate(serverId = other.id, itemId = "x", assetId = "x"))
        q.flush(server.id, StubProvider(server))
        assertEquals(listOf(other.id), q.pending.map { it.serverId })
    }
}
