import Foundation

/// The load state of one CarPlay list tab.
public enum CarPlayLoadState: Sendable, Equatable {
    case loading
    case ready(items: [CarPlayBookRow])
    case empty
    case offline
    case error(message: String)
}

/// A single book for CarPlay display.
public struct CarPlayBookRow: Sendable, Equatable {
    /// Media id used to start playback when the row is selected.
    public var id: String?
    public var title: String
    public var subtitle: String?
    /// Cover art location; loaded asynchronously by the scene delegate.
    public var coverURL: URL?

    public init(id: String? = nil, title: String, subtitle: String? = nil, coverURL: URL? = nil) {
        self.id = id
        self.title = title
        self.subtitle = subtitle
        self.coverURL = coverURL
    }
}

/// A row the scene delegate turns into a `CPListItem(text:detailText:)`.
public struct CarPlayRowModel: Sendable, Equatable {
    public var id: String?
    public var text: String
    public var detailText: String?
    public var coverURL: URL?
    /// Placeholder rows only report status; they are not book rows.
    public var isPlaceholder: Bool

    public init(id: String? = nil, text: String, detailText: String? = nil, coverURL: URL? = nil,
                isPlaceholder: Bool = false) {
        self.id = id
        self.text = text
        self.detailText = detailText
        self.coverURL = coverURL
        self.isPlaceholder = isPlaceholder
    }
}

/// A section the scene delegate turns into a `CPListSection`.
public struct CarPlaySectionModel: Sendable, Equatable {
    public var header: String?
    public var items: [CarPlayRowModel]

    public init(header: String? = nil, items: [CarPlayRowModel]) {
        self.header = header
        self.items = items
    }
}

/// Pure mapper from a `CarPlayLoadState` to section models (no CarPlay import).
public enum CarPlayStateMapper {

    public static let loadingText = "Loading\u{2026}"
    public static let emptyText = "No books in library"
    public static let offlineText = "Offline \u{2014} tap to retry"

    /// `header` titles the section of book rows in the `.ready` state; `emptyText`
    /// is the placeholder for `.empty` (and `.ready` with no items).
    public static func sections(for state: CarPlayLoadState, header: String? = nil,
                                emptyText: String = emptyText) -> [CarPlaySectionModel] {
        switch state {
        case .loading:
            return placeholder(loadingText)
        case .ready(let items):
            guard !items.isEmpty else { return placeholder(emptyText) }
            let rows = items.map { CarPlayRowModel(id: $0.id, text: $0.title, detailText: $0.subtitle, coverURL: $0.coverURL) }
            return [CarPlaySectionModel(header: header, items: rows)]
        case .empty:
            return placeholder(emptyText)
        case .offline:
            return placeholder(offlineText)
        case .error(let message):
            return placeholder(message)
        }
    }

    /// The state to show when a load fails: network failures are `.offline`
    /// (retryable), everything else is `.error` with the error's description.
    public static func state(for error: Error) -> CarPlayLoadState {
        if case APIError.network = error { return .offline }
        if error is URLError { return .offline }
        let message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        return .error(message: message)
    }

    private static func placeholder(_ text: String) -> [CarPlaySectionModel] {
        [CarPlaySectionModel(items: [CarPlayRowModel(text: text, isPlaceholder: true)])]
    }
}
