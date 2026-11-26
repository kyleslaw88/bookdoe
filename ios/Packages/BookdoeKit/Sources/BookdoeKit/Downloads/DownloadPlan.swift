import Foundation

/// What to fetch for offline use and where to put it. The app's DownloadManager
/// executes the plan with a background URLSession; this type keeps the naming and
/// bookkeeping testable without one.
public struct DownloadPlan: Equatable, Sendable {
    public struct File: Equatable, Sendable {
        public var trackID: String
        public var remote: URL
        public var localRelativePath: String
        public var headers: [String: String]
        public var expectedBytes: Int64?
    }

    public var serverID: UUID
    public var itemID: String
    public var files: [File]

    public init(serverID: UUID, detail: MediaDetail, headers: [String: String]) {
        self.serverID = serverID
        self.itemID = detail.item.id
        var files: [File] = []
        for t in detail.tracks {
            let ext = Self.extensionFor(url: t.url, mime: t.mimeType)
            files.append(File(trackID: t.id, remote: t.url, localRelativePath: "\(serverID.uuidString)/\(Self.safe(detail.item.id))/\(String(format: "%03d", t.index))-\(Self.safe(t.id)).\(ext)",
                              headers: headers, expectedBytes: t.sizeBytes))
        }
        if let e = detail.ebookDownloadURL {
            files.append(File(trackID: "ebook", remote: e, localRelativePath: "\(serverID.uuidString)/\(Self.safe(detail.item.id))/book.\(detail.ebookFormat ?? Self.extensionFor(url: e, mime: nil))",
                              headers: headers, expectedBytes: nil))
        }
        self.files = files
    }

    public var totalExpectedBytes: Int64 { files.reduce(0) { $0 + ($1.expectedBytes ?? 0) } }

    static func safe(_ s: String) -> String {
        let allowed = Set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        let out = String(s.map { allowed.contains($0) ? $0 : "_" })
        return out.isEmpty ? "item" : String(out.prefix(80))
    }

    /// The extension to save a finished download under, from what the server said about the
    /// file (Content-Disposition filename, then Content-Type), then the planned one. AVPlayer
    /// picks the container by extension for local files, so a `.bin` audio file never plays.
    public static func resolvedExtension(suggestedFilename: String?, mimeType: String?, planned: String) -> String {
        if let name = suggestedFilename {
            let e = (name as NSString).pathExtension.lowercased()
            if !e.isEmpty, e.count <= 5, e.allSatisfy({ $0.isLetter || $0.isNumber }), e != "bin" { return e }
        }
        let mime = mimeType?.lowercased().split(separator: ";").first.map(String.init)
        switch mime {
        case "audio/x-m4b", "audio/m4b": return "m4b"
        case "audio/mp4", "audio/x-m4a", "audio/m4a", "video/mp4": return "m4a"
        case "audio/mpeg", "audio/mp3": return "mp3"
        case "audio/flac", "audio/x-flac": return "flac"
        case "audio/ogg", "application/ogg": return "ogg"
        case "audio/aac": return "aac"
        case "audio/wav", "audio/x-wav": return "wav"
        case "application/epub+zip": return "epub"
        case "application/pdf": return "pdf"
        default: return planned
        }
    }

    static func extensionFor(url: URL, mime: String?) -> String {
        let ext = url.pathExtension.lowercased()
        if !ext.isEmpty, ext.count <= 5, ext.allSatisfy({ $0.isLetter || $0.isNumber }) { return ext }
        switch mime?.lowercased() {
        case "audio/mp4", "audio/x-m4b", "audio/m4b": return "m4b"
        case "audio/mpeg", "audio/mp3": return "mp3"
        case "audio/flac": return "flac"
        case "audio/ogg": return "ogg"
        case "audio/aac": return "aac"
        default: return "bin"
        }
    }
}

/// Index of completed downloads, persisted as JSON.
public struct DownloadedItem: Codable, Equatable, Identifiable, Sendable {
    public var id: String { "\(serverID.uuidString):\(itemID)" }
    public var serverID: UUID
    public var itemID: String
    public var title: String
    public var authors: [String]
    public var kind: MediaKind
    public var assetID: String
    /// trackID -> relative path under the downloads root, in track order.
    public var tracks: [DownloadedTrack]
    public var chapters: [Chapter]
    public var durationSeconds: Double?
    public var bytes: Int64
    public var completedAt: Date

    public init(serverID: UUID, detail: MediaDetail, tracks: [DownloadedTrack], bytes: Int64, completedAt: Date = Date()) {
        self.serverID = serverID
        self.itemID = detail.item.id
        self.title = detail.item.title
        self.authors = detail.item.authors
        self.kind = detail.item.kind
        self.assetID = detail.assetID
        self.tracks = tracks
        self.chapters = detail.chapters
        self.durationSeconds = detail.item.durationSeconds
        self.bytes = bytes
        self.completedAt = completedAt
    }

    /// A detail whose track URLs point at the local files, so the player code path
    /// is the same online and offline.
    public func detail(root: URL) -> MediaDetail {
        var offset = 0.0
        let ts = tracks.enumerated().map { i, t -> Track in
            let tr = Track(id: t.trackID, index: i, title: t.title, url: root.appendingPathComponent(t.relativePath), durationSeconds: t.durationSeconds, startOffset: offset)
            offset += t.durationSeconds ?? 0
            return tr
        }
        let item = MediaItem(id: itemID, title: title, authors: authors, kind: kind, durationSeconds: durationSeconds)
        return MediaDetail(item: item, tracks: ts, chapters: chapters, assetID: assetID)
    }
}

public struct DownloadedTrack: Codable, Equatable, Sendable {
    public var trackID: String
    public var title: String
    public var relativePath: String
    public var durationSeconds: Double?

    public init(trackID: String, title: String, relativePath: String, durationSeconds: Double?) {
        self.trackID = trackID
        self.title = title
        self.relativePath = relativePath
        self.durationSeconds = durationSeconds
    }
}

public final class DownloadIndex: @unchecked Sendable {
    public private(set) var items: [DownloadedItem]
    private let fileURL: URL?
    private let lock = NSLock()

    public init(fileURL: URL? = nil) {
        self.fileURL = fileURL
        if let fileURL, let data = try? Data(contentsOf: fileURL), let decoded = try? JSONDecoder().decode([DownloadedItem].self, from: data) {
            items = decoded
        } else {
            items = []
        }
    }

    public func item(serverID: UUID, itemID: String) -> DownloadedItem? {
        lock.lock(); defer { lock.unlock() }
        return items.first { $0.serverID == serverID && $0.itemID == itemID }
    }

    public func upsert(_ item: DownloadedItem) {
        lock.lock(); defer { lock.unlock() }
        items.removeAll { $0.id == item.id }
        items.append(item)
        persist()
    }

    public func remove(serverID: UUID, itemID: String) -> DownloadedItem? {
        lock.lock(); defer { lock.unlock() }
        guard let i = items.firstIndex(where: { $0.serverID == serverID && $0.itemID == itemID }) else { return nil }
        let removed = items.remove(at: i)
        persist()
        return removed
    }

    private func persist() {
        guard let fileURL, let data = try? JSONEncoder().encode(items) else { return }
        try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        #if os(iOS)
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try? data.write(to: fileURL, options: [.atomic])
        #endif
    }
}
