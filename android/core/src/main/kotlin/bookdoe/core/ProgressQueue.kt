package bookdoe.core

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Offline-safe progress reporting. Updates are queued with their original client
 * timestamp, coalesced per item (the newest wins locally) and flushed in order. A
 * server answer of `applied: false` carries the server's newer position, which the
 * caller adopts (bookdoe's conflict rule; Jellyfin and Audiobookshelf always
 * apply, so for them the queue is only a retry buffer).
 */
class ProgressQueue(private val file: File? = null) {
    private val mutex = Mutex()
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    private val serializer = ListSerializer(ProgressUpdate.serializer())

    @Volatile
    private var items: List<ProgressUpdate> = file?.takeIf { it.exists() }?.let { f ->
        runCatching { json.decodeFromString(serializer, f.readText()) }.getOrNull()
    } ?: emptyList()

    /** Snapshot of what is queued. */
    val pending: List<ProgressUpdate> get() = items

    /**
     * Queue an update. An older queued update for the same server and asset is
     * replaced, with its `listened` seconds folded into the new one so
     * Audiobookshelf's time-listened stays accurate.
     */
    suspend fun enqueue(update: ProgressUpdate) = mutex.withLock {
        var merged = update
        val list = items.toMutableList()
        val i = list.indexOfFirst { it.serverId == update.serverId && it.assetId == update.assetId && it.sessionId == update.sessionId }
        if (i >= 0) {
            merged = merged.copy(listened = merged.listened + list[i].listened)
            list.removeAt(i)
        }
        list.add(merged)
        items = list
        persist()
    }

    data class FlushResult(
        val sent: Int,
        val failed: Int,
        /** Updates the server rejected as stale, with the server position to adopt. */
        val conflicts: List<Pair<ProgressUpdate, PlaybackPosition>>,
    )

    /**
     * Send every pending update for [serverId] through [provider]. Network failures
     * keep the item queued; authorization and not-found errors drop it (retrying would
     * never succeed). Returns what happened so the UI can adopt server positions.
     */
    suspend fun flush(serverId: String, provider: MediaProvider, session: PlaybackSession? = null): FlushResult = mutex.withLock {
        var sent = 0
        var failed = 0
        val conflicts = ArrayList<Pair<ProgressUpdate, PlaybackPosition>>()
        val mine = items.filter { it.serverId == serverId }.sortedBy { it.clientUpdatedAt }
        for (update in mine) {
            try {
                val report = provider.reportProgress(update, session ?: update.sessionId?.let { PlaybackSession(it) })
                items = items.filter { it.id != update.id }
                sent += 1
                val pos = report.serverPosition
                if (!report.applied && pos != null) conflicts.add(update to pos)
            } catch (e: ApiError) {
                when (e) {
                    is ApiError.Unauthorized, is ApiError.NotFound, is ApiError.Forbidden -> {
                        items = items.filter { it.id != update.id }
                        failed += 1
                    }
                    else -> {
                        failed += 1
                        break // keep order; try again next flush
                    }
                }
            } catch (e: Exception) {
                failed += 1
                break
            }
        }
        persist()
        FlushResult(sent, failed, conflicts)
    }

    suspend fun clear(serverId: String) = mutex.withLock {
        items = items.filter { it.serverId != serverId }
        persist()
    }

    private fun persist() {
        val f = file ?: return
        runCatching {
            f.parentFile?.mkdirs()
            val tmp = File(f.path + ".tmp")
            tmp.writeText(json.encodeToString(serializer, items))
            if (!tmp.renameTo(f)) {
                f.writeText(json.encodeToString(serializer, items))
                tmp.delete()
            }
        }
    }
}
