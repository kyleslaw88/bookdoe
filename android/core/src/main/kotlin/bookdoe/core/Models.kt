package bookdoe.core

import kotlinx.serialization.Serializable
import java.util.UUID

/** Which server API a saved server speaks. */
@Serializable
enum class ServerKind {
    bookdoe, jellyfin, audiobookshelf;

    val displayName: String
        get() = when (this) {
            bookdoe -> "bookdoe"
            jellyfin -> "Jellyfin"
            audiobookshelf -> "Audiobookshelf"
        }
}

@Serializable
enum class MediaKind { audiobook, ebook }

/** A saved server. Credentials are not stored here (see [CredentialStore]). */
@Serializable
data class ServerRecord(
    val id: String = UUID.randomUUID().toString(),
    val name: String,
    val kind: ServerKind,
    /** Absolute http(s) URL without a trailing slash; may carry a base path. */
    val baseUrl: String,
    val username: String,
    /** Server-side user id (Jellyfin, Audiobookshelf) once known. */
    val userId: String? = null,
    /** Selected library (Jellyfin view id, Audiobookshelf library id). bookdoe has one library. */
    val libraryId: String? = null,
    /** Stable per-install identifier sent as the device id. */
    val deviceId: String = UUID.randomUUID().toString(),
    /** Epoch milliseconds. */
    val addedAt: Long = System.currentTimeMillis(),
) {
    init {
        require(baseUrl.startsWith("http://") || baseUrl.startsWith("https://")) { "baseUrl must be absolute" }
    }

    /** Host (and port when not the default) for display. */
    val hostLabel: String
        get() = Urls.hostLabel(baseUrl)

    companion object {
        /** Normalises what a person typed: trims, strips a trailing slash. */
        fun normalizeBaseUrl(input: String): String? {
            val t = input.trim().trimEnd('/')
            if (t.isEmpty()) return null
            val withScheme = if (t.startsWith("http://") || t.startsWith("https://")) t else "http://$t"
            return if (Urls.isValid(withScheme)) withScheme else null
        }
    }
}

/** What the credential store holds per server. */
@Serializable
data class Credential(
    val accessToken: String,
    val refreshToken: String? = null,
    /** Epoch milliseconds. */
    val expiresAt: Long? = null,
)

data class ServerInfo(
    val name: String,
    val version: String? = null,
    val supportsPasswordLogin: Boolean,
    val supportsDevicePairing: Boolean = false,
    val setupRequired: Boolean = false,
)

data class LibraryRef(val id: String, val name: String, val kind: MediaKind? = null)

@Serializable
data class MediaItem(
    val id: String,
    val title: String,
    val subtitle: String? = null,
    val authors: List<String> = emptyList(),
    val narrators: List<String> = emptyList(),
    val series: String? = null,
    val seriesSequence: String? = null,
    val kind: MediaKind,
    val durationSeconds: Double? = null,
    val coverUrl: String? = null,
    /** 0..1 when the server knows a position. */
    val progress: Double? = null,
    /** Epoch milliseconds. */
    val updatedAt: Long? = null,
    /** True for a podcast episode in the Continue Listening feed; [id] is then the episode id and [showTitle] its show. */
    val isEpisode: Boolean = false,
    val showTitle: String? = null,
) {
    val authorLine: String get() = authors.joinToString(", ")
    /** The line under the title in a row: an episode's show name, otherwise the authors. */
    val subtitleLine: String get() = if (isEpisode) showTitle.orEmpty() else authorLine
}

@Serializable
data class Track(
    val id: String,
    val index: Int,
    val title: String,
    val url: String,
    val durationSeconds: Double? = null,
    /** Seconds from the start of the book to the start of this track. */
    val startOffset: Double = 0.0,
    val mimeType: String? = null,
    val sizeBytes: Long? = null,
)

@Serializable
data class Chapter(val title: String, val start: Double, val end: Double? = null)

/** A listening or reading position as the server last stored it. */
@Serializable
data class PlaybackPosition(
    val trackId: String? = null,
    val trackIndex: Int = 0,
    val positionInTrack: Double = 0.0,
    /** Seconds from the start of the whole book. */
    val absolutePosition: Double = 0.0,
    val percent: Double? = null,
    /** Epoch milliseconds. */
    val updatedAt: Long? = null,
    /** Ebook location (EPUB CFI or PDF page) when the item is an ebook. */
    val location: String? = null,
)

data class MediaDetail(
    val item: MediaItem,
    val description: String? = null,
    val tracks: List<Track> = emptyList(),
    val chapters: List<Chapter> = emptyList(),
    val position: PlaybackPosition? = null,
    /**
     * Provider-specific asset identity used when reporting progress (bookdoe asset
     * id, Audiobookshelf library item id, Jellyfin item id).
     */
    val assetId: String,
    val ebookDownloadUrl: String? = null,
    val ebookFormat: String? = null,
) {
    val totalDuration: Double
        get() {
            val d = item.durationSeconds
            if (d != null && d > 0) return d
            return tracks.sumOf { it.durationSeconds ?: 0.0 }
        }

    /** Chapters derived from track boundaries when the server supplied none. */
    val effectiveChapters: List<Chapter>
        get() = chapters.ifEmpty {
            tracks.map { t -> Chapter(t.title, t.startOffset, t.durationSeconds?.let { t.startOffset + it }) }
        }

    data class Location(val index: Int, val offset: Double)

    /** The track and in-track offset for an absolute position. */
    fun locate(absolute: Double): Location {
        if (tracks.isEmpty()) return Location(0, maxOf(0.0, absolute))
        for (t in tracks.asReversed()) {
            if (absolute >= t.startOffset) return Location(t.index, maxOf(0.0, absolute - t.startOffset))
        }
        return Location(tracks[0].index, 0.0)
    }

    fun track(index: Int): Track? = tracks.firstOrNull { it.index == index }
}

data class LibraryPage(val items: List<MediaItem>, val total: Int, val offset: Int, val limit: Int) {
    val nextOffset: Int?
        get() = if (offset + items.size < total && items.isNotEmpty()) offset + items.size else null
}

enum class LibrarySort { title, added, updated }

/**
 * One playback session as the server sees it (Audiobookshelf session id, Jellyfin
 * PlaySessionId). bookdoe needs none.
 */
data class PlaybackSession(val sessionId: String?, val startedAt: Long = System.currentTimeMillis())

/** A progress report queued for the server. */
@Serializable
data class ProgressUpdate(
    val id: String = UUID.randomUUID().toString(),
    val serverId: String,
    val itemId: String,
    val assetId: String,
    val trackId: String? = null,
    val trackIndex: Int = 0,
    val positionInTrack: Double = 0.0,
    val absolutePosition: Double = 0.0,
    val duration: Double? = null,
    val percent: Double? = null,
    val location: String? = null,
    /** Epoch milliseconds on the client clock. */
    val clientUpdatedAt: Long = System.currentTimeMillis(),
    val sessionId: String? = null,
    val isFinished: Boolean = false,
    /** Seconds listened since the previous report (Audiobookshelf timeListened). */
    val listened: Double = 0.0,
)

/** What the server answered to a progress report. */
data class ProgressReport(
    val applied: Boolean,
    /** When the server rejected the update as stale it returns its own position. */
    val serverPosition: PlaybackPosition? = null,
)

data class AuthResult(val credential: Credential, val server: ServerRecord, val displayName: String? = null)

/** Device-code pairing state (bookdoe). */
data class DevicePairing(
    val deviceCode: String,
    val userCode: String,
    val verificationUrl: String?,
    val interval: Int,
    val expiresIn: Int,
)

sealed class PairingPoll {
    data object Pending : PairingPoll()
    data object SlowDown : PairingPoll()
    data class Approved(val result: AuthResult) : PairingPoll()
    data object Denied : PairingPoll()
    data object Expired : PairingPoll()
}
