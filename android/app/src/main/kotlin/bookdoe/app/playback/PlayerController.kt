package bookdoe.app.playback

import bookdoe.core.Chapter
import bookdoe.core.MediaDetail
import bookdoe.core.MediaProvider
import bookdoe.core.PlaybackPosition
import bookdoe.core.PlaybackSession
import bookdoe.core.ProgressUpdate
import android.content.ComponentName
import android.content.Context
import android.net.Uri
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * App-side playback state over a [MediaController] connected to [PlaybackService].
 * Multi-file books are one timeline: each track is a playlist entry and the
 * absolute position is `track.startOffset + positionInTrack`. Progress reports go
 * out every 15 s while playing and on pause, seek, track change and stop.
 */
class PlayerController(private val context: Context, private val scope: CoroutineScope) {
    private val _current = MutableStateFlow<MediaDetail?>(null)
    val current: StateFlow<MediaDetail?> = _current.asStateFlow()
    private val _isPlaying = MutableStateFlow(false)
    val isPlaying: StateFlow<Boolean> = _isPlaying.asStateFlow()
    private val _trackIndex = MutableStateFlow(0)
    val trackIndex: StateFlow<Int> = _trackIndex.asStateFlow()
    private val _positionInTrack = MutableStateFlow(0.0)
    val positionInTrack: StateFlow<Double> = _positionInTrack.asStateFlow()
    private val _rate = MutableStateFlow(1.0f)
    val rate: StateFlow<Float> = _rate.asStateFlow()
    private val _sleepTimerEnds = MutableStateFlow<Long?>(null)
    val sleepTimerEnds: StateFlow<Long?> = _sleepTimerEnds.asStateFlow()
    private val _buffering = MutableStateFlow(false)
    val buffering: StateFlow<Boolean> = _buffering.asStateFlow()

    var onProgress: ((ProgressUpdate, PlaybackSession?) -> Unit)? = null

    var serverId: String? = null
        private set
    private var provider: MediaProvider? = null
    private var session: PlaybackSession? = null
    private var controllerDeferred: CompletableDeferred<MediaController>? = null
    private var ticker: Job? = null
    private var lastReportAt = 0L
    private var lastReportedPosition = 0.0
    private var listenedSinceReport = 0.0
    private var lastTickPosition = 0.0

    val absolutePosition: Double
        get() {
            val d = _current.value ?: return _positionInTrack.value
            val t = d.track(_trackIndex.value) ?: return _positionInTrack.value
            return t.startOffset + _positionInTrack.value
        }

    val duration: Double get() = _current.value?.totalDuration ?: 0.0

    val currentChapter: Chapter?
        get() {
            val d = _current.value ?: return null
            val pos = absolutePosition
            return d.effectiveChapters.lastOrNull { it.start <= pos }
        }

    private val listener = object : Player.Listener {
        override fun onIsPlayingChanged(isPlaying: Boolean) {
            _isPlaying.value = isPlaying
            if (!isPlaying) report(force = true)
        }

        override fun onPlaybackStateChanged(playbackState: Int) {
            _buffering.value = playbackState == Player.STATE_BUFFERING
            if (playbackState == Player.STATE_ENDED) {
                _isPlaying.value = false
                makeUpdate(finished = true)?.let { onProgress?.invoke(it, session) }
            }
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            val d = _current.value ?: return
            val id = mediaItem?.mediaId ?: return
            val t = d.tracks.firstOrNull { it.id == id } ?: return
            if (t.index != _trackIndex.value) {
                _trackIndex.value = t.index
                _positionInTrack.value = 0.0
                lastTickPosition = 0.0
                report(force = true)
            }
        }

        override fun onPlaybackParametersChanged(playbackParameters: androidx.media3.common.PlaybackParameters) {
            _rate.value = playbackParameters.speed
        }
    }

    private suspend fun controller(): MediaController {
        controllerDeferred?.let { return it.await() }
        val deferred = CompletableDeferred<MediaController>()
        controllerDeferred = deferred
        val token = SessionToken(context, ComponentName(context, PlaybackService::class.java))
        val future = MediaController.Builder(context, token).buildAsync()
        future.addListener({
            try {
                val c = future.get()
                c.addListener(listener)
                deferred.complete(c)
            } catch (e: Exception) {
                controllerDeferred = null
                deferred.completeExceptionally(e)
            }
        }, { r -> scope.launch { r.run() } })
        return deferred.await()
    }

    // load and control

    suspend fun load(detail: MediaDetail, provider: MediaProvider, serverId: String, autoplay: Boolean = true) {
        stop(report = true)
        _current.value = detail
        this.provider = provider
        this.serverId = serverId
        // bookdoe needs no session; Jellyfin and Audiobookshelf get one so their
        // progress reports are attributed. A failure here must not block playback.
        session = withTimeoutOrNull(10_000) { runCatching { provider.startPlayback(detail) }.getOrNull() }
        PlaybackHeaders.set(provider.server.baseUrl, runCatching { provider.mediaHeaders() }.getOrDefault(emptyMap()))
        val c = controller()
        val items = detail.tracks.sortedBy { it.index }.map { t ->
            MediaItem.Builder()
                .setUri(t.url)
                .setMediaId(t.id)
                .setMediaMetadata(
                    MediaMetadata.Builder()
                        .setTitle(detail.item.title)
                        .setArtist(detail.item.authorLine.ifEmpty { null })
                        .setAlbumTitle(detail.item.series ?: detail.item.title)
                        .setSubtitle(t.title)
                        .setArtworkUri(detail.item.coverUrl?.let(Uri::parse))
                        .setMediaType(MediaMetadata.MEDIA_TYPE_AUDIO_BOOK)
                        .build(),
                )
                .build()
        }
        val start = detail.position ?: PlaybackPosition()
        val startList = detail.tracks.sortedBy { it.index }.indexOfFirst { it.index == start.trackIndex }.coerceAtLeast(0)
        c.setMediaItems(items, startList, (start.positionInTrack * 1000).toLong())
        c.setPlaybackSpeed(_rate.value)
        c.prepare()
        _trackIndex.value = start.trackIndex
        _positionInTrack.value = start.positionInTrack
        lastTickPosition = start.positionInTrack
        lastReportedPosition = absolutePosition
        lastReportAt = System.currentTimeMillis()
        startTicker()
        if (autoplay) c.play()
    }

    fun play() = scope.launch { controller().play() }

    fun pause() = scope.launch { controller().pause() }

    fun togglePlayPause() = scope.launch { val c = controller(); if (c.isPlaying) c.pause() else c.play() }

    fun skip(seconds: Double) = seekToAbsolute(maxOf(0.0, absolutePosition + seconds))

    fun seekToAbsolute(position: Double) {
        val d = _current.value ?: return
        val loc = d.locate(minOf(position, maxOf(0.0, d.totalDuration - 1)))
        seekTo(loc.index, loc.offset)
    }

    fun seekTo(trackIndex: Int, offset: Double) {
        val d = _current.value ?: return
        val sorted = d.tracks.sortedBy { it.index }
        val listIndex = sorted.indexOfFirst { it.index == trackIndex }
        if (listIndex < 0) return
        _trackIndex.value = trackIndex
        _positionInTrack.value = offset
        lastTickPosition = offset
        scope.launch {
            controller().seekTo(listIndex, (offset * 1000).toLong())
            report(force = true)
        }
    }

    fun seekToChapter(chapter: Chapter) = seekToAbsolute(chapter.start)

    fun setRate(rate: Float) {
        _rate.value = rate
        scope.launch { controller().setPlaybackSpeed(rate) }
    }

    suspend fun stop(report: Boolean) {
        if (_current.value == null) return
        ticker?.cancel()
        syncFromController()
        if (report) {
            makeUpdate(finished = false)?.let { update ->
                onProgress?.invoke(update, session)
                provider?.let { p -> runCatching { withTimeoutOrNull(10_000) { p.stopPlayback(update, session) } } }
            }
        }
        runCatching { controller() }.getOrNull()?.let { c ->
            c.pause()
            c.stop()
            c.clearMediaItems()
        }
        _isPlaying.value = false
        _current.value = null
        session = null
        PlaybackHeaders.clear()
    }

    fun adopt(serverPosition: PlaybackPosition) {
        val d = _current.value ?: return
        val t = serverPosition.trackId?.let { id -> d.tracks.firstOrNull { it.id == id } }
        if (t != null) seekTo(t.index, serverPosition.positionInTrack) else seekToAbsolute(serverPosition.absolutePosition)
    }

    fun setSleepTimer(minutes: Int?) {
        _sleepTimerEnds.value = minutes?.let { System.currentTimeMillis() + it * 60_000L }
    }

    // internals

    private fun startTicker() {
        ticker?.cancel()
        ticker = scope.launch {
            while (isActive) {
                delay(500)
                tick()
            }
        }
    }

    private fun syncFromController() {
        val c = controllerDeferred?.takeIf { it.isCompleted }?.getCompleted() ?: return
        val d = _current.value ?: return
        val sorted = d.tracks.sortedBy { it.index }
        val li = c.currentMediaItemIndex
        if (li in sorted.indices) _trackIndex.value = sorted[li].index
        val pos = c.currentPosition.coerceAtLeast(0) / 1000.0
        _positionInTrack.value = pos
    }

    private fun tick() {
        if (_current.value == null) return
        val before = lastTickPosition
        syncFromController()
        val now = _positionInTrack.value
        if (_isPlaying.value) {
            val delta = now - before
            if (delta in 0.0..5.0) listenedSinceReport += delta
        }
        lastTickPosition = now
        val ends = _sleepTimerEnds.value
        if (ends != null && System.currentTimeMillis() >= ends) {
            _sleepTimerEnds.value = null
            pause()
            return
        }
        if (_isPlaying.value) report(force = false)
    }

    private fun makeUpdate(finished: Boolean): ProgressUpdate? {
        val d = _current.value ?: return null
        val serverId = serverId ?: return null
        val track = d.track(_trackIndex.value)
        val total = d.totalDuration
        val abs = absolutePosition
        val update = ProgressUpdate(
            serverId = serverId, itemId = d.item.id, assetId = d.assetId, trackId = track?.id, trackIndex = _trackIndex.value,
            positionInTrack = _positionInTrack.value, absolutePosition = abs, duration = if (total > 0) total else track?.durationSeconds,
            percent = if (total > 0) minOf(1.0, abs / total) else null, clientUpdatedAt = System.currentTimeMillis(), sessionId = session?.sessionId,
            isFinished = finished || (total > 0 && abs >= total - 2), listened = listenedSinceReport,
        )
        listenedSinceReport = 0.0
        return update
    }

    private fun report(force: Boolean) {
        val now = System.currentTimeMillis()
        if (!force && (now - lastReportAt < 15_000 || kotlin.math.abs(absolutePosition - lastReportedPosition) < 1)) return
        val update = makeUpdate(finished = false) ?: return
        lastReportAt = now
        lastReportedPosition = update.absolutePosition
        onProgress?.invoke(update, session)
    }
}
