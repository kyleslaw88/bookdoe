package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.core.MediaItem
import bookdoe.core.MediaKind
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp

@Composable
fun ItemRow(container: AppContainer, item: MediaItem, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick).padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        CoverImage(container, item, 56.dp)
        Column(Modifier.padding(start = 12.dp).weight(1f)) {
            Text(item.title, style = MaterialTheme.typography.bodyLarge, maxLines = 2, overflow = TextOverflow.Ellipsis)
            val line = buildString {
                append(item.subtitleLine)
                if (item.kind == MediaKind.ebook) append(if (isEmpty()) "Ebook" else "  Ebook")
                formatDuration(item.durationSeconds)?.let { append(if (isEmpty()) it else "  $it") }
            }
            if (line.isNotEmpty()) Text(line, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
            item.series?.let { s ->
                Text(if (item.seriesSequence != null) "$s #${item.seriesSequence}" else s, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
            }
            val p = item.progress
            if (p != null && p > 0) LinearProgressIndicator(progress = { p.toFloat().coerceIn(0f, 1f) }, modifier = Modifier.width(120.dp).padding(top = 4.dp))
        }
    }
}
