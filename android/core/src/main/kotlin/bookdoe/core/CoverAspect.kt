package bookdoe.core

import kotlin.math.abs

/** Cover framing rules, kept in core so they can be unit tested without the app module. */
object CoverAspect {
    /** True for square art (podcast covers), which is shown whole instead of cropped like portrait book covers. */
    fun isSquare(width: Float, height: Float): Boolean =
        width > 0f && height > 0f && width.isFinite() && height.isFinite() && abs(height / width - 1f) <= 0.05f
}
