package bookdoe.app.playback

import bookdoe.app.BookdoeApplication
import bookdoe.app.MainActivity
import bookdoe.app.R
import android.app.PendingIntent
import android.content.Intent
import androidx.annotation.OptIn
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.ResolvingDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.DefaultMediaNotificationProvider
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService

/**
 * Foreground media service: ExoPlayer behind a Media3 MediaSession, which gives the
 * notification, lock-screen and Bluetooth/Android Auto controls for free. Seek
 * increments are 15 s back and 30 s forward so the notification buttons match the
 * app. Audio focus and "becoming noisy" (headphones unplugged) are handled by
 * ExoPlayer.
 */
@OptIn(UnstableApi::class)
class PlaybackService : MediaSessionService() {
    private var session: MediaSession? = null

    override fun onCreate() {
        super.onCreate()
        val container = BookdoeApplication.container(this)
        // Every HTTP request carries the active provider's media headers (Bearer
        // token or MediaBrowser header); local files bypass this.
        val http = OkHttpDataSource.Factory(container.okHttp).setUserAgent("bookdoe-Android")
        val resolving = ResolvingDataSource.Factory(http) { spec ->
            if (spec.uri.scheme == "http" || spec.uri.scheme == "https") spec.withAdditionalHeaders(PlaybackHeaders.headersFor(spec.uri.toString())) else spec
        }
        val dataSourceFactory: DataSource.Factory = DefaultDataSource.Factory(this, resolving)

        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(DefaultMediaSourceFactory(dataSourceFactory))
            .setAudioAttributes(AudioAttributes.Builder().setContentType(C.AUDIO_CONTENT_TYPE_SPEECH).setUsage(C.USAGE_MEDIA).build(), true)
            .setHandleAudioBecomingNoisy(true)
            .setWakeMode(C.WAKE_MODE_NETWORK)
            .setSeekBackIncrementMs(15_000)
            .setSeekForwardIncrementMs(30_000)
            .build()

        val launch = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        session = MediaSession.Builder(this, player)
            .setSessionActivity(launch)
            .build()
        setMediaNotificationProvider(
            DefaultMediaNotificationProvider.Builder(this)
                .setChannelId(BookdoeApplication.CHANNEL_PLAYBACK)
                .setChannelName(R.string.notification_channel_playback)
                .build(),
        )
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onTaskRemoved(rootIntent: Intent?) {
        val player = session?.player
        if (player == null || !player.playWhenReady || player.mediaItemCount == 0) stopSelf()
    }

    override fun onDestroy() {
        session?.run {
            player.release()
            release()
        }
        session = null
        super.onDestroy()
    }
}

/** Process-wide media headers, set by [PlayerController] before each load. */
object PlaybackHeaders {
    @Volatile
    private var headers: Map<String, String> = emptyMap()

    @Volatile
    private var hostPrefix: String? = null

    fun set(baseUrl: String, headers: Map<String, String>) {
        hostPrefix = baseUrl.trimEnd('/')
        this.headers = headers
    }

    fun clear() {
        hostPrefix = null
        headers = emptyMap()
    }

    /** Only the configured server receives the credential, never a third-party host. */
    fun headersFor(url: String): Map<String, String> {
        val prefix = hostPrefix ?: return emptyMap()
        return if (url.startsWith(prefix)) headers else emptyMap()
    }
}
