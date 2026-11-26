package bookdoe.core

import kotlinx.coroutines.CancellationException

/** Where downloaded episode files live. The app backs it with the downloads root; tests with a map. */
interface EpisodeStorage {
    fun exists(relativePath: String): Boolean
    fun size(relativePath: String): Long

    /** Removes the file; true when something was removed. */
    fun delete(relativePath: String): Boolean

    /** The URL the player reads the file from. */
    fun uri(relativePath: String): String
}

/** Fetches one URL into [relativePath] of the storage and returns the bytes written. Throws on any network or HTTP failure. */
fun interface EpisodeFetcher {
    suspend fun fetch(url: String, headers: Map<String, String>, relativePath: String): Long
}

sealed interface EpisodeDownloadState {
    data object NotDownloaded : EpisodeDownloadState
    data object Downloading : EpisodeDownloadState
    data class Downloaded(val bytes: Long) : EpisodeDownloadState
    data class Failed(val message: String) : EpisodeDownloadState
}

/**
 * Offline download of podcast episodes (issue #521). One state per episode: not downloaded, downloading, downloaded or
 * failed (a failed download can be started again). Completed downloads are recorded in the same [DownloadIndex] the
 * audiobook downloads use, so [offlineDetail] is the shape the player already loads.
 */
class PodcastDownloads(
    private val serverId: String,
    private val storage: EpisodeStorage,
    private val fetcher: EpisodeFetcher,
    private val index: DownloadIndex,
) {
    private val failures = HashMap<String, String>()
    private val running = HashSet<String>()

    @Synchronized
    fun state(episodeId: String): EpisodeDownloadState {
        if (episodeId in running) return EpisodeDownloadState.Downloading
        index.item(serverId, episodeId)?.let { return EpisodeDownloadState.Downloaded(it.bytes) }
        return failures[episodeId]?.let { EpisodeDownloadState.Failed(it) } ?: EpisodeDownloadState.NotDownloaded
    }

    /** Total bytes held by downloaded episodes. */
    val usedBytes: Long get() = index.items.filter { it.serverId == serverId }.sumOf { it.bytes }

    /**
     * Download the episode described by [detail] (the one-track detail from the show page). Does nothing when the episode
     * is already downloaded or downloading. On failure the partial file is removed and the state is Failed.
     */
    suspend fun download(detail: MediaDetail, headers: Map<String, String>): EpisodeDownloadState {
        val id = detail.item.id
        synchronized(this) {
            if (id in running || index.item(serverId, id) != null) return state(id)
            running.add(id)
            failures.remove(id)
        }
        val plan = DownloadPlan.from(serverId, detail, headers)
        val file = plan.files.firstOrNull { it.trackId != "ebook" }
        try {
            if (file == null) throw IllegalStateException("The episode has no audio file.")
            val bytes = fetcher.fetch(file.remote, file.headers, file.localRelativePath)
            if (!storage.exists(file.localRelativePath)) throw IllegalStateException("The download did not produce a file.")
            val track = detail.tracks.first { it.id == file.trackId }
            index.upsert(DownloadedItem.from(serverId, detail, listOf(DownloadedTrack(track.id, track.title, file.localRelativePath, track.durationSeconds)), bytes))
        } catch (e: CancellationException) {
            file?.let { storage.delete(it.localRelativePath) }
            synchronized(this) { running.remove(id) }
            throw e
        } catch (e: Exception) {
            file?.let { storage.delete(it.localRelativePath) }
            synchronized(this) { failures[id] = e.message ?: "Download failed"; running.remove(id) }
            return state(id)
        }
        synchronized(this) { running.remove(id) }
        return state(id)
    }

    /** Delete the downloaded file and its index entry. Returns the bytes freed (0 when nothing was downloaded). */
    @Synchronized
    fun delete(episodeId: String): Long {
        failures.remove(episodeId)
        val item = index.remove(serverId, episodeId) ?: return 0
        for (t in item.tracks) storage.delete(t.relativePath)
        return item.bytes
    }

    /**
     * The detail of a downloaded episode with its track pointing at the local file, or null when it is not downloaded (or the
     * file has gone). [positionSeconds] is where to resume.
     */
    fun offlineDetail(episodeId: String, positionSeconds: Double = 0.0): MediaDetail? {
        val item = index.item(serverId, episodeId) ?: return null
        if (item.tracks.any { !storage.exists(it.relativePath) }) return null
        val base = item.detail(java.io.File("."))
        val tracks = base.tracks.mapIndexed { i, t -> t.copy(url = storage.uri(item.tracks[i].relativePath)) }
        val track = tracks.firstOrNull()
        return base.copy(
            tracks = tracks,
            position = PlaybackPosition(trackId = track?.id, trackIndex = 0, positionInTrack = positionSeconds, absolutePosition = positionSeconds),
        )
    }

    /** Tapping a downloaded episode: hand the local-file detail to the player. False when the episode is not available offline. */
    suspend fun playOffline(episodeId: String, positionSeconds: Double, player: EpisodePlayer): Boolean {
        val detail = offlineDetail(episodeId, positionSeconds) ?: return false
        player.load(detail)
        return true
    }
}

/** Resume positions listened to offline: queued with their own timestamp, written to the podcast progress route (#516) on flush. */
object PodcastOfflineProgress {
    suspend fun record(queue: ProgressQueue, serverId: String, episodeId: String, positionSeconds: Double, durationSeconds: Double?, at: Long = System.currentTimeMillis()) {
        queue.enqueue(
            ProgressUpdate(
                serverId = serverId, itemId = episodeId, assetId = BookdoeProvider.PODCAST_ASSET_PREFIX + episodeId, trackId = episodeId,
                positionInTrack = positionSeconds, absolutePosition = positionSeconds, duration = durationSeconds, clientUpdatedAt = at,
            ),
        )
    }
}
