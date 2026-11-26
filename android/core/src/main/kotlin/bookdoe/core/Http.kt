package bookdoe.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

enum class HttpMethod { GET, POST, PUT, PATCH, DELETE }

data class HttpRequest(
    val method: HttpMethod = HttpMethod.GET,
    val url: String,
    val headers: Map<String, String> = emptyMap(),
    val body: ByteArray? = null,
    val timeoutSeconds: Long = 30,
) {
    /** The body parsed as a JSON object, for tests and logging. */
    val bodyJson: JsonObject?
        get() = body?.let { runCatching { Json.parseToJsonElement(it.decodeToString()) as? JsonObject }.getOrNull() }

    fun withHeader(name: String, value: String): HttpRequest = copy(headers = headers + (name to value))

    /** Path part of the URL, for assertions. */
    val path: String get() = Urls.path(url)

    override fun equals(other: Any?): Boolean =
        other is HttpRequest && method == other.method && url == other.url && headers == other.headers &&
            (body?.contentEquals(other.body) ?: (other.body == null)) && timeoutSeconds == other.timeoutSeconds

    override fun hashCode(): Int = 31 * url.hashCode() + method.hashCode()

    companion object {
        /** JSON body convenience; sets Content-Type. */
        fun json(method: HttpMethod, url: String, body: JsonElement, headers: Map<String, String> = emptyMap()): HttpRequest =
            HttpRequest(method, url, headers + ("Content-Type" to "application/json"), Json.encodeToString(JsonElement.serializer(), body).encodeToByteArray())
    }
}

data class HttpResponse(
    val status: Int,
    val headers: Map<String, String> = emptyMap(),
    val body: ByteArray = ByteArray(0),
) {
    val isSuccess: Boolean get() = status in 200..299

    fun json(): JsonElement = try {
        Json.parseToJsonElement(body.decodeToString())
    } catch (e: Exception) {
        throw ApiError.Decoding("not JSON")
    }

    fun jsonObject(): JsonObject = json() as? JsonObject ?: throw ApiError.Decoding("expected a JSON object")

    fun jsonArray(): JsonArray = json() as? JsonArray ?: throw ApiError.Decoding("expected a JSON array")

    fun header(name: String): String? = headers.entries.firstOrNull { it.key.equals(name, ignoreCase = true) }?.value

    override fun equals(other: Any?): Boolean =
        other is HttpResponse && status == other.status && headers == other.headers && body.contentEquals(other.body)

    override fun hashCode(): Int = status
}

/** Every failure a provider can raise; data classes so tests compare them. */
sealed class ApiError(override val message: String?) : Exception(message) {
    data object Unauthorized : ApiError("Sign in again: the server rejected the credential.") {
        private fun readResolve(): Any = Unauthorized
    }
    data class Forbidden(val detail: String?) : ApiError(detail ?: "The server refused this request.")
    data object NotFound : ApiError("Not found on the server.") {
        private fun readResolve(): Any = NotFound
    }
    data class RateLimited(val retryAfter: Int?) : ApiError("Too many requests; try again in ${retryAfter ?: 60} seconds.")
    data class Server(val status: Int, val detail: String?) : ApiError(detail ?: "Server error (HTTP $status).")
    data class Decoding(val detail: String) : ApiError("Unexpected answer from the server ($detail).")
    data class Network(val detail: String) : ApiError("Network problem: $detail")
    data class NotSupported(val detail: String) : ApiError(detail)
    data object SetupRequired : ApiError("This server has not finished its first-run setup. Open it in a browser first.") {
        private fun readResolve(): Any = SetupRequired
    }

    companion object {
        /** Map a non-2xx response to an error; `detail` is the bookdoe/FastAPI shape. */
        fun from(response: HttpResponse): ApiError {
            val obj = runCatching { response.jsonObject() }.getOrNull()
            val message = obj?.let { JsonHelpers.string(it, "detail") ?: JsonHelpers.string(it, "message") ?: JsonHelpers.string(it, "error") }
            return when (response.status) {
                401 -> Unauthorized
                403 -> Forbidden(message)
                404 -> NotFound
                429 -> RateLimited(response.header("Retry-After")?.toIntOrNull())
                else -> Server(response.status, message)
            }
        }
    }
}

/** The one seam every provider talks through, so tests swap in a recorder. */
interface HttpClient {
    suspend fun send(request: HttpRequest): HttpResponse
}

/** OkHttp-backed client used by the app and by the MockWebServer tests. */
class OkHttpHttpClient(
    private val client: OkHttpClient = OkHttpClient(),
    private val userAgent: String = "bookdoe-Android",
) : HttpClient {
    override suspend fun send(request: HttpRequest): HttpResponse = withContext(Dispatchers.IO) {
        val builder = Request.Builder().url(request.url).header("User-Agent", userAgent)
        for ((k, v) in request.headers) builder.header(k, v)
        val contentType = request.headers.entries.firstOrNull { it.key.equals("Content-Type", true) }?.value ?: "application/octet-stream"
        val body = request.body?.toRequestBody(contentType.toMediaType())
        when (request.method) {
            HttpMethod.GET -> builder.get()
            HttpMethod.POST -> builder.post(body ?: ByteArray(0).toRequestBody(null))
            HttpMethod.PUT -> builder.put(body ?: ByteArray(0).toRequestBody(null))
            HttpMethod.PATCH -> builder.patch(body ?: ByteArray(0).toRequestBody(null))
            HttpMethod.DELETE -> if (body != null) builder.delete(body) else builder.delete()
        }
        val timed = client.newBuilder()
            .callTimeout(request.timeoutSeconds, TimeUnit.SECONDS)
            .build()
        try {
            timed.newCall(builder.build()).execute().use { resp ->
                val headers = HashMap<String, String>()
                for (name in resp.headers.names()) headers[name] = resp.headers[name] ?: ""
                HttpResponse(resp.code, headers, resp.body?.bytes() ?: ByteArray(0))
            }
        } catch (e: IOException) {
            throw ApiError.Network(e.message ?: e.javaClass.simpleName)
        }
    }
}
