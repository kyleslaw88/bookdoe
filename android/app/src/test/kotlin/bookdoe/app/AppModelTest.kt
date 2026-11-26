package bookdoe.app

import bookdoe.core.ApiError
import bookdoe.core.Credential
import bookdoe.core.HttpClient
import bookdoe.core.HttpRequest
import bookdoe.core.HttpResponse
import bookdoe.core.InMemoryCredentialStore
import bookdoe.core.PlaybackPosition
import bookdoe.core.ProgressQueue
import bookdoe.core.ProgressUpdate
import bookdoe.core.ServerKind
import bookdoe.core.ServerRecord
import bookdoe.core.ServerStore
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Plain-JVM tests for the app model: no Android classes involved. */
class AppModelTest {
    private class ScriptedClient : HttpClient {
        val queue = ArrayDeque<HttpResponse>()
        val requests = ArrayList<HttpRequest>()
        fun json(status: Int, body: String) = queue.addLast(HttpResponse(status, mapOf("Content-Type" to "application/json"), body.encodeToByteArray()))
        override suspend fun send(request: HttpRequest): HttpResponse {
            requests.add(request)
            return queue.removeFirstOrNull() ?: throw ApiError.Network("no response for ${request.path}")
        }
    }

    private fun model(client: ScriptedClient, scope: kotlinx.coroutines.CoroutineScope, store: ServerStore = ServerStore(), creds: InMemoryCredentialStore = InMemoryCredentialStore(), queue: ProgressQueue = ProgressQueue()) =
        AppModel(store, creds, client, queue, "1.0", "Test device", scope)

    @Test
    fun addServerStoresRecordAndCredentialSeparately() = runTest {
        val client = ScriptedClient()
        val creds = InMemoryCredentialStore()
        val store = ServerStore()
        val m = model(client, backgroundScope, store, creds)
        client.json(200, """{"server": "bookdoe", "version": "0.9.1", "auth": {"password_login": true, "device_pairing": true}}""")
        client.json(200, """{"token": "alxd_t", "user": {"id": "u1", "username": "ada"}}""")
        val record = m.addServer(ServerKind.bookdoe, "", "http://lan-box:8795", "ada", "pw")
        assertEquals("bookdoe", record.name)
        assertEquals("u1", record.userId)
        assertEquals("alxd_t", creds.credential(record.id)?.accessToken)
        assertEquals(record.id, store.activeServerId)
        assertNotNull(m.provider)
        assertEquals(ServerKind.bookdoe, m.provider?.kind)
        assertEquals("Test device", client.requests.last().bodyJson?.get("device_name")?.toString()?.trim('"'))
    }

    @Test
    fun removeServerClearsEverything() = runTest {
        val client = ScriptedClient()
        val creds = InMemoryCredentialStore()
        val queue = ProgressQueue()
        val server = ServerRecord(name = "JF", kind = ServerKind.jellyfin, baseUrl = "https://jf.example.com", username = "k", userId = "u")
        val store = ServerStore(listOf(server))
        creds.save(Credential("tok"), server.id)
        queue.enqueue(ProgressUpdate(serverId = server.id, itemId = "i", assetId = "a"))
        val m = model(client, backgroundScope, store, creds, queue)
        var removedCallback: String? = null
        m.onServerRemoved = { removedCallback = it }
        client.queue.addLast(HttpResponse(204))
        m.removeServer(server)
        assertEquals(server.id, removedCallback)
        assertNull(creds.credential(server.id))
        assertTrue(queue.pending.isEmpty())
        assertTrue(store.servers.isEmpty())
        assertNull(store.active)
        assertNull(m.provider)
        assertEquals("/Sessions/Logout", client.requests.last().path)
    }

    @Test
    fun flushSurfacesBookdoeConflictToThePlayer() = runTest {
        val client = ScriptedClient()
        val creds = InMemoryCredentialStore()
        val queue = ProgressQueue()
        val server = ServerRecord(name = "A", kind = ServerKind.bookdoe, baseUrl = "https://a.example.com", username = "k")
        val store = ServerStore(listOf(server))
        creds.save(Credential("tok"), server.id)
        val m = model(client, backgroundScope, store, creds, queue)
        var conflict: Pair<ProgressUpdate, PlaybackPosition>? = null
        m.onConflict = { u, p -> conflict = u to p }
        client.json(200, """{"items": [{"asset_id": "as1", "applied": false, "file_id": "f1", "position": 42}]}""")
        m.enqueueAndFlush(ProgressUpdate(serverId = server.id, itemId = "w", assetId = "as1", trackId = "f2", positionInTrack = 5.0), null)
        assertNotNull(conflict)
        assertEquals(42.0, conflict!!.second.positionInTrack, 0.0)
        assertTrue(queue.pending.isEmpty())
    }

    @Test
    fun offlineFlushKeepsTheUpdateQueued() = runTest {
        val client = ScriptedClient()
        val creds = InMemoryCredentialStore()
        val queue = ProgressQueue()
        val server = ServerRecord(name = "A", kind = ServerKind.bookdoe, baseUrl = "https://a.example.com", username = "k")
        creds.save(Credential("tok"), server.id)
        val m = model(client, backgroundScope, ServerStore(listOf(server)), creds, queue)
        val r = m.flushProgressAfter(ProgressUpdate(serverId = server.id, itemId = "w", assetId = "as1"))
        assertEquals(1, r?.failed)
        assertEquals(1, queue.pending.size)
        assertFalse(m.lastError.value != null)
    }

    private suspend fun AppModel.flushProgressAfter(update: ProgressUpdate): ProgressQueue.FlushResult? {
        progressQueue.enqueue(update)
        return flushProgress(null)
    }
}
