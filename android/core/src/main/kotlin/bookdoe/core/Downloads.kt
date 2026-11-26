package bookdoe.core

import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import java.io.File

/**
 * What to fetch for offline use and where to put it. The app's download worker
 * executes the plan; this type keeps the naming and bookkeeping testable without one.
 */
data class DownloadPlan(val serverId: String, val itemId: String, val files: List<PlanFile>) {
    @Serializable
    data class PlanFile(
        val trackId: String,
        val remote: String,
        val localRelativePath: String,
        val headers: Map<String, String>,
        val expectedBytes: Long? = null,
    )

    val totalExpectedBytes: Long get() = files.sumOf { it.expectedBytes ?: 0L }

    companion object {
        fun from(serverId: String, detail: MediaDetail, headers: Map<String, String>): DownloadPlan {
            val files = ArrayList<PlanFile>()
            for (t in detail.tracks) {
                val ext = extensionFor(t.url, t.mimeType)
                files.add(PlanFile(t.id, t.url, "$serverId/${safe(detail.item.id)}/${"%03d".format(t.index)}-${safe(t.id)}.$ext", headers, t.sizeBytes))
            }
            detail.ebookDownloadUrl?.let { e ->
                files.add(PlanFile("ebook", e, "$serverId/${safe(detail.item.id)}/book.${detail.ebookFormat ?: extensionFor(e, null)}", headers, null))
            }
            return DownloadPlan(serverId, detail.item.id, files)
        }

        private val allowed = ('a'..'z').toSet() + ('A'..'Z') + ('0'..'9') + setOf('-', '_')

        fun safe(s: String): String {
            val out = s.map { if (it in allowed) it else '_' }.joinToString("")
            return if (out.isEmpty()) "item" else out.take(80)
        }

        fun extensionFor(url: String, mime: String?): String {
            val name = Urls.lastPathComponent(url)
            val ext = name.substringAfterLast('.', "").lowercase()
            if (ext.isNotEmpty() && ext.length <= 5 && ext.all { it.isLetterOrDigit() }) return ext
            return when (mime?.lowercase()) {
                "audio/mp4", "audio/x-m4b", "audio/m4b" -> "m4b"
                "audio/mpeg", "audio/mp3" -> "mp3"
                "audio/flac" -> "flac"
                "audio/ogg" -> "ogg"
                "audio/aac" -> "aac"
                else -> "bin"
            }
        }
    }
}

@Serializable
data class DownloadedTrack(val trackId: String, val title: String, val relativePath: String, val durationSeconds: Double?)

/** Index entry for one completed download. */
@Serializable
data class DownloadedItem(
    val serverId: String,
    val itemId: String,
    val title: String,
    val authors: List<String>,
    val kind: MediaKind,
    val assetId: String,
    /** In track order; relative paths under the downloads root. */
    val tracks: List<DownloadedTrack>,
    val chapters: List<Chapter>,
    val durationSeconds: Double?,
    val bytes: Long,
    val completedAt: Long = System.currentTimeMillis(),
    val coverUrl: String? = null,
    val ebookRelativePath: String? = null,
    val ebookFormat: String? = null,
) {
    val id: String get() = "$serverId:$itemId"

    /**
     * A detail whose track URLs point at the local files, so the player code path
     * is the same online and offline.
     */
    fun detail(root: File): MediaDetail {
        var offset = 0.0
        val ts = tracks.mapIndexed { i, t ->
            val tr = Track(id = t.trackId, index = i, title = t.title, url = File(root, t.relativePath).toURI().toString(), durationSeconds = t.durationSeconds, startOffset = offset)
            offset += t.durationSeconds ?: 0.0
            tr
        }
        val item = MediaItem(id = itemId, title = title, authors = authors, kind = kind, durationSeconds = durationSeconds, coverUrl = coverUrl)
        return MediaDetail(
            item = item, tracks = ts, chapters = chapters, assetId = assetId,
            ebookDownloadUrl = ebookRelativePath?.let { File(root, it).toURI().toString() }, ebookFormat = ebookFormat,
        )
    }

    companion object {
        fun from(serverId: String, detail: MediaDetail, tracks: List<DownloadedTrack>, bytes: Long, completedAt: Long = System.currentTimeMillis(), ebookRelativePath: String? = null): DownloadedItem =
            DownloadedItem(
                serverId = serverId, itemId = detail.item.id, title = detail.item.title, authors = detail.item.authors, kind = detail.item.kind,
                assetId = detail.assetId, tracks = tracks, chapters = detail.chapters, durationSeconds = detail.item.durationSeconds, bytes = bytes,
                completedAt = completedAt, coverUrl = detail.item.coverUrl, ebookRelativePath = ebookRelativePath, ebookFormat = detail.ebookFormat,
            )
    }
}

/** Index of completed downloads, persisted as JSON. */
class DownloadIndex(private val file: File? = null) {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val serializer = ListSerializer(DownloadedItem.serializer())

    @Volatile
    var items: List<DownloadedItem> = file?.takeIf { it.exists() }?.let { f ->
        runCatching { json.decodeFromString(serializer, f.readText()) }.getOrNull()
    } ?: emptyList()
        private set

    @Synchronized
    fun item(serverId: String, itemId: String): DownloadedItem? = items.firstOrNull { it.serverId == serverId && it.itemId == itemId }

    @Synchronized
    fun upsert(item: DownloadedItem) {
        items = items.filter { it.id != item.id } + item
        persist()
    }

    @Synchronized
    fun remove(serverId: String, itemId: String): DownloadedItem? {
        val removed = items.firstOrNull { it.serverId == serverId && it.itemId == itemId } ?: return null
        items = items.filter { it.id != removed.id }
        persist()
        return removed
    }

    private fun persist() {
        val f = file ?: return
        runCatching {
            f.parentFile?.mkdirs()
            f.writeText(json.encodeToString(serializer, items))
        }
    }
}
