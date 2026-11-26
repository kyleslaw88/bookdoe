package bookdoe.app

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context

class BookdoeApplication : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        createChannels()
        container = AppContainer(this)
    }

    private fun createChannels() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel(CHANNEL_PLAYBACK, getString(R.string.notification_channel_playback), NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CHANNEL_DOWNLOADS, getString(R.string.notification_channel_downloads), NotificationManager.IMPORTANCE_LOW))
    }

    companion object {
        const val CHANNEL_PLAYBACK = "playback"
        const val CHANNEL_DOWNLOADS = "downloads"

        fun container(context: Context): AppContainer = (context.applicationContext as BookdoeApplication).container
    }
}
