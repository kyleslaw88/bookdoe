@preconcurrency import Foundation

/// Which server API a saved server speaks.
public enum ServerKind: String, Codable, CaseIterable, Sendable {
    case bookdoe
    case jellyfin
    case audiobookshelf

    /// Servers saved by builds before the rename carry the old raw value; read it as `.bookdoe`.
    /// New saves write `bookdoe`.
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        if raw == "alexandria" { self = .bookdoe; return }
        guard let kind = ServerKind(rawValue: raw) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "unknown server kind \(raw)"))
        }
        self = kind
    }

    public var displayName: String {
        switch self {
        case .bookdoe: return "bookdoe"
        case .jellyfin: return "Jellyfin"
        case .audiobookshelf: return "Audiobookshelf"
        }
    }
}

public enum MediaKind: String, Codable, CaseIterable, Sendable {
    case audiobook
    case ebook
}

/// A saved server. Credentials are not stored here (see CredentialStore).
public struct ServerRecord: Codable, Identifiable, Hashable, Sendable {
    public var id: UUID
    public var name: String
    public var kind: ServerKind
    public var baseURL: URL
    public var username: String
    /// Server-side user id (Jellyfin, Audiobookshelf) once known.
    public var userID: String?
    /// Selected library (Jellyfin view id, Audiobookshelf library id). bookdoe has one library.
    public var libraryID: String?
    /// Stable per-install identifier sent as the device id.
    public var deviceID: String
    public var addedAt: Date

    public init(id: UUID = UUID(), name: String, kind: ServerKind, baseURL: URL, username: String,
                userID: String? = nil, libraryID: String? = nil, deviceID: String = UUID().uuidString, addedAt: Date = Date()) {
        self.id = id
        self.name = name
        self.kind = kind
        self.baseURL = baseURL.standardized
        self.username = username
        self.userID = userID
        self.libraryID = libraryID
        self.deviceID = deviceID
        self.addedAt = addedAt
    }
}

/// What the keychain holds per server.
public struct Credential: Codable, Equatable, Sendable {
    public var accessToken: String
    public var refreshToken: String?
    public var expiresAt: Date?

    public init(accessToken: String, refreshToken: String? = nil, expiresAt: Date? = nil) {
        self.accessToken = accessToken
        self.refreshToken = refreshToken
        self.expiresAt = expiresAt
    }
}

public struct ServerInfo: Equatable, Sendable {
    public var name: String
    public var version: String?
    public var supportsPasswordLogin: Bool
    public var supportsDevicePairing: Bool
    public var setupRequired: Bool
    /// The server offers single sign-on (Audiobookshelf's OpenID Connect) and what its button says.
    public var supportsOpenID: Bool
    public var openIDButtonText: String?
    /// The server's `discover_chat` capability: true only when its AI is on and reachable. An older server omits it.
    public var supportsDiscoverChat: Bool

    public init(name: String, version: String? = nil, supportsPasswordLogin: Bool, supportsDevicePairing: Bool = false, setupRequired: Bool = false,
                supportsOpenID: Bool = false, openIDButtonText: String? = nil, supportsDiscoverChat: Bool = false) {
        self.supportsDiscoverChat = supportsDiscoverChat
        self.supportsOpenID = supportsOpenID
        self.openIDButtonText = openIDButtonText
        self.name = name
        self.version = version
        self.supportsPasswordLogin = supportsPasswordLogin
        self.supportsDevicePairing = supportsDevicePairing
        self.setupRequired = setupRequired
    }
}

public struct LibraryRef: Identifiable, Equatable, Sendable {
    public var id: String
    public var name: String
    public var kind: MediaKind?

    public init(id: String, name: String, kind: MediaKind? = nil) {
        self.id = id
        self.name = name
        self.kind = kind
    }
}

public struct MediaItem: Identifiable, Codable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var subtitle: String?
    public var authors: [String]
    public var narrators: [String]
    public var series: String?
    public var seriesSequence: String?
    public var kind: MediaKind
    public var durationSeconds: Double?
    public var coverURL: URL?
    /// 0...1 when the server knows a position.
    public var progress: Double?
    public var updatedAt: Date?
    /// The asset a Continue row is about (bookdoe keeps one position per asset, so a book
    /// can be in Continue once as audio and once as ebook). Nil for catalogue rows.
    public var assetID: String?
    /// True for a podcast episode row in Continue (`id` is then the episode id and `showID` is set).
    public var isEpisode: Bool
    public var showID: String?

    public init(id: String, title: String, subtitle: String? = nil, authors: [String] = [], narrators: [String] = [],
                series: String? = nil, seriesSequence: String? = nil, kind: MediaKind, durationSeconds: Double? = nil,
                coverURL: URL? = nil, progress: Double? = nil, updatedAt: Date? = nil, assetID: String? = nil,
                isEpisode: Bool = false, showID: String? = nil) {
        self.assetID = assetID
        self.isEpisode = isEpisode
        self.showID = showID
        self.id = id
        self.title = title
        self.subtitle = subtitle
        self.authors = authors
        self.narrators = narrators
        self.series = series
        self.seriesSequence = seriesSequence
        self.kind = kind
        self.durationSeconds = durationSeconds
        self.coverURL = coverURL
        self.progress = progress
        self.updatedAt = updatedAt
    }

    public var authorLine: String { authors.joined(separator: ", ") }
}

public struct Track: Identifiable, Codable, Equatable, Sendable {
    public var id: String
    public var index: Int
    public var title: String
    public var url: URL
    public var durationSeconds: Double?
    /// Seconds from the start of the book to the start of this track.
    public var startOffset: Double
    public var mimeType: String?
    public var sizeBytes: Int64?

    public init(id: String, index: Int, title: String, url: URL, durationSeconds: Double? = nil, startOffset: Double = 0,
                mimeType: String? = nil, sizeBytes: Int64? = nil) {
        self.id = id
        self.index = index
        self.title = title
        self.url = url
        self.durationSeconds = durationSeconds
        self.startOffset = startOffset
        self.mimeType = mimeType
        self.sizeBytes = sizeBytes
    }
}

public struct Chapter: Codable, Equatable, Sendable {
    public var title: String
    public var start: Double
    public var end: Double?

    public init(title: String, start: Double, end: Double? = nil) {
        self.title = title
        self.start = start
        self.end = end
    }
}

/// A listening or reading position as the server last stored it.
public struct PlaybackPosition: Codable, Equatable, Sendable {
    public var trackID: String?
    public var trackIndex: Int
    public var positionInTrack: Double
    /// Seconds from the start of the whole book.
    public var absolutePosition: Double
    public var percent: Double?
    public var updatedAt: Date?
    /// Ebook location (EPUB CFI or PDF page) when the item is an ebook.
    public var location: String?

    public init(trackID: String? = nil, trackIndex: Int = 0, positionInTrack: Double = 0, absolutePosition: Double = 0,
                percent: Double? = nil, updatedAt: Date? = nil, location: String? = nil) {
        self.trackID = trackID
        self.trackIndex = trackIndex
        self.positionInTrack = positionInTrack
        self.absolutePosition = absolutePosition
        self.percent = percent
        self.updatedAt = updatedAt
        self.location = location
    }
}

public struct MediaDetail: Equatable, Sendable {
    public var item: MediaItem
    public var description: String?
    public var tracks: [Track]
    public var chapters: [Chapter]
    public var position: PlaybackPosition?
    /// Provider-specific asset identity used when reporting progress (bookdoe asset id,
    /// Audiobookshelf library item id, Jellyfin item id).
    public var assetID: String
    public var ebookDownloadURL: URL?
    public var ebookFormat: String?
    /// The EPUB/PDF asset of a work that also has audio (`assetID` and `position` then belong to the audiobook, so
    /// listening and reading progress stay separate). Nil for an ebook-only work, whose `assetID` is the ebook.
    public var ebookAssetID: String?
    public var ebookPosition: PlaybackPosition?

    public init(item: MediaItem, description: String? = nil, tracks: [Track] = [], chapters: [Chapter] = [],
                position: PlaybackPosition? = nil, assetID: String, ebookDownloadURL: URL? = nil, ebookFormat: String? = nil,
                ebookAssetID: String? = nil, ebookPosition: PlaybackPosition? = nil) {
        self.item = item
        self.description = description
        self.tracks = tracks
        self.chapters = chapters
        self.position = position
        self.assetID = assetID
        self.ebookDownloadURL = ebookDownloadURL
        self.ebookFormat = ebookFormat
        self.ebookAssetID = ebookAssetID
        self.ebookPosition = ebookPosition
    }

    public var hasAudio: Bool { !tracks.isEmpty }
    public var hasEbook: Bool { ebookDownloadURL != nil }
    /// The asset reading progress and ebook bookmarks belong to.
    public var readingAssetID: String { ebookAssetID ?? assetID }
    /// Where the reader last was: the separate ebook position, or the only position of an ebook-only work.
    public var readingPosition: PlaybackPosition? { ebookPosition ?? (hasAudio ? nil : position) }

    public var totalDuration: Double {
        if let d = item.durationSeconds, d > 0 { return d }
        return tracks.reduce(0) { $0 + ($1.durationSeconds ?? 0) }
    }

    /// This detail with the playable item's own duration filled in for a track whose server
    /// duration is missing or 0. Known durations are never changed. When the item has no overall
    /// duration either, later tracks' offsets (derived from the durations) shift with it.
    public func learningDuration(_ seconds: Double, ofTrack index: Int) -> MediaDetail {
        guard seconds.isFinite, seconds > 0,
              let i = tracks.firstIndex(where: { $0.index == index }),
              (tracks[i].durationSeconds ?? 0) <= 0 else { return self }
        var d = self
        d.tracks[i].durationSeconds = seconds
        if (item.durationSeconds ?? 0) <= 0 {
            for j in tracks.indices where j > i { d.tracks[j].startOffset += seconds }
        }
        return d
    }

    /// Where a skip of `delta` seconds from `position` lands. Relative to the current time; it is
    /// capped near the end only when the total is known, so an unknown total never means 0.
    public func skipTarget(from position: Double, by delta: Double) -> Double {
        let target = max(0, position + delta)
        let total = totalDuration
        return total > 0 ? min(target, max(0, total - 1)) : target
    }

    /// Chapters derived from track boundaries when the server supplied none.
    public var effectiveChapters: [Chapter] {
        if !chapters.isEmpty { return chapters }
        return tracks.map { t in Chapter(title: t.title, start: t.startOffset, end: t.durationSeconds.map { d in t.startOffset + d }) }
    }

    /// The track and in-track offset for an absolute position.
    public func locate(absolute: Double) -> (index: Int, offset: Double) {
        guard !tracks.isEmpty else { return (0, max(0, absolute)) }
        for t in tracks.reversed() where absolute >= t.startOffset {
            return (t.index, max(0, absolute - t.startOffset))
        }
        return (tracks[0].index, 0)
    }
}

public struct LibraryPage: Equatable, Sendable {
    public var items: [MediaItem]
    public var total: Int
    public var offset: Int
    public var limit: Int

    public init(items: [MediaItem], total: Int, offset: Int, limit: Int) {
        self.items = items
        self.total = total
        self.offset = offset
        self.limit = limit
    }

    public var nextOffset: Int? { offset + items.count < total && !items.isEmpty ? offset + items.count : nil }
}

public enum LibrarySort: String, CaseIterable, Sendable {
    case title
    case added
    case updated
}

/// One playback session as the server sees it (Audiobookshelf session id, Jellyfin
/// PlaySessionId). bookdoe needs none.
public struct PlaybackSession: Equatable, Sendable {
    public var sessionID: String?
    public var startedAt: Date

    public init(sessionID: String?, startedAt: Date = Date()) {
        self.sessionID = sessionID
        self.startedAt = startedAt
    }
}

/// A progress report queued for the server.
public struct ProgressUpdate: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var serverID: UUID
    public var itemID: String
    public var assetID: String
    public var trackID: String?
    public var trackIndex: Int
    public var positionInTrack: Double
    public var absolutePosition: Double
    public var duration: Double?
    public var percent: Double?
    public var location: String?
    public var clientUpdatedAt: Date
    public var sessionID: String?
    public var isFinished: Bool
    /// Seconds listened since the previous report (Audiobookshelf timeListened).
    public var listened: Double

    public init(id: UUID = UUID(), serverID: UUID, itemID: String, assetID: String, trackID: String? = nil, trackIndex: Int = 0,
                positionInTrack: Double = 0, absolutePosition: Double = 0, duration: Double? = nil, percent: Double? = nil,
                location: String? = nil, clientUpdatedAt: Date = Date(), sessionID: String? = nil, isFinished: Bool = false, listened: Double = 0) {
        self.id = id
        self.serverID = serverID
        self.itemID = itemID
        self.assetID = assetID
        self.trackID = trackID
        self.trackIndex = trackIndex
        self.positionInTrack = positionInTrack
        self.absolutePosition = absolutePosition
        self.duration = duration
        self.percent = percent
        self.location = location
        self.clientUpdatedAt = clientUpdatedAt
        self.sessionID = sessionID
        self.isFinished = isFinished
        self.listened = listened
    }
}

/// What the server answered to a progress report.
public struct ProgressReport: Equatable, Sendable {
    public var applied: Bool
    /// When the server rejected the update as stale it returns its own position.
    public var serverPosition: PlaybackPosition?

    public init(applied: Bool, serverPosition: PlaybackPosition? = nil) {
        self.applied = applied
        self.serverPosition = serverPosition
    }
}

public struct AuthResult: Equatable, Sendable {
    public var credential: Credential
    public var server: ServerRecord
    public var displayName: String?

    public init(credential: Credential, server: ServerRecord, displayName: String? = nil) {
        self.credential = credential
        self.server = server
        self.displayName = displayName
    }
}

/// Device-code pairing state (bookdoe).
public struct DevicePairing: Equatable, Sendable {
    public var deviceCode: String
    public var userCode: String
    public var verificationURL: URL?
    public var interval: Int
    public var expiresIn: Int

    public init(deviceCode: String, userCode: String, verificationURL: URL?, interval: Int, expiresIn: Int) {
        self.deviceCode = deviceCode
        self.userCode = userCode
        self.verificationURL = verificationURL
        self.interval = interval
        self.expiresIn = expiresIn
    }
}

public enum PairingPoll: Equatable, Sendable {
    case pending
    case slowDown
    case approved(AuthResult)
    case denied
    case expired
}
