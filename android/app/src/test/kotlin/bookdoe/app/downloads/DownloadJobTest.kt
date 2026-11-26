package bookdoe.app.downloads

import bookdoe.core.Chapter
import bookdoe.core.MediaDetail
import bookdoe.core.MediaItem
import bookdoe.core.MediaKind
import bookdoe.core.Track
import org.junit.Assert.assertEquals
import org.junit.Test

class DownloadJobTest {
    @Test
    fun jobRoundTripsThroughJsonAndRebuildsTheDetail() {
        val detail = MediaDetail(
            item = MediaItem(id = "w1", title = "Dune", authors = listOf("Frank Herbert"), kind = MediaKind.audiobook, durationSeconds = 1000.0, coverUrl = "https://x/c"),
            tracks = listOf(Track("f1", 0, "Part 1", "https://x/f1.mp3", 600.0, 0.0, sizeBytes = 10), Track("f2", 1, "Part 2", "https://x/f2.mp3", 400.0, 600.0)),
            chapters = listOf(Chapter("One", 0.0, 500.0)), assetId = "as1",
        )
        val job = DownloadJob.from("srv", detail, mapOf("Authorization" to "Bearer t"))
        val decoded = DownloadJob.decode(DownloadJob.encode(job))
        assertEquals(job, decoded)
        assertEquals("srv:w1", decoded.key)
        assertEquals(2, decoded.files.size)
        assertEquals("srv/w1/001-f2.mp3", decoded.files[1].localRelativePath)
        assertEquals("Bearer t", decoded.files[0].headers["Authorization"])
        val rebuilt = decoded.detail()
        assertEquals(detail.tracks, rebuilt.tracks)
        assertEquals(detail.chapters, rebuilt.chapters)
        assertEquals("as1", rebuilt.assetId)
        assertEquals(1000.0, rebuilt.totalDuration, 0.0)
    }
}
