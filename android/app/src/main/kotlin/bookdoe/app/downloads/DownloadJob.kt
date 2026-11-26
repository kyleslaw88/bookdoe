package bookdoe.app.downloads

import bookdoe.core.Chapter
import bookdoe.core.DownloadPlan
import bookdoe.core.MediaDetail
import bookdoe.core.MediaItem
import bookdoe.core.Track
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * Everything the worker needs, written to a small JSON file (WorkManager input data
 * is capped at 10 KB) and read back when the job runs. Headers travel with it only
 * inside app-private storage and the file is deleted when the job finishes.
 */
@Serializable
data class DownloadJob(
    val serverId: String,
    val item: MediaItem,
    val tracks: List<Track>,
    val chapters: List<Chapter>,
    val assetId: String,
    val ebookDownloadUrl: String?,
    val ebookFormat: String?,
    val files: List<DownloadPlan.PlanFile>,
) {
    val key: String get() = key(serverId, item.id)

    fun detail(): MediaDetail = MediaDetail(
        item = item, tracks = tracks, chapters = chapters, assetId = assetId,
        ebookDownloadUrl = ebookDownloadUrl, ebookFormat = ebookFormat,
    )

    companion object {
        private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

        fun from(serverId: String, detail: MediaDetail, headers: Map<String, String>): DownloadJob {
            val plan = DownloadPlan.from(serverId, detail, headers)
            return DownloadJob(serverId, detail.item, detail.tracks, detail.chapters, detail.assetId, detail.ebookDownloadUrl, detail.ebookFormat, plan.files)
        }

        fun key(serverId: String, itemId: String) = "$serverId:$itemId"

        fun encode(job: DownloadJob): String = json.encodeToString(serializer(), job)
        fun decode(text: String): DownloadJob = json.decodeFromString(serializer(), text)
    }
}
