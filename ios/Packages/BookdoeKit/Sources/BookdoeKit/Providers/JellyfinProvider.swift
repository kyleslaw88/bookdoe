import Foundation

/// Jellyfin 10.9+ (routes from docs/COMPAT_PLAYERS.md section 2.4; the ones marked
/// "capture" there were verified against a 12.x demo server, the audiobook item
/// shapes are assumptions, see docs/IOS_APP.md).
///
/// Positions are 100 ns ticks. Audiobook libraries (`CollectionType == "books"`)
/// hold `AudioBook` items, one per file; a multi-file book appears as a folder
/// (`Folder`/`MusicAlbum`) whose children carry `IndexNumber`. Both shapes map onto
/// one `MediaDetail` with ordered tracks.
public final class JellyfinProvider: BaseProvider, MediaProvider {
    public var kind: ServerKind { .jellyfin }
    static let ticksPerSecond = 10_000_000.0

    private var clientName: String { "bookdoe" }

    private func authHeader(token: String?) -> String {
        var parts = ["Client=\"\(clientName)\"", "Device=\"iOS\"", "DeviceId=\"\(server.deviceID)\"", "Version=\"\(appVersion)\""]
        if let token { parts.append("Token=\"\(token)\"") }
        return "MediaBrowser " + parts.joined(separator: ", ")
    }

    private func authed(_ req: HTTPRequest) throws -> HTTPRequest {
        var r = req
        r.headers["Authorization"] = authHeader(token: try credential().accessToken)
        return r
    }

    private func get(_ path: String, _ query: [String: String?] = [:]) async throws -> [String: Any] {
        try await send(try authed(HTTPRequest(url: url(path, query)))).jsonObject()
    }

    private func userID() throws -> String {
        guard let u = server.userID else { throw APIError.unauthorized }
        return u
    }

    // MARK: probe and auth

    public func probe() async throws -> ServerInfo {
        let obj = try await send(HTTPRequest(url: url("/System/Info/Public"))).jsonObject()
        return ServerInfo(name: JSON.string(obj, "ServerName") ?? "Jellyfin", version: JSON.string(obj, "Version"), supportsPasswordLogin: true)
    }

    public func login(username: String, password: String, deviceName: String) async throws -> AuthResult {
        var req = try HTTPRequest.json(.post, url("/Users/AuthenticateByName"), body: ["Username": username, "Pw": password])
        req.headers["Authorization"] = authHeader(token: nil)
        let obj = try await send(req).jsonObject()
        guard let token = JSON.string(obj, "AccessToken"), let user = JSON.object(obj, "User"), let uid = JSON.string(user, "Id") else {
            throw APIError.decoding("AuthenticateByName answer")
        }
        updateServer { s in
            s.userID = uid
            s.username = JSON.string(user, "Name") ?? username
        }
        let cred = Credential(accessToken: token)
        try credentials.save(cred, for: server.id)
        // Pick the first books library when none is chosen yet.
        if server.libraryID == nil, let first = try? await libraries().first {
            updateServer { $0.libraryID = first.id }
        }
        return AuthResult(credential: cred, server: server, displayName: JSON.string(user, "Name"))
    }

    public func logout() async throws {
        if let c = try? credential() {
            var req = HTTPRequest(method: .post, url: url("/Sessions/Logout"))
            req.headers["Authorization"] = authHeader(token: c.accessToken)
            _ = try? await client.send(req)
        }
        try credentials.remove(for: server.id)
    }

    public func mediaHeaders() throws -> [String: String] {
        ["Authorization": authHeader(token: try credential().accessToken)]
    }

    // MARK: library

    public func libraries() async throws -> [LibraryRef] {
        let obj = try await get("/UserViews", ["userId": try userID()])
        let views = JSON.array(obj, "Items")
        let books = views.filter { JSON.string($0, "CollectionType") == "books" }
        return (books.isEmpty ? views : books).compactMap { v in
            guard let id = JSON.string(v, "Id") else { return nil }
            return LibraryRef(id: id, name: JSON.string(v, "Name") ?? id, kind: JSON.string(v, "CollectionType") == "books" ? .audiobook : nil)
        }
    }

    private func coverURL(_ obj: [String: Any]) -> URL? {
        guard let id = JSON.string(obj, "Id") else { return nil }
        let tags = JSON.object(obj, "ImageTags") ?? [:]
        guard tags["Primary"] != nil || JSON.string(obj, "AlbumPrimaryImageTag") != nil else { return nil }
        return url("/Items/\(id)/Images/Primary", ["maxWidth": "384", "tag": JSON.string(tags, "Primary")])
    }

    private func item(_ o: [String: Any]) -> MediaItem {
        let type = JSON.string(o, "Type") ?? ""
        let kind: MediaKind = type == "Book" ? .ebook : .audiobook
        let artists = (o["Artists"] as? [String]) ?? (o["AlbumArtists"] as? [[String: Any]])?.compactMap { JSON.string($0, "Name") } ?? []
        let ticks = JSON.double(o, "RunTimeTicks")
        let userData = JSON.object(o, "UserData") ?? [:]
        var progress: Double?
        if let ticks, ticks > 0, let pos = JSON.double(userData, "PlaybackPositionTicks"), pos > 0 { progress = pos / ticks }
        if JSON.bool(userData, "Played") == true { progress = 1 }
        return MediaItem(id: JSON.string(o, "Id") ?? "", title: JSON.string(o, "Name") ?? "", subtitle: JSON.string(o, "Album"),
                         authors: artists, narrators: [], series: JSON.string(o, "SeriesName"), seriesSequence: JSON.int(o, "IndexNumber").map(String.init),
                         kind: kind, durationSeconds: ticks.map { $0 / Self.ticksPerSecond }, coverURL: coverURL(o), progress: progress,
                         updatedAt: JSON.date(iso: JSON.string(o, "DateLastSaved") ?? JSON.string(o, "DateCreated")))
    }

    private static let itemTypes = "AudioBook,Audio,MusicAlbum,Folder,Book"
    private static let fields = "MediaSources,Overview,ChildCount,DateCreated,SeriesName,ParentId"

    public func browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int) async throws -> LibraryPage {
        let sortBy: String
        switch sort {
        case .title: sortBy = "SortName"
        case .added: sortBy = "DateCreated"
        case .updated: sortBy = "DateLastSaved"
        }
        var q: [String: String?] = ["userId": try userID(), "parentId": server.libraryID, "recursive": "true",
                                    "includeItemTypes": kind == .ebook ? "Book" : Self.itemTypes, "fields": Self.fields,
                                    "sortBy": sortBy, "sortOrder": sort == .title ? "Ascending" : "Descending",
                                    "limit": String(limit), "startIndex": String(offset)]
        if let query, !query.isEmpty { q["searchTerm"] = query }
        let obj = try await get("/Items", q)
        // A book is the folder (or album) that directly holds the audio files. The library
        // root and pure containers (only subfolders) are not books, and a book's per-file
        // children are hidden (they come back from `detail`). Loose files stay as items.
        let rows = JSON.array(obj, "Items")
        func isFolder(_ o: [String: Any]) -> Bool { ["Folder", "MusicAlbum"].contains(JSON.string(o, "Type") ?? "") }
        let audioParents = Set(rows.filter { !isFolder($0) }.compactMap { JSON.string($0, "ParentId") })
        let folderParents = Set(rows.filter(isFolder).compactMap { JSON.string($0, "ParentId") })
        func isBook(_ o: [String: Any]) -> Bool {
            guard isFolder(o), let id = JSON.string(o, "Id") else { return false }
            if id == server.libraryID || JSON.int(o, "ChildCount") == 0 { return false }
            if audioParents.contains(id) || JSON.string(o, "Type") == "MusicAlbum" { return true }
            return !folderParents.contains(id)
        }
        let bookIDs = Set(rows.filter(isBook).compactMap { JSON.string($0, "Id") })
        let visible = rows.filter { isFolder($0) ? isBook($0) : !(JSON.string($0, "ParentId").map(bookIDs.contains) ?? false) }
        return LibraryPage(items: visible.map(item), total: JSON.int(obj, "TotalRecordCount") ?? visible.count, offset: offset, limit: limit)
    }

    public func continueListening() async throws -> [MediaItem] {
        let obj = try await get("/UserItems/Resume", ["userId": try userID(), "parentId": server.libraryID, "mediaTypes": "Audio", "fields": Self.fields, "limit": "20"])
        return JSON.array(obj, "Items").map(item)
    }

    private func streamURL(itemID: String, container: String?) -> URL {
        url("/Audio/\(itemID)/universal", ["userId": server.userID, "deviceId": server.deviceID,
                                            "container": "m4b,m4a,mp3,aac,flac,ogg,opus,wav", "audioCodec": "aac",
                                            "transcodingContainer": "ts", "transcodingProtocol": "hls",
                                            "api_key": (try? credential().accessToken)])
    }

    public func detail(id: String) async throws -> MediaDetail {
        let o = try await get("/Items/\(id)", ["userId": try userID(), "fields": Self.fields])
        var m = item(o)
        var fileItems: [[String: Any]] = [o]
        if ["Folder", "MusicAlbum"].contains(JSON.string(o, "Type") ?? "") {
            let kids = try await get("/Items", ["userId": try userID(), "parentId": id, "includeItemTypes": "AudioBook,Audio", "fields": Self.fields,
                                                 "sortBy": "ParentIndexNumber,IndexNumber,SortName", "limit": "500"])
            fileItems = JSON.array(kids, "Items")
        }
        var tracks: [Track] = []
        var offset = 0.0
        for (i, f) in fileItems.enumerated() {
            guard let fid = JSON.string(f, "Id") else { continue }
            let d = JSON.double(f, "RunTimeTicks").map { $0 / Self.ticksPerSecond }
            let container = JSON.array(f, "MediaSources").first.flatMap { JSON.string($0, "Container") }
            tracks.append(Track(id: fid, index: i, title: JSON.string(f, "Name") ?? "Part \(i + 1)", url: streamURL(itemID: fid, container: container),
                                durationSeconds: d, startOffset: offset, mimeType: container.map { "audio/\($0)" }))
            offset += d ?? 0
        }
        if m.durationSeconds == nil || fileItems.count > 1 { m.durationSeconds = offset > 0 ? offset : m.durationSeconds }
        let chapters = JSON.array(o, "Chapters").map { Chapter(title: JSON.string($0, "Name") ?? "", start: (JSON.double($0, "StartPositionTicks") ?? 0) / Self.ticksPerSecond) }
        var detail = MediaDetail(item: m, description: JSON.string(o, "Overview"), tracks: tracks, chapters: chapters, assetID: id)
        // Position: a single item carries UserData; a folder's position is the first
        // child with a non-zero position (Jellyfin stores it per file).
        if let pos = try? await position(for: detail, items: fileItems) { detail.position = pos }
        return detail
    }

    private func position(for detail: MediaDetail, items: [[String: Any]]) async throws -> PlaybackPosition? {
        for (i, f) in items.enumerated() {
            let ud = JSON.object(f, "UserData") ?? [:]
            if let ticks = JSON.double(ud, "PlaybackPositionTicks"), ticks > 0, let t = detail.tracks.first(where: { $0.index == i }) {
                let inTrack = ticks / Self.ticksPerSecond
                let total = detail.totalDuration
                return PlaybackPosition(trackID: t.id, trackIndex: i, positionInTrack: inTrack, absolutePosition: t.startOffset + inTrack,
                                        percent: total > 0 ? (t.startOffset + inTrack) / total : nil,
                                        updatedAt: JSON.date(iso: JSON.string(ud, "LastPlayedDate")))
            }
        }
        return nil
    }

    // MARK: playback reporting

    public func startPlayback(_ detail: MediaDetail) async throws -> PlaybackSession {
        let sessionID = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        let track = detail.tracks.first { $0.index == (detail.position?.trackIndex ?? 0) } ?? detail.tracks.first
        let body: [String: Any] = ["ItemId": track?.id ?? detail.assetID, "PlaySessionId": sessionID, "CanSeek": true,
                                   "PlayMethod": "DirectPlay", "IsPaused": false,
                                   "PositionTicks": Int64(((detail.position?.positionInTrack) ?? 0) * Self.ticksPerSecond)]
        _ = try await send(try authed(try HTTPRequest.json(.post, url("/Sessions/Playing"), body: body)))
        return PlaybackSession(sessionID: sessionID)
    }

    private func reportBody(_ u: ProgressUpdate, session: PlaybackSession?, paused: Bool) -> [String: Any] {
        ["ItemId": u.trackID ?? u.assetID, "PlaySessionId": session?.sessionID ?? u.sessionID ?? "", "PositionTicks": Int64(u.positionInTrack * Self.ticksPerSecond),
         "IsPaused": paused, "PlayMethod": "DirectPlay", "CanSeek": true]
    }

    public func reportProgress(_ update: ProgressUpdate, session: PlaybackSession?) async throws -> ProgressReport {
        _ = try await send(try authed(try HTTPRequest.json(.post, url("/Sessions/Playing/Progress"), body: reportBody(update, session: session, paused: false))))
        if update.isFinished {
            _ = try? await send(try authed(HTTPRequest(method: .post, url: url("/UserPlayedItems/\(update.trackID ?? update.assetID)", ["userId": server.userID]))))
        }
        return ProgressReport(applied: true)
    }

    public func stopPlayback(_ update: ProgressUpdate, session: PlaybackSession?) async throws {
        _ = try await send(try authed(try HTTPRequest.json(.post, url("/Sessions/Playing/Stopped"), body: reportBody(update, session: session, paused: true))))
    }
}
