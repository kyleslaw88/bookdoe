package bookdoe.app.downloads

import bookdoe.app.BookdoeApplication
import bookdoe.app.R
import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import bookdoe.core.DownloadedItem
import bookdoe.core.DownloadedTrack
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.io.File
import java.io.IOException

/**
 * Fetches every file of a [DownloadJob] into the app-private downloads root, then
 * records the item in the [bookdoe.core.DownloadIndex]. Files already present
 * are skipped so a retry resumes where it stopped.
 */
class DownloadWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val jobPath = inputData.getString(KEY_JOB_FILE) ?: return@withContext Result.failure(workDataOf(KEY_ERROR to "no job"))
        val jobFile = File(jobPath)
        val job = runCatching { DownloadJob.decode(jobFile.readText()) }.getOrElse { return@withContext Result.failure(workDataOf(KEY_ERROR to "unreadable job")) }
        val container = BookdoeApplication.container(applicationContext)
        val root = container.downloadsRoot
        runCatching { setForeground(foregroundInfo(job.item.title)) }

        var bytes = 0L
        var completed = 0
        val total = job.files.size
        val finishedTracks = ArrayList<DownloadedTrack>()
        var ebookPath: String? = null
        fun progress() = workDataOf(
            KEY_SERVER_ID to job.serverId, KEY_ITEM_ID to job.item.id, KEY_COMPLETED to completed, KEY_TOTAL to total, KEY_BYTES to bytes,
        )
        setProgress(progress())

        for (file in job.files) {
            if (isStopped) return@withContext Result.failure(workDataOf(KEY_ERROR to "cancelled"))
            val dest = File(root, file.localRelativePath)
            try {
                if (!dest.exists() || dest.length() == 0L) fetch(container, file.remote, file.headers, dest)
            } catch (e: IOException) {
                return@withContext if (runAttemptCount < 3) Result.retry() else Result.failure(workDataOf(KEY_ERROR to (e.message ?: "download failed"), KEY_SERVER_ID to job.serverId, KEY_ITEM_ID to job.item.id))
            } catch (e: HttpStatusException) {
                return@withContext Result.failure(workDataOf(KEY_ERROR to "Server answered HTTP ${e.status}", KEY_SERVER_ID to job.serverId, KEY_ITEM_ID to job.item.id))
            }
            bytes += dest.length()
            if (file.trackId == "ebook") {
                ebookPath = file.localRelativePath
            } else {
                job.tracks.firstOrNull { it.id == file.trackId }?.let { t ->
                    finishedTracks.add(DownloadedTrack(t.id, t.title, file.localRelativePath, t.durationSeconds))
                }
            }
            completed += 1
            setProgress(progress())
        }
        val ordered = finishedTracks.sortedBy { ft -> job.tracks.firstOrNull { it.id == ft.trackId }?.index ?: 0 }
        container.downloadIndex.upsert(DownloadedItem.from(job.serverId, job.detail(), ordered, bytes, ebookRelativePath = ebookPath))
        jobFile.delete()
        Result.success(workDataOf(KEY_SERVER_ID to job.serverId, KEY_ITEM_ID to job.item.id, KEY_BYTES to bytes))
    }

    private class HttpStatusException(val status: Int) : Exception("HTTP $status")

    private fun fetch(container: bookdoe.app.AppContainer, url: String, headers: Map<String, String>, dest: File) {
        val builder = Request.Builder().url(url)
        for ((k, v) in headers) builder.header(k, v)
        container.okHttp.newCall(builder.build()).execute().use { resp ->
            if (!resp.isSuccessful) throw HttpStatusException(resp.code)
            dest.parentFile?.mkdirs()
            val tmp = File(dest.path + ".part")
            resp.body?.byteStream()?.use { input -> tmp.outputStream().use { out -> input.copyTo(out, 256 * 1024) } }
                ?: throw IOException("empty body")
            if (!tmp.renameTo(dest)) {
                tmp.copyTo(dest, overwrite = true)
                tmp.delete()
            }
        }
    }

    override suspend fun getForegroundInfo(): ForegroundInfo = foregroundInfo("Downloading")

    private fun foregroundInfo(title: String): ForegroundInfo {
        val notification = NotificationCompat.Builder(applicationContext, BookdoeApplication.CHANNEL_DOWNLOADS)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentTitle(applicationContext.getString(R.string.app_name))
            .setContentText(title)
            .setOngoing(true)
            .setProgress(0, 0, true)
            .build()
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(NOTIFICATION_ID, notification)
        }
    }

    companion object {
        const val TAG = "bookdoe-download"
        const val KEY_JOB_FILE = "jobFile"
        const val KEY_SERVER_ID = "serverId"
        const val KEY_ITEM_ID = "itemId"
        const val KEY_COMPLETED = "completed"
        const val KEY_TOTAL = "total"
        const val KEY_BYTES = "bytes"
        const val KEY_ERROR = "error"
        private const val NOTIFICATION_ID = 4101
    }
}
