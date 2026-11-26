import Foundation

/// Audiobookshelf 2.26+ (routes read from the server source and the official app in
/// docs/COMPAT_PLAYERS.md section 2.2). Login is `POST /login` with
/// `x-return-tokens: true`; the access token goes in `Authorization: Bearer`, the
/// refresh token in `x-refresh-token` on `POST /auth/refresh`. Playback opens a
/// session (`POST /api/items/:id/play`) whose track URLs are `/public/session/...`
/// (no auth needed), progress is `POST /api/session/:id/sync`.
public final class AudiobookshelfProvider: BaseProvider, MediaProvider {
    public var kind: ServerKind { .audiobookshelf }

    private func authed(_ req: HTTPRequest) throws -> HTTPRequest {
        var r = req
        r.headers["Authorization"] = "Bearer \(try credential().accessToken)"
        return r
    }

    /// Send with one transparent token refresh on 401.
    private func sendAuthed(_ req: HTTPRequest, allow: Set<Int> = []) async throws -> HTTPResponse {
        let first = try await client.send(try authed(req))
        if first.status != 401 {
            if first.isSuccess || allow.contains(first.status) { return first }
            throw APIError.from(first)
        }
        try await refresh()
        return try await send(try authed(req), allow: allow)
    }

    private func get(_ path: String, _ query: [String: String?] = [:]) async throws -> [String: Any] {
        try await sendAuthed(HTTPRequest(url: url(path, query))).jsonObject()
    }

    // MARK: probe and auth

    public func probe() async throws -> ServerInfo {
        let obj = try await send(HTTPRequest(url: url("/status"))).jsonObject()
        let methods = (obj["authMethods"] as? [String]) ?? ["local"]
        let form = JSON.object(obj, "authFormData") ?? [:]
        return ServerInfo(name: "Audiobookshelf", version: JSON.string(obj, "serverVersion"),
                          supportsPasswordLogin: methods.contains("local") || methods.isEmpty, setupRequired: JSON.bool(obj, "isInit") == false,
                          supportsOpenID: methods.contains("openid"), openIDButtonText: JSON.string(form, "authOpenIDButtonText"))
    }

    // MARK: OpenID Connect (single sign-on)

    /// The app link Audiobookshelf's mobile flow sends the browser back to. It is on the server's
    /// default list of allowed mobile redirects, so the server needs no change.
    public static let openIDRedirect = "audiobookshelf://oauth"
    public static let openIDScheme = "audiobookshelf"

    /// What a sign-in in the browser needs to be finished afterwards.
    public struct OpenIDStart: Equatable, Sendable {
        /// The identity provider's sign-in page, to open in a web authentication session.
        public var authorizeURL: URL
        public var verifier: String
        public var state: String
    }

    /// Step 1: ask the server for the identity provider's sign-in URL (the answer is a redirect that
    /// is read, not followed). The server remembers this request in a cookie session, so step 3 must
    /// use the same HTTP client.
    public func beginOpenID(verifier: String = PKCE.makeVerifier()) async throws -> OpenIDStart {
        let challenge = PKCE.challenge(for: verifier)
        let start = url("/auth/openid", ["code_challenge": challenge, "code_challenge_method": "S256", "redirect_uri": Self.openIDRedirect,
                                         "client_id": "Audiobookshelf-App", "response_type": "code"])
        let resp = try await send(HTTPRequest(url: start, followRedirects: false), allow: [301, 302, 303, 307, 308])
        guard (300..<400).contains(resp.status) else { throw APIError.decoding("expected the server to redirect to its sign-in provider (got HTTP \(resp.status))") }
        guard let location = resp.header("Location"), let idp = URL(string: location, relativeTo: server.baseURL)?.absoluteURL,
              let comps = URLComponents(url: idp, resolvingAgainstBaseURL: true) else {
            throw APIError.decoding("the server did not say where to sign in")
        }
        func param(_ name: String) -> String? { comps.queryItems?.first { $0.name == name }?.value }
        guard let clientID = param("client_id"), let scope = param("scope"), let state = param("state"), let redirect = param("redirect_uri") else {
            throw APIError.decoding("the sign-in provider's answer is missing client_id, scope, state or redirect_uri")
        }
        guard idp.scheme == "https" || server.baseURL.scheme == "http" else {
            throw APIError.notSupported("The single sign-on provider is not on HTTPS, so the app will not use it.")
        }
        // Rebuilt the way the official app does, so the provider sees exactly the parameters it expects.
        var out = URLComponents()
        out.scheme = idp.scheme
        out.host = idp.host
        out.port = idp.port
        out.percentEncodedPath = comps.percentEncodedPath   // keeps a trailing slash, which some providers need
        out.queryItems = [URLQueryItem(name: "response_type", value: "code"), URLQueryItem(name: "client_id", value: clientID),
                          URLQueryItem(name: "scope", value: scope), URLQueryItem(name: "state", value: state),
                          URLQueryItem(name: "redirect_uri", value: redirect), URLQueryItem(name: "code_challenge", value: challenge),
                          URLQueryItem(name: "code_challenge_method", value: "S256")]
        guard let authorize = out.url else { throw APIError.decoding("the sign-in address could not be built") }
        return OpenIDStart(authorizeURL: authorize, verifier: verifier, state: state)
    }

    /// Step 3: the browser came back to `audiobookshelf://oauth?code=...&state=...`; swap the code for a
    /// session. The answer has the same shape as a password login.
    public func completeOpenID(callback: URL, start: OpenIDStart) async throws -> AuthResult {
        let items = URLComponents(url: callback, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func param(_ name: String) -> String? { items.first { $0.name == name }?.value }
        if let error = param("error") { throw APIError.forbidden(param("error_description") ?? error) }
        guard param("state") == start.state else { throw APIError.forbidden("The sign-in answer did not match the request (wrong state). Try again.") }
        guard let code = param("code"), !code.isEmpty else { throw APIError.decoding("the sign-in answer had no code") }
        let obj = try await send(HTTPRequest(url: url("/auth/openid/callback", ["state": start.state, "code": code, "code_verifier": start.verifier]))).jsonObject()
        guard let user = JSON.object(obj, "user") else { throw APIError.decoding("sign-in answer") }
        let result = try apply(user: user, username: nil)
        if server.libraryID == nil {
            var libID = JSON.string(obj, "userDefaultLibraryId")
            if libID == nil { libID = try? await libraries().first?.id }
            updateServer { $0.libraryID = libID }
        }
        return AuthResult(credential: result.credential, server: server, displayName: result.displayName)
    }

    private func apply(user: [String: Any], username: String?) throws -> AuthResult {
        guard let token = JSON.string(user, "accessToken") ?? JSON.string(user, "token") else { throw APIError.decoding("no accessToken") }
        let cred = Credential(accessToken: token, refreshToken: JSON.string(user, "refreshToken"))
        updateServer { s in
            s.userID = JSON.string(user, "id")
            if let u = JSON.string(user, "username") { s.username = u } else if let username { s.username = username }
        }
        try credentials.save(cred, for: server.id)
        return AuthResult(credential: cred, server: server, displayName: JSON.string(user, "username"))
    }

    public func login(username: String, password: String, deviceName: String) async throws -> AuthResult {
        var req = try HTTPRequest.json(.post, url("/login"), body: ["username": username, "password": password])
        req.headers["x-return-tokens"] = "true"
        let obj = try await send(req).jsonObject()
        guard let user = JSON.object(obj, "user") else { throw APIError.decoding("login answer") }
        let result = try apply(user: user, username: username)
        if server.libraryID == nil {
            var libID = JSON.string(obj, "userDefaultLibraryId")
            if libID == nil { libID = try? await libraries().first?.id }
            updateServer { $0.libraryID = libID }
        }
        return AuthResult(credential: result.credential, server: server, displayName: result.displayName)
    }

    // MARK: bookmarks (audio positions)

    private func bookmark(from o: [String: Any], asset: String) -> Bookmark? {
        guard let time = JSON.double(o, "time") else { return nil }
        let created = JSON.date(seconds: JSON.double(o, "createdAt").map { $0 / 1000 }) ?? Date()
        // Audiobookshelf identifies a bookmark by its time within the item.
        return Bookmark(id: String(time), assetID: asset, kind: .bookmark, position: time, title: JSON.string(o, "title"), createdAt: created)
    }

    public func bookmarks(for book: BookRef) async throws -> [Bookmark] {
        let obj = try await get("/api/me/bookmarks/\(book.itemID)")
        return JSON.array(obj, "bookmarks").compactMap { bookmark(from: $0, asset: book.assetID) }.sorted { ($0.position ?? 0) < ($1.position ?? 0) }
    }

    public func addBookmark(_ new: NewBookmark, to book: BookRef) async throws -> Bookmark {
        guard let time = new.position else { throw APIError.notSupported("Audiobookshelf bookmarks are positions in an audiobook.") }
        let title = (new.title?.isEmpty == false ? new.title : nil) ?? "Bookmark"
        let req = try HTTPRequest.json(.post, url("/api/me/item/\(book.itemID)/bookmark"), body: ["time": time, "title": title])
        let obj = try await sendAuthed(req).jsonObject()
        guard let made = bookmark(from: obj, asset: book.assetID) else { throw APIError.decoding("bookmark answer") }
        return made
    }

    public func updateBookmark(_ bookmark: Bookmark, change: BookmarkChange, in book: BookRef) async throws -> Bookmark {
        guard case .set(let new) = change.title, let time = bookmark.position else { return bookmark }
        let req = try HTTPRequest.json(.patch, url("/api/me/item/\(book.itemID)/bookmark"), body: ["time": time, "title": new ?? "Bookmark"])
        let obj = try await sendAuthed(req).jsonObject()
        return self.bookmark(from: obj, asset: book.assetID) ?? bookmark
    }

    public func deleteBookmark(_ bookmark: Bookmark, in book: BookRef) async throws {
        guard let time = bookmark.position else { return }
        _ = try await sendAuthed(HTTPRequest(method: .delete, url: url("/api/me/item/\(book.itemID)/bookmark/\(time)")))
    }

    public func refresh() async throws {
        guard let rt = try credential().refreshToken else { throw APIError.unauthorized }
        var req = HTTPRequest(method: .post, url: url("/auth/refresh"))
        req.headers["x-refresh-token"] = rt
        let resp = try await client.send(req)
        guard resp.isSuccess, let obj = try? resp.jsonObject(), let user = JSON.object(obj, "user") else { throw APIError.unauthorized }
        _ = try apply(user: user, username: nil)
    }

    public func logout() async throws {
        if let c = try? credential() {
            var req = HTTPRequest(method: .post, url: url("/logout"))
            req.headers["Authorization"] = "Bearer \(c.accessToken)"
            if let rt = c.refreshToken { req.headers["x-refresh-token"] = rt }
            _ = try? await client.send(req)
        }
        try credentials.remove(for: server.id)
    }

    public func mediaHeaders() throws -> [String: String] {
        ["Authorization": "Bearer \(try credential().accessToken)"]
    }

    // MARK: library

    public func libraries() async throws -> [LibraryRef] {
        let obj = try await get("/api/libraries")
        return JSON.array(obj, "libraries").compactMap { l in
            guard let id = JSON.string(l, "id") else { return nil }
            return LibraryRef(id: id, name: JSON.string(l, "name") ?? id, kind: JSON.string(l, "mediaType") == "book" ? .audiobook : nil)
        }
    }

    private func libraryID() throws -> String {
        guard let id = server.libraryID else { throw APIError.notSupported("Choose an Audiobookshelf library first.") }
        return id
    }

    private func coverURL(_ id: String) -> URL { url("/api/items/\(id)/cover", ["width": "384"]) }

    private func item(_ o: [String: Any]) -> MediaItem {
        let media = JSON.object(o, "media") ?? [:]
        let meta = JSON.object(media, "metadata") ?? [:]
        // minified listings carry authorName/narratorName strings; expanded ones arrays.
        let authors = (JSON.string(meta, "authorName").map { [$0] }) ?? JSON.array(meta, "authors").compactMap { JSON.string($0, "name") }
        let narrators = (JSON.string(meta, "narratorName").map { [$0] }) ?? ((meta["narrators"] as? [String]) ?? [])
        let series = JSON.string(meta, "seriesName") ?? JSON.array(meta, "series").first.flatMap { JSON.string($0, "name") }
        let seq = JSON.array(meta, "series").first.flatMap { JSON.string($0, "sequence") }
        let hasAudio = (JSON.int(media, "numAudioFiles") ?? 0) > 0 || !JSON.array(media, "audioFiles").isEmpty || !JSON.array(media, "tracks").isEmpty || JSON.double(media, "duration") != nil
        let id = JSON.string(o, "id") ?? ""
        let progress = JSON.object(o, "userMediaProgress").flatMap { JSON.double($0, "progress") }
        let updated = JSON.double(o, "updatedAt").map { Date(timeIntervalSince1970: $0 / 1000) }
        return MediaItem(id: id, title: JSON.string(meta, "title") ?? "", subtitle: JSON.string(meta, "subtitle"), authors: authors, narrators: narrators,
                         series: series, seriesSequence: seq, kind: hasAudio || JSON.string(o, "mediaType") == "book" ? .audiobook : .ebook,
                         durationSeconds: JSON.double(media, "duration"), coverURL: JSON.string(media, "coverPath") != nil ? coverURL(id) : nil,
                         progress: progress, updatedAt: updated)
    }

    public func browse(query: String?, kind: MediaKind?, sort: LibrarySort, offset: Int, limit: Int) async throws -> LibraryPage {
        let lib = try libraryID()
        if let query, !query.isEmpty {
            let obj = try await get("/api/libraries/\(lib)/search", ["q": query, "limit": String(limit)])
            let rows = JSON.array(obj, "book").compactMap { JSON.object($0, "libraryItem") }
            return LibraryPage(items: rows.map(item), total: rows.count, offset: 0, limit: limit)
        }
        let sortKey: String
        switch sort {
        case .title: sortKey = "media.metadata.title"
        case .added: sortKey = "addedAt"
        case .updated: sortKey = "updatedAt"
        }
        let page = limit > 0 ? offset / limit : 0
        let obj = try await get("/api/libraries/\(lib)/items", ["limit": String(limit), "page": String(page), "sort": sortKey, "desc": sort == .title ? "0" : "1",
                                                                  "minified": "1", "include": "progress"])
        return LibraryPage(items: JSON.array(obj, "results").map(item), total: JSON.int(obj, "total") ?? 0, offset: offset, limit: limit)
    }

    public func continueListening() async throws -> [MediaItem] {
        let arr = try await sendAuthed(HTTPRequest(url: url("/api/libraries/\(try libraryID())/personalized", ["include": "progress"]))).jsonArray()
        for shelf in arr.compactMap({ $0 as? [String: Any] }) where JSON.string(shelf, "id") == "continue-listening" {
            return JSON.array(shelf, "entities").map(item)
        }
        return []
    }

    public func detail(id: String) async throws -> MediaDetail {
        let o = try await get("/api/items/\(id)", ["expanded": "1", "include": "progress"])
        var m = item(o)
        let media = JSON.object(o, "media") ?? [:]
        var tracks: [Track] = []
        let trackRows = JSON.array(media, "tracks")
        let rows = trackRows.isEmpty ? JSON.array(media, "audioFiles") : trackRows
        var offset = 0.0
        for (i, f) in rows.enumerated() {
            let d = JSON.double(f, "duration")
            let ino = JSON.string(f, "ino")
            // Direct file route (Range + Bearer), which Plappa also uses; the play
            // session below replaces these URLs with /public/session ones.
            let u = ino.map { url("/api/items/\(id)/file/\($0)") } ?? url("/api/items/\(id)/file/\(i)")
            let title = JSON.string(f, "title") ?? JSON.object(f, "metadata").flatMap { JSON.string($0, "filename") } ?? "Part \(i + 1)"
            tracks.append(Track(id: ino ?? String(i), index: i, title: title, url: u, durationSeconds: d, startOffset: JSON.double(f, "startOffset") ?? offset,
                                mimeType: JSON.string(f, "mimeType")))
            offset += d ?? 0
        }
        let chapters = JSON.array(media, "chapters").map { Chapter(title: JSON.string($0, "title") ?? "", start: JSON.double($0, "start") ?? 0, end: JSON.double($0, "end")) }
        m.durationSeconds = JSON.double(media, "duration") ?? (offset > 0 ? offset : nil)
        var detail = MediaDetail(item: m, description: JSON.object(media, "metadata").flatMap { JSON.string($0, "description") }, tracks: tracks, chapters: chapters, assetID: id)
        if let p = JSON.object(o, "userMediaProgress"), let cur = JSON.double(p, "currentTime") {
            let loc = detail.locate(absolute: cur)
            detail.position = PlaybackPosition(trackID: tracks.first { $0.index == loc.index }?.id, trackIndex: loc.index, positionInTrack: loc.offset,
                                               absolutePosition: cur, percent: JSON.double(p, "progress"),
                                               updatedAt: JSON.double(p, "lastUpdate").map { Date(timeIntervalSince1970: $0 / 1000) })
        }
        return detail
    }

    // MARK: playback sessions

    public func startPlayback(_ detail: MediaDetail) async throws -> PlaybackSession {
        let body: [String: Any] = ["deviceInfo": ["deviceId": server.deviceID, "clientName": "bookdoe", "clientVersion": appVersion, "manufacturer": "Apple", "model": "iPhone"],
                                   "mediaPlayer": "AVPlayer", "forceDirectPlay": "1", "forceTranscode": ""]
        let obj = try await sendAuthed(try HTTPRequest.json(.post, url("/api/items/\(detail.assetID)/play"), body: body)).jsonObject()
        return PlaybackSession(sessionID: JSON.string(obj, "id"))
    }

    /// Tracks as the play session serves them (`/public/session/:id/track/:index`);
    /// call after `startPlayback` to replace `detail.tracks` when the server returned them.
    public func sessionTracks(from sessionAnswer: [String: Any]) -> [Track] {
        JSON.array(sessionAnswer, "audioTracks").enumerated().compactMap { i, t in
            guard let rel = JSON.string(t, "contentUrl"), let u = server.baseURL.resolving(rel) else { return nil }
            return Track(id: String(JSON.int(t, "index") ?? i), index: JSON.int(t, "index") ?? i, title: JSON.string(t, "title") ?? "Part \(i + 1)", url: u,
                         durationSeconds: JSON.double(t, "duration"), startOffset: JSON.double(t, "startOffset") ?? 0, mimeType: JSON.string(t, "mimeType"))
        }
    }

    public func reportProgress(_ update: ProgressUpdate, session: PlaybackSession?) async throws -> ProgressReport {
        let duration = update.duration ?? 0
        if let sid = session?.sessionID ?? update.sessionID {
            let body: [String: Any] = ["currentTime": update.absolutePosition, "duration": duration, "timeListened": update.listened]
            _ = try await sendAuthed(try HTTPRequest.json(.post, url("/api/session/\(sid)/sync"), body: body))
        } else {
            var body: [String: Any] = ["currentTime": update.absolutePosition, "isFinished": update.isFinished]
            if duration > 0 { body["duration"] = duration; body["progress"] = update.absolutePosition / duration }
            _ = try await sendAuthed(try HTTPRequest.json(.patch, url("/api/me/progress/\(update.assetID)"), body: body))
        }
        return ProgressReport(applied: true)
    }

    public func stopPlayback(_ update: ProgressUpdate, session: PlaybackSession?) async throws {
        _ = try await reportProgress(update, session: session)
        if let sid = session?.sessionID ?? update.sessionID {
            let body: [String: Any] = ["currentTime": update.absolutePosition, "duration": update.duration ?? 0, "timeListened": 0]
            _ = try? await sendAuthed(try HTTPRequest.json(.post, url("/api/session/\(sid)/close"), body: body))
        }
    }
}
