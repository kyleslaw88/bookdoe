import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// One abstraction over bookdoe, Jellyfin and Audiobookshelf. Each provider holds
/// a `ServerRecord`, an `HTTPClient` and a `CredentialStore`; it never caches a
/// token outside the store so a credential change is visible everywhere at once.
public protocol MediaProvider: AnyObject, Sendable {
    var server: ServerRecord { get }
    var kind: ServerKind { get }

    /// Unauthenticated facts: name, version, which sign-in methods exist.
    func probe() async throws -> ServerInfo

    /// Username and password sign-in. Returns the credential and an updated server
    /// record (user id, default library). The caller persists both.
    func login(username: String, password: String, deviceName: String) async throws -> AuthResult

    /// Libraries to choose from (Jellyfin views, Audiobookshelf libraries). bookdoe
    /// returns one entry.
    func libraries() async throws -> [LibraryRef]

    func browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int) async throws -> LibraryPage

    /// In-progress items, most recent first.
    func continueListening() async throws -> [MediaItem]

    func detail(id: String) async throws -> MediaDetail

    /// Tell the server playback starts; the returned session is passed to every
    /// progress report until `stop`.
    func startPlayback(_ detail: MediaDetail) async throws -> PlaybackSession

    func reportProgress(_ update: ProgressUpdate, session: PlaybackSession?) async throws -> ProgressReport

    func stopPlayback(_ update: ProgressUpdate, session: PlaybackSession?) async throws

    /// Headers an AVPlayer or download task must attach to media URLs.
    func mediaHeaders() throws -> [String: String]

    /// Sign out server-side where supported and forget the credential.
    func logout() async throws

    /// Mark a book finished (it drops out of Continue) or unread (its position is
    /// forgotten). `detail`, when the caller has it, lets a provider park the position on
    /// the end of the book. Providers that cannot do this throw `notSupported`.
    func setFinished(_ finished: Bool, assetID: String, detail: MediaDetail?) async throws

    /// Bookmarks (and, where the server has them, highlights) of one book.
    func bookmarks(for book: BookRef) async throws -> [Bookmark]
    func addBookmark(_ new: NewBookmark, to book: BookRef) async throws -> Bookmark
    func updateBookmark(_ bookmark: Bookmark, change: BookmarkChange, in book: BookRef) async throws -> Bookmark
    func deleteBookmark(_ bookmark: Bookmark, in book: BookRef) async throws
    /// Whether this server can store highlights with notes (ebook markup).
    func supportsHighlights() async -> Bool
}

public extension MediaProvider {
    func bookmarks(for book: BookRef) async throws -> [Bookmark] { throw APIError.notSupported("This server type does not store bookmarks from the app yet.") }
    func addBookmark(_ new: NewBookmark, to book: BookRef) async throws -> Bookmark { throw APIError.notSupported("This server type does not store bookmarks from the app yet.") }
    func updateBookmark(_ bookmark: Bookmark, change: BookmarkChange, in book: BookRef) async throws -> Bookmark { throw APIError.notSupported("This server type does not store bookmarks from the app yet.") }
    func deleteBookmark(_ bookmark: Bookmark, in book: BookRef) async throws { throw APIError.notSupported("This server type does not store bookmarks from the app yet.") }
    func supportsHighlights() async -> Bool { false }

    func setFinished(_ finished: Bool, assetID: String, detail: MediaDetail?) async throws {
        throw APIError.notSupported("This server type cannot mark books read or unread from the app yet.")
    }

    func mediaRequest(for url: URL) throws -> URLRequest {
        var req = URLRequest(url: url)
        for (k, v) in try mediaHeaders() { req.setValue(v, forHTTPHeaderField: k) }
        return req
    }
}

/// Builds the right provider for a saved server.
public struct ProviderFactory: Sendable {
    public var client: HTTPClient
    public var credentials: CredentialStore
    public var appVersion: String

    public init(client: HTTPClient, credentials: CredentialStore, appVersion: String = "1.0") {
        self.client = client
        self.credentials = credentials
        self.appVersion = appVersion
    }

    public func make(_ server: ServerRecord) -> MediaProvider {
        switch server.kind {
        case .bookdoe: return BookdoeProvider(server: server, client: client, credentials: credentials, appVersion: appVersion)
        case .jellyfin: return JellyfinProvider(server: server, client: client, credentials: credentials, appVersion: appVersion)
        case .audiobookshelf: return AudiobookshelfProvider(server: server, client: client, credentials: credentials, appVersion: appVersion)
        }
    }
}

/// Shared plumbing for the three providers.
public class BaseProvider: @unchecked Sendable {
    public private(set) var server: ServerRecord
    let client: HTTPClient
    let credentials: CredentialStore
    let appVersion: String
    private let lock = NSLock()

    public init(server: ServerRecord, client: HTTPClient, credentials: CredentialStore, appVersion: String) {
        self.server = server
        self.client = client
        self.credentials = credentials
        self.appVersion = appVersion
    }

    func updateServer(_ change: (inout ServerRecord) -> Void) {
        lock.lock(); defer { lock.unlock() }
        change(&server)
    }

    func credential() throws -> Credential {
        guard let c = try credentials.credential(for: server.id) else { throw APIError.unauthorized }
        return c
    }

    func url(_ path: String, _ query: [String: String?] = [:]) -> URL {
        server.baseURL.appending(path: path, query: query)
    }

    /// Send and map non-2xx to APIError (404 included, so callers see `.notFound`).
    func send(_ request: HTTPRequest, allow: Set<Int> = []) async throws -> HTTPResponse {
        let resp = try await client.send(request)
        if resp.isSuccess || allow.contains(resp.status) { return resp }
        throw APIError.from(resp)
    }
}
