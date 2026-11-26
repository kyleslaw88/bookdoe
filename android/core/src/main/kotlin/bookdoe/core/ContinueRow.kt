package bookdoe.core

/** View model for the Home "Continue listening" row: books and podcast episodes in feed (last played) order. */
class ContinueRow(val items: List<MediaItem>) {
    /** The row is hidden when the feed is empty. */
    val visible: Boolean get() = items.isNotEmpty()

    /** Episode entries show the show name; books show their authors. */
    fun subtitle(item: MediaItem): String = item.subtitleLine

    /** A tap starts an episode in the player, and opens a book's detail page. */
    fun tap(item: MediaItem, playEpisode: (MediaItem) -> Unit, openBook: (String) -> Unit) {
        if (item.isEpisode) playEpisode(item) else openBook(item.id)
    }
}
