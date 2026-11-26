package bookdoe.core

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CoverAspectTest {
    @Test
    fun squareArtIsNotCropped() {
        assertTrue(CoverAspect.isSquare(1400f, 1400f))
        assertTrue(CoverAspect.isSquare(3000f, 2990f))
    }

    @Test
    fun portraitAndUnknownArtKeepTheCropFrame() {
        assertFalse(CoverAspect.isSquare(1000f, 1500f))
        assertFalse(CoverAspect.isSquare(0f, 0f))
    }
}
