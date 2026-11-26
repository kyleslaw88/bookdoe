import SwiftUI
import BookdoeKit

/// Where a book's bookmarks live: the server, or memory (debug builds and the UI tests).
@MainActor
protocol BookmarkSource: AnyObject {
    func list() async throws -> [Bookmark]
    func add(_ new: NewBookmark) async throws -> Bookmark
    func update(_ bookmark: Bookmark, change: BookmarkChange) async throws -> Bookmark
    func delete(_ bookmark: Bookmark) async throws
    func supportsHighlights() async -> Bool
}

@MainActor
final class ProviderBookmarkSource: BookmarkSource {
    private let provider: MediaProvider
    private let book: BookRef
    init(provider: MediaProvider, book: BookRef) {
        self.provider = provider
        self.book = book
    }
    func list() async throws -> [Bookmark] { try await provider.bookmarks(for: book) }
    func add(_ new: NewBookmark) async throws -> Bookmark { try await provider.addBookmark(new, to: book) }
    func update(_ b: Bookmark, change: BookmarkChange) async throws -> Bookmark { try await provider.updateBookmark(b, change: change, in: book) }
    func delete(_ b: Bookmark) async throws { try await provider.deleteBookmark(b, in: book) }
    func supportsHighlights() async -> Bool { await provider.supportsHighlights() }
}

#if DEBUG
/// Keeps bookmarks in memory so the reader can be driven without a server.
@MainActor
final class MemoryBookmarkSource: BookmarkSource {
    private(set) var items: [Bookmark] = []
    private var next = 1
    func list() async throws -> [Bookmark] { items }
    func add(_ new: NewBookmark) async throws -> Bookmark {
        let b = Bookmark(id: "mem\(next)", assetID: "demo", kind: new.kind, location: new.location, position: new.position, percent: new.percent,
                         title: new.title, note: new.note, color: new.color)
        next += 1
        items.append(b)
        return b
    }
    func update(_ b: Bookmark, change: BookmarkChange) async throws -> Bookmark {
        guard let i = items.firstIndex(where: { $0.id == b.id }) else { return b }
        if case .set(let v) = change.title { items[i].title = v }
        if case .set(let v) = change.note { items[i].note = v }
        if case .set(let v) = change.color { items[i].color = v }
        items[i].updatedAt = Date()
        return items[i]
    }
    func delete(_ b: Bookmark) async throws { items.removeAll { $0.id == b.id } }
    func supportsHighlights() async -> Bool { true }
}
#endif

/// One book's bookmarks and highlights, loaded once and kept in step with what the person does.
@MainActor
final class BookmarksModel: ObservableObject {
    @Published private(set) var items: [Bookmark] = []
    @Published private(set) var loaded = false
    @Published private(set) var highlightsSupported = false
    @Published var error: String?

    let book: BookRef
    private let source: BookmarkSource

    init(book: BookRef, source: BookmarkSource) {
        self.book = book
        self.source = source
    }

    var bookmarks: [Bookmark] { items.filter { $0.kind == .bookmark } }
    var highlights: [Bookmark] { items.filter { $0.kind == .highlight } }

    func load() async {
        do {
            let list = try await source.list()
            items = list.sorted(by: Self.order)
            error = nil
        } catch { report(error) }
        highlightsSupported = await source.supportsHighlights()
        loaded = true
    }

    @discardableResult
    func add(_ new: NewBookmark) async -> Bookmark? {
        do {
            let made = try await source.add(new)
            items = (items + [made]).sorted(by: Self.order)
            error = nil
            return made
        } catch { report(error); return nil }
    }

    @discardableResult
    func update(_ bookmark: Bookmark, change: BookmarkChange) async -> Bookmark? {
        do {
            let new = try await source.update(bookmark, change: change)
            if let i = items.firstIndex(where: { $0.id == bookmark.id }) { items[i] = new }
            error = nil
            return new
        } catch { report(error); return nil }
    }

    func remove(_ bookmark: Bookmark) async {
        do {
            try await source.delete(bookmark)
            items.removeAll { $0.id == bookmark.id }
            error = nil
        } catch { report(error) }
    }

    /// Book order: audio by time, ebook by how far in, then by when it was made.
    private static func order(_ a: Bookmark, _ b: Bookmark) -> Bool {
        if let x = a.position, let y = b.position, x != y { return x < y }
        if let x = a.percent, let y = b.percent, x != y { return x < y }
        return a.createdAt < b.createdAt
    }

    private func report(_ e: Error) {
        error = (e as? APIError)?.errorDescription ?? e.localizedDescription
    }
}

extension AppModel {
    /// A bookmarks model for a book on the active server, or nil with no server.
    func bookmarksModel(for book: BookRef) -> BookmarksModel? {
        guard let provider else { return nil }
        return BookmarksModel(book: book, source: ProviderBookmarkSource(provider: provider, book: book))
    }
}
