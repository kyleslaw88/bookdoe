package bookdoe.app.ui

import java.util.Locale

fun formatTime(seconds: Double): String {
    val s = seconds.toLong().coerceAtLeast(0)
    val h = s / 3600
    val m = (s % 3600) / 60
    val sec = s % 60
    return if (h > 0) String.format(Locale.ROOT, "%d:%02d:%02d", h, m, sec) else String.format(Locale.ROOT, "%d:%02d", m, sec)
}

fun formatDuration(seconds: Double?): String? {
    val s = seconds ?: return null
    if (s <= 0) return null
    val h = (s / 3600).toLong()
    val m = ((s % 3600) / 60).toLong()
    return if (h > 0) "${h}h ${m}m" else "${m}m"
}

fun formatBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val kb = bytes / 1024.0
    if (kb < 1024) return String.format(Locale.ROOT, "%.0f KB", kb)
    val mb = kb / 1024.0
    if (mb < 1024) return String.format(Locale.ROOT, "%.1f MB", mb)
    return String.format(Locale.ROOT, "%.2f GB", mb / 1024.0)
}
