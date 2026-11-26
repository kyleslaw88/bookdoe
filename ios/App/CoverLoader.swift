import UIKit
import BookdoeKit

/// Loads cover art off the main thread with the provider's auth headers, downscales it
/// and caches the result. Used by CarPlay rows and Now Playing artwork.
final class CoverLoader: @unchecked Sendable {
    static let shared = CoverLoader()

    private let cache = NSCache<NSString, UIImage>()
    private let limiter = Limiter(max: 4)

    /// Fetch and downscale the cover to fit `maxSize` (points). Returns nil on any
    /// failure or cancellation; callers keep their placeholder.
    func image(for url: URL, provider: MediaProvider, maxSize: CGSize) async -> UIImage? {
        let key = CoverSizing.cacheKey(url: url, maxWidth: maxSize.width, maxHeight: maxSize.height) as NSString
        if let hit = cache.object(forKey: key) { return hit }
        await limiter.acquire()
        defer { Task { await limiter.release() } }
        if Task.isCancelled { return nil }
        if let hit = cache.object(forKey: key) { return hit }
        guard let req = try? provider.mediaRequest(for: url),
              let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              !Task.isCancelled,
              let ui = UIImage(data: data),
              let scaled = Self.downscale(ui, maxSize: maxSize) else { return nil }
        cache.setObject(scaled, forKey: key)
        return scaled
    }

    private static func downscale(_ image: UIImage, maxSize: CGSize) -> UIImage? {
        guard let target = CoverSizing.fittedSize(source: (image.size.width, image.size.height),
                                                  max: (maxSize.width, maxSize.height)) else { return nil }
        let size = CGSize(width: target.width, height: target.height)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        return UIGraphicsImageRenderer(size: size, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
    }

    /// Simple async counting semaphore to bound concurrent fetches.
    private actor Limiter {
        private var available: Int
        private var waiters: [CheckedContinuation<Void, Never>] = []
        init(max: Int) { available = max }
        func acquire() async {
            if available > 0 { available -= 1; return }
            await withCheckedContinuation { waiters.append($0) }
        }
        func release() {
            if waiters.isEmpty { available += 1 } else { waiters.removeFirst().resume() }
        }
    }
}
