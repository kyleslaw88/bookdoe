package bookdoe.core

import bookdoe.core.JsonHelpers as J
import kotlinx.serialization.json.JsonObject

/** Podcast shows as the bookdoe Player API serves them (`GET /api/player/v1/podcasts/shows/{id}`, issue #514). */
data class PodcastEpisode(
    val id: String,
    val title: String,
    val releaseDate: String? = null,
    val durationSeconds: Double? = null,
    val description: String = "",
    val played: Boolean = false,
    val positionSeconds: Double = 0.0,
    val missing: Boolean = false,
)

data class PodcastSeason(val id: String, val number: Int?, val isExtras: Boolean, val episodes: List<PodcastEpisode> = emptyList()) {
    val title: String get() = if (isExtras) "Extras" else "Season ${number ?: 0}"
}

data class PodcastShow(
    val id: String,
    val title: String,
    val coverUrl: String?,
    /** Numbered seasons ascending, Extras last (the server's order). */
    val seasons: List<PodcastSeason> = emptyList(),
)

/** One row of the show list. */
data class PodcastShowRef(val id: String, val title: String)

/** What the show page draws for one episode. */
data class PodcastEpisodeRow(
    val id: String,
    val title: String,
    val dateText: String,
    val description: String,
    val played: Boolean,
    val playable: Boolean,
)

/** State behind the show page: the season selector (numbered seasons in order, Extras last) and the selected season's rows. */
data class PodcastShowViewModel(val show: PodcastShow, val selectedSeasonId: String? = show.seasons.firstOrNull()?.id) {
    val title: String get() = show.title
    val coverUrl: String? get() = show.coverUrl
    val seasonTitles: List<Pair<String, String>> get() = show.seasons.map { it.id to it.title }
    val selectedSeason: PodcastSeason? get() = show.seasons.firstOrNull { it.id == selectedSeasonId }

    /** A new state with [seasonId] selected; an unknown id keeps the current selection. */
    fun select(seasonId: String): PodcastShowViewModel =
        if (show.seasons.any { it.id == seasonId }) copy(selectedSeasonId = seasonId) else this

    val rows: List<PodcastEpisodeRow>
        get() = selectedSeason?.episodes.orEmpty().map {
            PodcastEpisodeRow(it.id, it.title, it.releaseDate ?: "", plainText(it.description), it.played, !it.missing)
        }

    fun episode(id: String): PodcastEpisode? = show.seasons.firstNotNullOfOrNull { s -> s.episodes.firstOrNull { it.id == id } }
}

/** The app's player, as the podcast code sees it ([bookdoe.app.playback.PlayerController] in the app module). */
fun interface EpisodePlayer {
    suspend fun load(detail: MediaDetail)
}

/** Tapping an episode: build the playable detail (audio URL, stored position, show name) and hand it to the existing player. */
object PodcastPlayback {
    suspend fun play(episode: PodcastEpisode, show: PodcastShow, provider: BookdoeProvider, player: EpisodePlayer) {
        player.load(provider.podcastEpisodeDetail(episode, show))
    }
}

internal fun parsePodcastShow(obj: JsonObject, baseUrl: String): PodcastShow? {
    val id = J.string(obj, "id") ?: return null
    val seasons = J.array(obj, "seasons").mapNotNull { s ->
        val sid = J.string(s, "id") ?: return@mapNotNull null
        val episodes = J.array(s, "episodes").mapNotNull { e ->
            val eid = J.string(e, "id") ?: return@mapNotNull null
            PodcastEpisode(
                id = eid, title = J.string(e, "title") ?: "", releaseDate = J.string(e, "release_date"),
                durationSeconds = J.double(e, "duration_seconds"), description = J.string(e, "description") ?: "",
                played = J.bool(e, "played") ?: false, positionSeconds = J.double(e, "position_seconds") ?: 0.0,
                missing = J.bool(e, "missing") ?: false,
            )
        }
        PodcastSeason(sid, J.int(s, "number"), J.bool(s, "is_extras") ?: false, episodes)
    }
    // The #514 cover_url is on the cookie-only /api/v1 route; a device token reaches the Player API copy.
    return PodcastShow(id, J.string(obj, "title") ?: "", Urls.appending(baseUrl, "/api/player/v1/podcasts/shows/$id/cover"), seasons)
}

private val tagPattern = Regex("<[^>]*>")
private val entities = mapOf("&amp;" to "&", "&lt;" to "<", "&gt;" to ">", "&quot;" to "\"", "&#39;" to "'", "&nbsp;" to " ")

/** Show notes often carry HTML; the page shows plain text: tags removed, common entities decoded. */
internal fun plainText(html: String): String {
    var t = html.replace(Regex("<\\s*br\\s*/?>|</\\s*p\\s*>", RegexOption.IGNORE_CASE), "\n").replace(tagPattern, "")
    for ((k, v) in entities) t = t.replace(k, v)
    return t.trim()
}
