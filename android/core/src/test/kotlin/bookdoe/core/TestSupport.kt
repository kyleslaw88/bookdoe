package bookdoe.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.longOrNull

/** Records every request and answers from a queue or a routing function. */
class MockHttpClient(var router: ((HttpRequest) -> HttpResponse?)? = null) : HttpClient {
    private val lock = Any()
    private val _requests = ArrayList<HttpRequest>()
    private val queue = ArrayDeque<HttpResponse>()

    val requests: List<HttpRequest> get() = synchronized(lock) { _requests.toList() }

    fun enqueue(response: HttpResponse) = synchronized(lock) { queue.addLast(response) }

    fun enqueueJson(status: Int = 200, json: String) = enqueue(jsonResponse(status, json))

    override suspend fun send(request: HttpRequest): HttpResponse = synchronized(lock) {
        _requests.add(request)
        router?.invoke(request)?.let { return it }
        if (queue.isEmpty()) throw ApiError.Network("no queued response for ${request.method} ${request.path}")
        queue.removeFirst()
    }

    val last: HttpRequest? get() = requests.lastOrNull()
    fun request(matchingPath: String): HttpRequest? = requests.firstOrNull { it.path == matchingPath }
}

fun jsonResponse(status: Int = 200, json: String): HttpResponse =
    HttpResponse(status, mapOf("Content-Type" to "application/json"), json.trimIndent().encodeToByteArray())

fun makeServer(kind: ServerKind, base: String = "https://books.example.com", libraryId: String? = null, userId: String? = null): ServerRecord =
    ServerRecord(name = "Test", kind = kind, baseUrl = base, username = "ada", userId = userId, libraryId = libraryId, deviceId = "dev-1")

val HttpRequest.query: Map<String, String> get() = Urls.queryMap(url)

fun JsonObject.str(key: String): String? = (this[key] as? JsonPrimitive)?.contentOrNull
fun JsonObject.dbl(key: String): Double? = this[key]?.jsonPrimitive?.doubleOrNull
fun JsonObject.lng(key: String): Long? = this[key]?.jsonPrimitive?.longOrNull
fun JsonObject.bool(key: String): Boolean? = this[key]?.jsonPrimitive?.booleanOrNull
fun JsonObject.obj(key: String): JsonObject? = this[key] as? JsonObject
fun JsonObject.arr(key: String): List<JsonObject> = JsonHelpers.array(this, key)

fun parse(json: String): JsonElement = Json.parseToJsonElement(json.trimIndent())
