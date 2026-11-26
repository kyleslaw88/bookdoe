import Foundation

public enum BookmarkKind: String, Codable, Sendable {
    case bookmark
    /// A selected passage of an ebook, optionally with a note and a colour.
    case highlight
}

/// A saved place in a book (an ebook page or an audio position) or a highlighted passage.
public struct Bookmark: Identifiable, Codable, Equatable, Sendable {
    public var id: String
    public var assetID: String
    public var kind: BookmarkKind
    /// Ebook: an EPUB CFI (a point for a bookmark, a range for a highlight) or a PDF page.
    public var location: String?
    /// Audio: seconds from the start of the book.
    public var position: Double?
    public var percent: Double?
    /// A bookmark's label, or a highlight's selected text.
    public var title: String?
    public var note: String?
    public var color: String?
    public var createdAt: Date
    public var updatedAt: Date

    public init(id: String, assetID: String, kind: BookmarkKind = .bookmark, location: String? = nil, position: Double? = nil,
                percent: Double? = nil, title: String? = nil, note: String? = nil, color: String? = nil,
                createdAt: Date = Date(), updatedAt: Date? = nil) {
        self.id = id
        self.assetID = assetID
        self.kind = kind
        self.location = location
        self.position = position
        self.percent = percent
        self.title = title
        self.note = note
        self.color = color
        self.createdAt = createdAt
        self.updatedAt = updatedAt ?? createdAt
    }
}

/// What to create: the server assigns the id and the dates.
public struct NewBookmark: Equatable, Sendable {
    public var kind: BookmarkKind
    public var location: String?
    public var position: Double?
    public var percent: Double?
    public var title: String?
    public var note: String?
    public var color: String?

    public init(kind: BookmarkKind = .bookmark, location: String? = nil, position: Double? = nil, percent: Double? = nil,
                title: String? = nil, note: String? = nil, color: String? = nil) {
        self.kind = kind
        self.location = location
        self.position = position
        self.percent = percent
        self.title = title
        self.note = note
        self.color = color
    }
}

/// One field of an edit: leave it alone, or set it (nil clears it).
public enum Patch<Value: Equatable & Sendable>: Equatable, Sendable {
    case keep
    case set(Value?)
}

public struct BookmarkChange: Equatable, Sendable {
    public var title: Patch<String> = .keep
    public var note: Patch<String> = .keep
    public var color: Patch<String> = .keep
    public init(title: Patch<String> = .keep, note: Patch<String> = .keep, color: Patch<String> = .keep) {
        self.title = title
        self.note = note
        self.color = color
    }
}

/// Which book a bookmark call is about. bookdoe keys bookmarks by asset, Audiobookshelf by library item.
public struct BookRef: Equatable, Sendable {
    public var itemID: String
    public var assetID: String
    public init(itemID: String, assetID: String) {
        self.itemID = itemID
        self.assetID = assetID
    }
}
