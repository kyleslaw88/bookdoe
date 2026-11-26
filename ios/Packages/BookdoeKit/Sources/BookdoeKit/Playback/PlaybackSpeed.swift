import Foundation

/// The playback speeds the player offers, the CarPlay cycle through them and the
/// rate values reported to the now-playing info (no MediaPlayer import).
public enum PlaybackSpeed {
    public static let options: [Float] = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
    public static let normal: Float = 1.0

    /// The speed after `current`: the next larger option, wrapping to 1.0 after the last.
    /// A rate that is not an option (an old saved value) moves to the next larger option.
    public static func next(after current: Float) -> Float {
        options.first { $0 > current + 0.001 } ?? normal
    }

    /// Button label such as "1.25x".
    public static func label(for rate: Float) -> String {
        String(format: "%.2fx", rate)
    }

    /// The rate values for `MPNowPlayingInfoPropertyPlaybackRate` and
    /// `MPNowPlayingInfoPropertyDefaultPlaybackRate`: zero while paused so elapsed time holds still.
    public static func nowPlayingRates(rate: Float, isPlaying: Bool) -> (playback: Double, defaultRate: Double) {
        (isPlaying ? Double(rate) : 0, Double(rate))
    }
}
