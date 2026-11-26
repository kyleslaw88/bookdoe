package bookdoe.core

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/** The real OkHttp path end to end against a fake server: headers, bodies, errors. */
class OkHttpClientTest {
    private val server = MockWebServer()

    @Before fun start() = server.start()
    @After fun stop() = server.shutdown()

    @Test
    fun bookdoeBrowseOverRealHttp() = runTest {
        server.enqueue(MockResponse().setBody("""{"items": [{"id": "w1", "title": "Dune", "authors": [], "narrators": [], "series": [], "assets": [{"id": "a", "kind": "audiobook"}]}], "total": 1}""").setHeader("Content-Type", "application/json"))
        val creds = InMemoryCredentialStore()
        val record = makeServer(ServerKind.bookdoe, base = server.url("/").toString().trimEnd('/'))
        creds.save(Credential("alxd_tok"), record.id)
        val provider = BookdoeProvider(record, OkHttpHttpClient(userAgent = "bookdoe-Android/test"), creds, "1.0")
        val page = provider.browse("dune", null, LibrarySort.added, 0, 20)
        assertEquals("Dune", page.items.first().title)
        val req = server.takeRequest()
        assertEquals("GET", req.method)
        assertEquals("/api/player/v1/library", req.requestUrl?.encodedPath)
        assertEquals("dune", req.requestUrl?.queryParameter("q"))
        assertEquals("Bearer alxd_tok", req.getHeader("Authorization"))
        assertEquals("bookdoe-Android/test", req.getHeader("User-Agent"))
    }

    @Test
    fun jsonBodiesAndStatusMapping() = runTest {
        server.enqueue(MockResponse().setResponseCode(403).setBody("""{"detail": "nope"}"""))
        val creds = InMemoryCredentialStore()
        val record = makeServer(ServerKind.audiobookshelf, base = server.url("/").toString().trimEnd('/'))
        val provider = AudiobookshelfProvider(record, OkHttpHttpClient(), creds, "1.0")
        try { provider.login("ada", "pw", "Pixel"); fail() } catch (e: ApiError) { assertEquals(ApiError.Forbidden("nope"), e) }
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/login", req.path)
        assertTrue(req.getHeader("Content-Type")!!.startsWith("application/json"))
        assertEquals("pw", (parse(req.body.readUtf8()) as kotlinx.serialization.json.JsonObject).str("password"))
    }

    @Test
    fun connectionFailureIsNetworkError() = runTest {
        val port = server.port
        server.shutdown()
        val client = OkHttpHttpClient()
        try {
            client.send(HttpRequest(url = "http://localhost:$port/status", timeoutSeconds = 5))
            fail()
        } catch (e: ApiError) {
            assertTrue(e is ApiError.Network)
        }
    }
}
