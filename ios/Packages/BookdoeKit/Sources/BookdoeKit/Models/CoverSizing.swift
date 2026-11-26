import Foundation

/// Pure helpers for cover art loading (CarPlay rows, Now Playing). Kept in the
/// package so they can be unit tested without the App target.
public enum CoverSizing {
    /// Size that fits `source` inside `maxSize`, keeping aspect ratio. Never upscales.
    /// Returns nil for an empty or invalid source or bound.
    public static func fittedSize(source: (width: Double, height: Double),
                                  max maxSize: (width: Double, height: Double)) -> (width: Double, height: Double)? {
        guard source.width > 0, source.height > 0, maxSize.width > 0, maxSize.height > 0,
              source.width.isFinite, source.height.isFinite else { return nil }
        let scale = min(1, maxSize.width / source.width, maxSize.height / source.height)
        return (max(1, (source.width * scale).rounded()), max(1, (source.height * scale).rounded()))
    }

    /// Frame height as a multiple of the width. Book covers are portrait (1:1.4); square
    /// art (podcasts) gets a 1:1 frame so it is never cropped.
    public static func heightFactor(source: (width: Double, height: Double)) -> Double {
        guard source.width > 0, source.height > 0, source.width.isFinite, source.height.isFinite else { return 1.4 }
        return abs(source.height / source.width - 1) <= 0.05 ? 1 : 1.4
    }

    /// Cache key for a cover at a target bound. Different bounds cache separately.
    public static func cacheKey(url: URL, maxWidth: Double, maxHeight: Double) -> String {
        "\(url.absoluteString)#\(Int(maxWidth.rounded()))x\(Int(maxHeight.rounded()))"
    }
}
