package bookdoe.app.ui.theme

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

private val light = lightColorScheme(
    primary = Color(0xFF6D4C41),
    secondary = Color(0xFF8D6E63),
    tertiary = Color(0xFF9C7C38),
    background = Color(0xFFFBF7F0),
    surface = Color(0xFFFFFBF5),
)

private val dark = darkColorScheme(
    primary = Color(0xFFD7B59A),
    secondary = Color(0xFFBCAAA4),
    tertiary = Color(0xFFE0C27A),
    background = Color(0xFF1C1B1A),
    surface = Color(0xFF242321),
)

@Composable
fun BookdoeTheme(content: @Composable () -> Unit) {
    val darkTheme = isSystemInDarkTheme()
    val context = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.S -> if (darkTheme) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        darkTheme -> dark
        else -> light
    }
    MaterialTheme(colorScheme = scheme, content = content)
}
