import SwiftUI
import BookdoeKit

/// Cover art loaded with the provider's auth headers (AsyncImage cannot send them)
/// and a small in-memory cache.
struct CoverImage: View {
    let url: URL?
    var size: CGFloat = 64

    @EnvironmentObject private var model: AppModel
    @State private var image: Image?
    /// Square art (podcasts) is shown whole in a 1:1 frame; book covers use the 1:1.4 portrait frame.
    @State private var heightFactor = 1.4

    private static let cache = NSCache<NSURL, CGImageBox>()

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.15))
            if let image {
                image.resizable().scaledToFill()
            } else {
                Image(systemName: "book.closed").foregroundStyle(.secondary)
            }
        }
        .frame(width: size, height: size * heightFactor)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isImage)
        .accessibilityLabel("Cover")
        .accessibilityIdentifier(image == nil ? "cover.placeholder" : "cover.loaded")
        .task(id: url) { await load() }
    }

    private func load() async {
        guard let url else { image = nil; return }
        if let cached = Self.cache.object(forKey: url as NSURL) {
            heightFactor = CoverSizing.heightFactor(source: (Double(cached.image.width), Double(cached.image.height)))
            image = Image(decorative: cached.image, scale: 1)
            return
        }
        guard let provider = model.provider, let req = try? provider.mediaRequest(for: url) else { return }
        guard let (data, resp) = try? await URLSession.shared.data(for: req), (resp as? HTTPURLResponse)?.statusCode == 200 else { return }
        #if canImport(UIKit)
        guard let ui = UIImage(data: data), let cg = ui.cgImage else { return }
        Self.cache.setObject(CGImageBox(cg), forKey: url as NSURL)
        heightFactor = CoverSizing.heightFactor(source: (Double(cg.width), Double(cg.height)))
        image = Image(decorative: cg, scale: ui.scale)
        #endif
    }
}

final class CGImageBox {
    let image: CGImage
    init(_ image: CGImage) { self.image = image }
}

#if canImport(UIKit)
import UIKit
#endif
