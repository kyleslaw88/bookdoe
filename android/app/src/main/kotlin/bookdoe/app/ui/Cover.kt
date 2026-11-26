package bookdoe.app.ui

import bookdoe.app.AppContainer
import bookdoe.core.MediaItem
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Book
import androidx.compose.material.icons.filled.Headphones
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.request.ImageRequest
import bookdoe.core.CoverAspect
import bookdoe.core.MediaKind

/** Cover art with the provider's auth headers, or a placeholder icon. Square (podcast) art is fitted whole, never cropped. */
@Composable
fun CoverImage(container: AppContainer, url: String?, kind: MediaKind, size: Dp, modifier: Modifier = Modifier) {
    var square by remember(url) { mutableStateOf(false) }
    val context = LocalContext.current
    val shape = RoundedCornerShape(6.dp)
    val headers = remember(url) { if (url != null && url.startsWith("http")) runCatching { container.model.provider?.mediaHeaders() }.getOrNull() ?: emptyMap() else emptyMap() }
    Box(modifier.size(size).clip(shape).background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
        if (url != null) {
            val request = remember(url, headers) {
                ImageRequest.Builder(context).data(url).crossfade(true).apply { headers.forEach { (k, v) -> addHeader(k, v) } }.build()
            }
            AsyncImage(model = request, contentDescription = null, contentScale = if (square) ContentScale.Fit else ContentScale.Crop, modifier = Modifier.size(size),
                onSuccess = { square = CoverAspect.isSquare(it.painter.intrinsicSize.width, it.painter.intrinsicSize.height) })
        } else {
            Icon(if (kind == MediaKind.ebook) Icons.Default.Book else Icons.Default.Headphones, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
fun CoverImage(container: AppContainer, item: MediaItem, size: Dp, modifier: Modifier = Modifier) =
    CoverImage(container, item.coverUrl, item.kind, size, modifier)
