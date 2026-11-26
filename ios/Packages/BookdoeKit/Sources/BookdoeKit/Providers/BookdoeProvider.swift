import Foundation

/// bookdoe Player API v1 (docs/PLAYER_API.md). Device tokens (`alxd_...`) are
/// minted by password login (`POST /auth/login`, servers 0.9.1+) or by device-code
/// pairing; both reach the Player API and the media byte routes and nothing else.
public final class BookdoeProvider: BaseProvider, MediaProvider {
    public var kind: ServerKind { .bookdoe }
    private let api = "/api/player/v1"
    private var highlightsKnown: Bool?

    private func authed(_ req: HTTPRequest) throws -> HTTPRequest {
        var r = req
        r.headers["Authorization"] = "Bearer \(try credential().accessToken)"
        return r
    }

    private func get(_ path: String, _ query: [String: String?] = [:]) async throws -> [String: Any] {
        try await send(try authed(HTTPRequest(url: url(api + path, query)))).jsonObject()
    }

    // MARK: probe and auth

    public func probe() async throws -> ServerInfo {
        let caps = try await send(HTTPRequest(url: url(api + "/capabilities"))).jsonObject()
        let auth = JSON.object(caps, "auth") ?? [:]
        return ServerInfo(name: JSON.string(caps, "server") ?? "bookdoe", version: JSON.string(caps, "version"),
                          supportsPasswordLogin: JSON.bool(auth, "password_login") ?? false,
                          supportsDevicePairing: JSON.bool(auth, "device_pairing") ?? true,
                          setupRequired: JSON.bool(caps, "setup_required") ?? false,
                          supportsDiscoverChat: JSON.bool(caps, "discover_chat") ?? false)
    }

    /// Client for the Discover chat routes of this server and credential.
    public func discoverChat() -> DiscoverChatClient {
        DiscoverChatClient(server: server, client: client, credentials: credentials, appVersion: appVersion)
    }

    public func login(username: String, password: String, deviceName: String) async throws -> AuthResult {
        let info = try await probe()
        if info.setupRequired { throw APIError.setupRequired }
        guard info.supportsPasswordLogin else {
            throw APIError.notSupported("This server signs devices in by pairing code (its auth mode has no password login).")
        }
        let body: [String: Any] = ["username": username, "password": password, "device_name": deviceName,
                                   "platform": "ios", "client_version": appVersion]
        let resp = try await send(try HTTPRequest.json(.post, url(api + "/auth/login"), body: body))
        return try authResult(from: try resp.jsonObject(), username: username)
    }

    private func authResult(from obj: [String: Any], username: String?) throws -> AuthResult {
        guard let token = JSON.string(obj, "token") else { throw APIError.decoding("no token in login answer") }
        let user = JSON.object(obj, "user") ?? [:]
        updateServer { s in
            s.userID = JSON.string(user, "id")
            if let u = JSON.string(user, "username") { s.username = u } else if let username { s.username = username }
        }
        return AuthResult(credential: Credential(accessToken: token), server: server, displayName: JSON.string(user, "display_name") ?? JSON.string(user, "username"))
    }

    /// Pairing step 1: ask for a code to show the person (RFC 8628 shaped).
    public func startPairing(deviceName: String) async throws -> DevicePairing {
        let body: [String: Any] = ["name": deviceName, "platform": "ios", "client_version": appVersion]
        let obj = try await send(try HTTPRequest.json(.post, url(api + "/device/code"), body: body)).jsonObject()
        guard let dc = JSON.string(obj, "device_code"), let uc = JSON.string(obj, "user_code") else { throw APIError.decoding("pairing answer") }
        return DevicePairing(deviceCode: dc, userCode: uc, verificationURL: JSON.string(obj, "verification_uri_complete").flatMap(URL.init(string:)),
                             interval: JSON.int(obj, "interval") ?? 5, expiresIn: JSON.int(obj, "expires_in") ?? 900)
    }

    /// Pairing step 3: poll until approved.
    public func pollPairing(_ pairing: DevicePairing) async throws -> PairingPoll {
        let resp = try await send(try HTTPRequest.json(.post, url(api + "/device/token"), body: ["device_code": pairing.deviceCode]), allow: [400, 403])
        let obj = try resp.jsonObject()
        if resp.isSuccess { return .approved(try authResult(from: obj, username: nil)) }
        switch JSON.string(obj, "error") {
        case "authorization_pending": return .pending
        case "slow_down": return .slowDown
        case "access_denied": return .denied
        default: return .expired
        }
    }

    // MARK: bookmarks and highlights

    private func bookmark(from o: [String: Any]) -> Bookmark? {
        guard let id = JSON.string(o, "id"), let asset = JSON.string(o, "asset_id") else { return nil }
        let created = JSON.date(seconds: JSON.double(o, "created_at")) ?? Date()
        return Bookmark(id: id, assetID: asset, kind: JSON.string(o, "kind") == "highlight" ? .highlight : .bookmark,
                        location: JSON.string(o, "location"), position: JSON.double(o, "position"), percent: JSON.double(o, "percent"),
                        title: JSON.string(o, "title"), note: JSON.string(o, "note"), color: JSON.string(o, "color"),
                        createdAt: created, updatedAt: JSON.date(seconds: JSON.double(o, "updated_at")) ?? created)
    }

    public func bookmarks(for book: BookRef) async throws -> [Bookmark] {
        var out: [Bookmark] = []
        var offset = 0
        while true {
            let obj = try await get("/bookmarks", ["asset_id": book.assetID, "limit": "200", "offset": String(offset)])
            let page = JSON.array(obj, "items").compactMap(bookmark)
            out += page
            offset += page.count
            if page.isEmpty || out.count >= (JSON.int(obj, "total") ?? out.count) { break }
        }
        return out
    }

    public func addBookmark(_ new: NewBookmark, to book: BookRef) async throws -> Bookmark {
        var body: [String: Any] = ["asset_id": book.assetID]
        if let v = new.location { body["location"] = v }
        if let v = new.position { body["position"] = v }
        if let v = new.percent { body["percent"] = min(1, max(0, v)) }
        if let v = new.title { body["title"] = String(v.prefix(200)) }
        if let v = new.note { body["note"] = String(v.prefix(2000)) }
        if new.kind == .highlight {   // an older server rejects the extra fields, so a plain bookmark never sends them
            body["kind"] = "highlight"
            if let v = new.color { body["color"] = v }
        }
        let obj = try await send(try authed(try HTTPRequest.json(.post, url(api + "/bookmarks"), body: body))).jsonObject()
        guard let made = bookmark(from: obj) else { throw APIError.decoding("bookmark answer") }
        return made
    }

    public func updateBookmark(_ bookmark: Bookmark, change: BookmarkChange, in book: BookRef) async throws -> Bookmark {
        var body: [String: Any] = [:]
        func put(_ key: String, _ patch: Patch<String>, limit: Int) {
            if case .set(let v) = patch { body[key] = v.map { String($0.prefix(limit)) } ?? NSNull() }
        }
        put("title", change.title, limit: 200)
        put("note", change.note, limit: 2000)
        put("color", change.color, limit: 20)
        guard !body.isEmpty else { return bookmark }
        let obj = try await send(try authed(try HTTPRequest.json(.patch, url(api + "/bookmarks/\(bookmark.id)"), body: body))).jsonObject()
        guard let updated = self.bookmark(from: obj) else { throw APIError.decoding("bookmark answer") }
        return updated
    }

    public func deleteBookmark(_ bookmark: Bookmark, in book: BookRef) async throws {
        _ = try await send(try authed(HTTPRequest(method: .delete, url: url(api + "/bookmarks/\(bookmark.id)"))))
    }

    public func supportsHighlights() async -> Bool {
        if let known = highlightsKnown { return known }
        let caps = try? await send(HTTPRequest(url: url(api + "/capabilities"))).jsonObject()
        let on = ((caps?["features"] as? [String]) ?? []).contains("highlights")
        highlightsKnown = on
        return on
    }

    /// Finished: save 100% (the server leaves finished books out of /continue). Unread:
    /// `DELETE /progress/{asset}` (servers with that route; older ones answer 404/405).
    public func setFinished(_ finished: Bool, assetID: String, detail: MediaDetail?) async throws {
        if let episodeID = Self.podcastEpisodeID(fromAssetID: assetID) {   // podcast episode: its own played/unplayed routes
            _ = try await send(try authed(HTTPRequest(method: .post, url: url(api + "/podcasts/episodes/\(episodeID)/" + (finished ? "played" : "unplayed")))))
            return
        }
        let path = api + "/progress/\(assetID)"
        if !finished {
            do {
                _ = try await send(try authed(HTTPRequest(method: .delete, url: url(path))))
            } catch APIError.notFound {
                throw APIError.notSupported("Marking a book unread needs a newer bookdoe server. Update the server and try again.")
            } catch APIError.server(let status, _) where status == 405 {
                throw APIError.notSupported("Marking a book unread needs a newer bookdoe server. Update the server and try again.")
            }
            return
        }
        var item: [String: Any] = ["asset_id": assetID, "device_id": server.deviceID,
                                   "client_updated_at": Date().timeIntervalSince1970, "percent": 1.0]
        if let detail, let last = detail.tracks.last {   // audiobook: park on the end of the last file
            item["file_id"] = last.id
            item["position"] = last.durationSeconds ?? 0
            if detail.totalDuration > 0 { item["duration"] = detail.totalDuration }
        } else if let location = detail?.position?.location {   // ebook: keep where the reader was
            item["location"] = location
        }
        let obj = try await send(try authed(try HTTPRequest.json(.put, url(path), body: item))).jsonObject()
        if JSON.bool(obj, "applied") == false { throw APIError.server(status: 409, message: "The server has a newer position for this book; try again.") }
    }

    public func logout() async throws {
        if let c = try? credential(), let me = try? await get("/me"), let dev = JSON.object(me, "device"), let id = JSON.string(dev, "id") {
            var req = HTTPRequest(method: .delete, url: url(api + "/devices/\(id)"))
            req.headers["Authorization"] = "Bearer \(c.accessToken)"
            _ = try? await client.send(req)
        }
        try credentials.remove(for: server.id)
    }

    public func mediaHeaders() throws -> [String: String] {
        ["Authorization": "Bearer \(try credential().accessToken)"]
    }

    // MARK: library

    public func libraries() async throws -> [LibraryRef] { [LibraryRef(id: "library", name: "Library")] }

    private func item(_ w: [String: Any]) -> MediaItem {
        let authors = JSON.array(w, "authors").compactMap { JSON.string($0, "name") }
        let narrators = JSON.array(w, "narrators").compactMap { JSON.string($0, "name") }
        let series = JSON.array(w, "series").first
        let assets = JSON.array(w, "assets")
        let audio = assets.first { JSON.string($0, "kind") == "audiobook" }
        let kind: MediaKind = audio != nil ? .audiobook : .ebook
        let duration = audio.flatMap { JSON.double($0, "duration_seconds") }
        let cover = JSON.string(w, "cover_url").map { server.baseURL.appending(path: $0, query: ["size": "384"]) }
        return MediaItem(id: JSON.string(w, "id") ?? "", title: JSON.string(w, "title") ?? "", subtitle: JSON.string(w, "subtitle"),
                         authors: authors, narrators: narrators,
                         series: series.flatMap { JSON.string($0, "name") }, seriesSequence: series.flatMap { JSON.string($0, "position") },
                         kind: kind, durationSeconds: duration, coverURL: cover, progress: nil,
                         updatedAt: JSON.date(seconds: JSON.double(w, "updated_at")))
    }

    public func browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int) async throws -> LibraryPage {
        let obj = try await get("/library", ["q": query?.isEmpty == false ? query : nil, "kind": kind?.rawValue, "sort": sort.rawValue,
                                             "limit": String(limit), "offset": String(offset)])
        return LibraryPage(items: JSON.array(obj, "items").map(item), total: JSON.int(obj, "total") ?? 0, offset: offset, limit: limit)
    }

    public func continueListening() async throws -> [MediaItem] {
        // /continue returns one flat row per in-progress asset (work_id, title, authors
        // as a comma string, percent, has_cover), not the compact work shape.
        let obj = try await get("/continue")
        return JSON.array(obj, "items").compactMap { row in
            if let episodeID = JSON.string(row, "episode_id") { return continueEpisode(row, episodeID: episodeID) }
            guard let workID = JSON.string(row, "work_id") else { return nil }
            let authors = (JSON.string(row, "authors") ?? "").split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            let kind: MediaKind = JSON.string(row, "kind") == "ebook" ? .ebook : .audiobook
            let cover = (JSON.bool(row, "has_cover") ?? false) ? server.baseURL.appending(path: "/api/player/v1/library/\(workID)/cover", query: ["size": "384"]) : nil
            return MediaItem(id: workID, title: JSON.string(row, "title") ?? "", authors: authors, kind: kind, coverURL: cover,
                             progress: JSON.double(row, "percent"), updatedAt: JSON.date(seconds: JSON.double(row, "updated_at")),
                             assetID: JSON.string(row, "asset_id"))
        }
    }

    /// An in-progress podcast episode row of /continue: show title as author, the show cover, progress = position / duration.
    private func continueEpisode(_ row: [String: Any], episodeID: String) -> MediaItem {
        let showID = JSON.string(row, "show_id")
        let duration = JSON.double(row, "duration_seconds")
        let position = JSON.double(row, "position_seconds") ?? 0
        let progress = duration.flatMap { $0 > 0 ? min(1, max(0, position / $0)) : nil }
        return MediaItem(id: episodeID, title: JSON.string(row, "title") ?? "", authors: JSON.string(row, "show_title").map { [$0] } ?? [],
                         kind: .audiobook, durationSeconds: duration, coverURL: showID.map { podcastCoverURL(showID: $0) }, progress: progress,
                         updatedAt: JSON.date(seconds: JSON.double(row, "updated_at")), assetID: Self.podcastAssetPrefix + episodeID,
                         isEpisode: true, showID: showID)
    }

    func podcastCoverURL(showID: String) -> URL { server.baseURL.appending(path: api + "/podcasts/shows/\(showID)/cover") }

    /// The playable detail for an episode row of Continue Listening: the same builder as the show page, resuming at the
    /// position the server stores.
    public func continueEpisodeDetail(_ item: MediaItem) async throws -> MediaDetail {
        let episode = PodcastEpisode(id: item.id, title: item.title, durationSeconds: item.durationSeconds)
        let show = PodcastShow(id: item.showID ?? "", title: item.authors.first ?? "", coverURL: item.coverURL)
        return try await podcastEpisodeDetail(episode, of: show)
    }

    public func detail(id: String) async throws -> MediaDetail {
        let w = try await get("/library/\(id)")
        var m = item(w)
        let assets = JSON.array(w, "assets")
        let progressRows = JSON.array(w, "progress")
        var detail: MediaDetail?
        if let audio = assets.first(where: { JSON.string($0, "kind") == "audiobook" }), let assetID = JSON.string(audio, "id") {
            let man = try await get("/audio/\(id)/\(assetID)")
            var tracks: [Track] = []
            var offset = 0.0
            for (i, f) in JSON.array(man, "files").enumerated() {
                let d = JSON.double(f, "duration_seconds")
                guard let rel = JSON.string(f, "url"), let u = server.baseURL.resolving(rel) else { continue }
                tracks.append(Track(id: JSON.string(f, "id") ?? String(i), index: i, title: JSON.string(f, "title") ?? "Part \(i + 1)", url: u,
                                    durationSeconds: d, startOffset: offset, sizeBytes: JSON.int(f, "size_bytes").map(Int64.init)))
                offset += d ?? 0
            }
            let chapters = JSON.array(man, "chapters").map { Chapter(title: JSON.string($0, "title") ?? "", start: JSON.double($0, "start") ?? 0, end: JSON.double($0, "end")) }
            m.durationSeconds = JSON.double(man, "duration_seconds") ?? (offset > 0 ? offset : nil)
            var d = MediaDetail(item: m, description: JSON.string(w, "description"), tracks: tracks, chapters: chapters, assetID: assetID)
            if let p = JSON.object(man, "progress") ?? progressRows.first(where: { JSON.string($0, "asset_id") == assetID }) {
                d.position = position(from: p, detail: d)
            }
            detail = d
        }
        // A work can own both formats: the ebook rides along on the same detail (the web Player shows both).
        if let ebook = assets.first(where: { JSON.string($0, "kind") == "ebook" }), let assetID = JSON.string(ebook, "id") {
            let man = try await get("/ebook/\(id)/\(assetID)")
            var pos: PlaybackPosition?
            if let p = JSON.object(man, "progress") { pos = PlaybackPosition(percent: JSON.double(p, "percent"), updatedAt: JSON.date(seconds: JSON.double(p, "updated_at")), location: JSON.string(p, "location")) }
            let url = JSON.string(man, "download_url").flatMap { server.baseURL.resolving($0) }
            if var d = detail {
                d.ebookDownloadURL = url
                d.ebookFormat = JSON.string(man, "format")
                d.ebookAssetID = assetID
                d.ebookPosition = pos
                return d
            }
            var d = MediaDetail(item: m, description: JSON.string(w, "description"), assetID: assetID, ebookDownloadURL: url, ebookFormat: JSON.string(man, "format"))
            d.position = pos
            return d
        }
        return detail ?? MediaDetail(item: m, description: JSON.string(w, "description"), assetID: id)
    }

    private func position(from p: [String: Any], detail: MediaDetail) -> PlaybackPosition {
        let fileID = JSON.string(p, "file_id")
        let inTrack = JSON.double(p, "position") ?? 0
        let track = detail.tracks.first { $0.id == fileID } ?? detail.tracks.first
        return PlaybackPosition(trackID: fileID, trackIndex: track?.index ?? 0, positionInTrack: inTrack,
                                absolutePosition: (track?.startOffset ?? 0) + inTrack, percent: JSON.double(p, "percent"),
                                updatedAt: JSON.date(seconds: JSON.double(p, "updated_at")), location: JSON.string(p, "location"))
    }

    // MARK: playback and progress

    public func startPlayback(_ detail: MediaDetail) async throws -> PlaybackSession { PlaybackSession(sessionID: nil) }

    public func reportProgress(_ update: ProgressUpdate, session: PlaybackSession?) async throws -> ProgressReport {
        if let episodeID = Self.podcastEpisodeID(fromAssetID: update.assetID) { return try await reportPodcastProgress(update, episodeID: episodeID) }
        var item: [String: Any] = ["asset_id": update.assetID, "device_id": server.deviceID,
                                   "client_updated_at": update.clientUpdatedAt.timeIntervalSince1970]
        if let t = update.trackID { item["file_id"] = t; item["position"] = update.positionInTrack }
        if let d = update.duration { item["duration"] = d }
        if let p = update.percent { item["percent"] = min(1, max(0, p)) }
        if let l = update.location { item["location"] = l }
        let obj = try await send(try authed(try HTTPRequest.json(.put, url(api + "/progress"), body: ["items": [item]]))).jsonObject()
        guard let first = JSON.array(obj, "items").first else { throw APIError.decoding("progress answer") }
        if let err = JSON.string(first, "error") {
            if err == "not_found" { throw APIError.notFound }
            throw APIError.server(status: 400, message: err)
        }
        let applied = JSON.bool(first, "applied") ?? true
        var serverPos: PlaybackPosition?
        if !applied {
            let stored = JSON.object(first, "stored") ?? first
            serverPos = PlaybackPosition(trackID: JSON.string(stored, "file_id"), positionInTrack: JSON.double(stored, "position") ?? 0,
                                         absolutePosition: JSON.double(stored, "position") ?? 0, percent: JSON.double(stored, "percent"),
                                         updatedAt: JSON.date(seconds: JSON.double(stored, "updated_at")), location: JSON.string(stored, "location"))
        }
        return ProgressReport(applied: applied, serverPosition: serverPos)
    }

    public func stopPlayback(_ update: ProgressUpdate, session: PlaybackSession?) async throws {
        _ = try await reportProgress(update, session: session)
    }

    // MARK: podcasts (issues #514, #516)

    /// Podcast episodes ride the audiobook player under an asset id of this form, so the progress queue can route them.
    static let podcastAssetPrefix = "podcast:"

    static func podcastEpisodeID(fromAssetID id: String) -> String? {
        id.hasPrefix(podcastAssetPrefix) ? String(id.dropFirst(podcastAssetPrefix.count)) : nil
    }

    public func podcastShows() async throws -> [PodcastShowRef] {
        JSON.array(try await get("/podcasts"), "shows").compactMap { s in
            JSON.string(s, "id").map { PodcastShowRef(id: $0, title: JSON.string(s, "title") ?? "") }
        }
    }

    public func podcastShow(id: String) async throws -> PodcastShow {
        let obj = try await get("/podcasts/shows/\(id)")
        guard let show = PodcastShow.parse(obj, baseURL: server.baseURL) else { throw APIError.decoding("podcast show answer") }
        return show
    }

    /// A one-track detail for the player: the episode title, the show as "author" (lock screen artist) and the position
    /// this account last saved for the episode.
    public func podcastEpisodeDetail(_ episode: PodcastEpisode, of show: PodcastShow) async throws -> MediaDetail {
        let saved = try await get("/podcasts/episodes/\(episode.id)/progress")
        let seconds = JSON.object(saved, "progress").flatMap { JSON.double($0, "position_seconds") } ?? 0
        let audio = server.baseURL.appending(path: api + "/podcasts/episodes/\(episode.id)/audio")
        let item = MediaItem(id: episode.id, title: episode.title, authors: [show.title], kind: .audiobook,
                             durationSeconds: episode.durationSeconds, coverURL: show.coverURL)
        let track = Track(id: episode.id, index: 0, title: episode.title, url: audio, durationSeconds: episode.durationSeconds)
        return MediaDetail(item: item, description: episode.description, tracks: [track],
                           position: PlaybackPosition(trackID: episode.id, trackIndex: 0, positionInTrack: seconds, absolutePosition: seconds),
                           assetID: Self.podcastAssetPrefix + episode.id)
    }

    public func nextPodcastEpisode(after episodeID: String) async throws -> PodcastEpisode? {
        let obj = try await get("/podcasts/episodes/\(episodeID)/next")
        guard let e = JSON.object(obj, "episode"), let id = JSON.string(e, "id") else { return nil }
        return PodcastEpisode(id: id, title: JSON.string(e, "title") ?? "", releaseDate: JSON.string(e, "release_date"),
                              durationSeconds: JSON.double(e, "duration_seconds"), description: "",
                              played: JSON.bool(e, "played") ?? false, positionSeconds: JSON.double(e, "position_seconds") ?? 0,
                              missing: JSON.bool(e, "missing") ?? false)
    }

    private func reportPodcastProgress(_ update: ProgressUpdate, episodeID: String) async throws -> ProgressReport {
        let body: [String: Any] = ["position_seconds": max(0, update.absolutePosition), "client_updated_at": update.clientUpdatedAt.timeIntervalSince1970]
        let obj = try await send(try authed(try HTTPRequest.json(.put, url(api + "/podcasts/episodes/\(episodeID)/progress"), body: body))).jsonObject()
        let applied = JSON.bool(obj, "applied") ?? true
        var serverPos: PlaybackPosition?
        if !applied, let stored = JSON.object(obj, "progress") {
            let p = JSON.double(stored, "position_seconds") ?? 0
            serverPos = PlaybackPosition(trackID: episodeID, positionInTrack: p, absolutePosition: p,
                                         updatedAt: JSON.date(seconds: JSON.double(stored, "client_updated_at")))
        }
        return ProgressReport(applied: applied, serverPosition: serverPos)
    }
}
