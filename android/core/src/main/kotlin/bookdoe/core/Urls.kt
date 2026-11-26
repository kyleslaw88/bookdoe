package bookdoe.core

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** URL helpers mirroring the iOS `URL.appending(path:query:)` and `resolving`. */
object Urls {
    fun isValid(url: String): Boolean = url.toHttpUrlOrNull() != null

    /** Append path components and query items to a base URL without double slashes. */
    fun appending(base: String, path: String, query: Map<String, String?> = emptyMap()): String {
        val b: HttpUrl = base.toHttpUrlOrNull() ?: return base
        val builder = b.newBuilder()
        if (b.pathSegments.isNotEmpty() && b.pathSegments.last().isEmpty()) builder.removePathSegment(b.pathSegments.size - 1)
        for (seg in path.split('/')) if (seg.isNotEmpty()) builder.addPathSegment(seg)
        for ((k, v) in query.entries.sortedBy { it.key }) if (v != null) builder.addQueryParameter(k, v)
        return builder.build().toString()
    }

    /** Resolve a server-relative URL ("/public/session/...") against the base. */
    fun resolving(base: String, relative: String): String? {
        if (relative.startsWith("http://") || relative.startsWith("https://")) return relative
        return appending(base, relative)
    }

    fun path(url: String): String = url.toHttpUrlOrNull()?.encodedPath ?: url

    fun queryMap(url: String): Map<String, String> {
        val u = url.toHttpUrlOrNull() ?: return emptyMap()
        val out = LinkedHashMap<String, String>()
        for (name in u.queryParameterNames) out[name] = u.queryParameter(name) ?: ""
        return out
    }

    fun hostLabel(url: String): String {
        val u = url.toHttpUrlOrNull() ?: return url
        val defaultPort = (u.scheme == "https" && u.port == 443) || (u.scheme == "http" && u.port == 80)
        return if (defaultPort) u.host else "${u.host}:${u.port}"
    }

    /** Last path component without query, for file naming. */
    fun lastPathComponent(url: String): String {
        val p = path(url).trimEnd('/')
        return p.substringAfterLast('/')
    }

    fun isFileUrl(url: String): Boolean = url.startsWith("file:") || url.startsWith("/")
}
