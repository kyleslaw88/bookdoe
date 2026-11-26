package bookdoe.app

import bookdoe.app.data.EncryptedCredentialStore
import bookdoe.app.downloads.DownloadManager
import bookdoe.app.playback.PlayerController
import bookdoe.core.DownloadIndex
import bookdoe.core.OkHttpHttpClient
import bookdoe.core.ProgressQueue
import bookdoe.core.ServerStore
import android.content.Context
import android.os.Build
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import java.io.File
import java.util.concurrent.TimeUnit

/** The object graph, built once per process by [BookdoeApplication]. */
class AppContainer(context: Context) {
    val appContext: Context = context.applicationContext
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    val okHttp: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private val support = File(appContext.filesDir, "bookdoe").apply { mkdirs() }
    val downloadsRoot = File(appContext.filesDir, "downloads").apply { mkdirs() }

    val serverStore = ServerStore(File(support, "servers.json"))
    val credentials = EncryptedCredentialStore(appContext)
    val progressQueue = ProgressQueue(File(support, "progress-queue.json"))
    val downloadIndex = DownloadIndex(File(support, "downloads.json"))

    val model = AppModel(
        serverStore = serverStore,
        credentials = credentials,
        client = OkHttpHttpClient(okHttp, "bookdoe-Android/${BuildConfig.VERSION_NAME}"),
        progressQueue = progressQueue,
        appVersion = BuildConfig.VERSION_NAME,
        deviceName = deviceName(),
        scope = scope,
    )

    val downloads = DownloadManager(appContext, downloadsRoot, downloadIndex, scope)
    val player = PlayerController(appContext, scope)

    init {
        player.onProgress = { update, session -> scope.launch { model.enqueueAndFlush(update, session) } }
        model.onConflict = { update, pos -> if (player.current.value?.assetId == update.assetId) player.adopt(pos) }
        model.onServerRemoved = { serverId ->
            if (player.serverId == serverId) player.stop(report = false)
            downloads.removeAll(serverId)
        }
        scope.launch { model.flushProgress(null) }
    }

    companion object {
        fun deviceName(): String {
            val model = Build.MODEL ?: "Android"
            val maker = Build.MANUFACTURER ?: ""
            return if (model.startsWith(maker, ignoreCase = true) || maker.isBlank()) model else "$maker $model"
        }
    }
}
