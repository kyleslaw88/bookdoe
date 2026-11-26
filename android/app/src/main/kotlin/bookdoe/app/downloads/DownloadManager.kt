package bookdoe.app.downloads

import bookdoe.core.DownloadIndex
import bookdoe.core.DownloadPlan
import bookdoe.core.DownloadedItem
import bookdoe.core.MediaDetail
import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.workDataOf
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.io.File

/**
 * Enqueues [DownloadWorker] jobs and keeps the [DownloadIndex]. Files land under
 * `root/<serverId>/<item>/...`; the index maps items to them and yields the same
 * [MediaDetail] the online path uses.
 */
class DownloadManager(context: Context, val root: File, val index: DownloadIndex, scope: CoroutineScope) {
    data class Progress(
        val itemId: String,
        val serverId: String,
        val completedFiles: Int,
        val totalFiles: Int,
        val bytes: Long,
        val error: String? = null,
        val title: String? = null,
    ) {
        val fraction: Double get() = if (totalFiles == 0) 0.0 else completedFiles.toDouble() / totalFiles
        val key: String get() = DownloadJob.key(serverId, itemId)
    }

    private val appContext = context.applicationContext
    private val workManager = WorkManager.getInstance(appContext)
    private val jobsDir = File(root, "jobs").apply { mkdirs() }

    private val _active = MutableStateFlow<Map<String, Progress>>(emptyMap())
    val active: StateFlow<Map<String, Progress>> = _active.asStateFlow()
    private val _completed = MutableStateFlow(index.items)
    val completed: StateFlow<List<DownloadedItem>> = _completed.asStateFlow()
    private val titles = HashMap<String, String>()

    init {
        scope.launch {
            workManager.getWorkInfosByTagFlow(DownloadWorker.TAG).collect { infos -> apply(infos) }
        }
    }

    private fun apply(infos: List<WorkInfo>) {
        val map = LinkedHashMap<String, Progress>()
        var refresh = false
        for (info in infos) {
            val data = if (info.state == WorkInfo.State.RUNNING) info.progress else info.outputData
            val serverId = data.getString(DownloadWorker.KEY_SERVER_ID) ?: info.tags.firstOrNull { it.startsWith("key:") }?.removePrefix("key:")?.substringBefore(':') ?: continue
            val itemId = data.getString(DownloadWorker.KEY_ITEM_ID) ?: info.tags.firstOrNull { it.startsWith("key:") }?.removePrefix("key:")?.substringAfter(':') ?: continue
            val key = DownloadJob.key(serverId, itemId)
            when (info.state) {
                WorkInfo.State.ENQUEUED, WorkInfo.State.RUNNING, WorkInfo.State.BLOCKED ->
                    map[key] = Progress(itemId, serverId, data.getInt(DownloadWorker.KEY_COMPLETED, 0), data.getInt(DownloadWorker.KEY_TOTAL, 0), data.getLong(DownloadWorker.KEY_BYTES, 0), title = titles[key])
                WorkInfo.State.FAILED ->
                    map[key] = Progress(itemId, serverId, 0, 0, 0, error = info.outputData.getString(DownloadWorker.KEY_ERROR) ?: "Download failed", title = titles[key])
                WorkInfo.State.SUCCEEDED -> refresh = true
                WorkInfo.State.CANCELLED -> {}
            }
        }
        _active.value = map
        if (refresh) _completed.value = index.items
    }

    fun isDownloaded(serverId: String, itemId: String): Boolean = index.item(serverId, itemId) != null

    fun offlineDetail(serverId: String, itemId: String): MediaDetail? = index.item(serverId, itemId)?.detail(root)

    fun localEbook(serverId: String, itemId: String): File? =
        index.item(serverId, itemId)?.ebookRelativePath?.let { File(root, it) }?.takeIf { it.exists() }

    fun download(detail: MediaDetail, serverId: String, headers: Map<String, String>) {
        val job = DownloadJob.from(serverId, detail, headers)
        if (job.files.isEmpty()) return
        val key = job.key
        titles[key] = detail.item.title
        val jobFile = File(jobsDir, DownloadPlan.safe(key) + ".json")
        jobFile.writeText(DownloadJob.encode(job))
        val request = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setInputData(workDataOf(DownloadWorker.KEY_JOB_FILE to jobFile.path))
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .addTag(DownloadWorker.TAG)
            .addTag("key:$key")
            .build()
        workManager.enqueueUniqueWork("download:$key", ExistingWorkPolicy.KEEP, request)
        _active.value = _active.value + (key to Progress(detail.item.id, serverId, 0, job.files.size, 0, title = detail.item.title))
    }

    fun cancel(serverId: String, itemId: String) {
        workManager.cancelUniqueWork("download:${DownloadJob.key(serverId, itemId)}")
        _active.value = _active.value - DownloadJob.key(serverId, itemId)
    }

    fun remove(serverId: String, itemId: String) {
        cancel(serverId, itemId)
        index.remove(serverId, itemId)?.let { item ->
            for (t in item.tracks) File(root, t.relativePath).delete()
            item.ebookRelativePath?.let { File(root, it).delete() }
        }
        File(root, "$serverId/${DownloadPlan.safe(itemId)}").deleteRecursively()
        _completed.value = index.items
    }

    fun removeAll(serverId: String) {
        for (item in index.items.filter { it.serverId == serverId }) remove(serverId, item.itemId)
        File(root, serverId).deleteRecursively()
        _completed.value = index.items
    }

    val totalBytes: Long get() = index.items.sumOf { it.bytes }
}
