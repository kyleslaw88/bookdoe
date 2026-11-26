import XCTest
@testable import BookdoeKit

final class AudiobookshelfProviderTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: AudiobookshelfProvider!
    let server = makeServer(.audiobookshelf, base: "https://abs.example.com", libraryID: "lib1", userID: "u1")

    override func setUp() {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        provider = AudiobookshelfProvider(server: server, client: client, credentials: creds, appVersion: "1.0")
    }

    func testProbeReadsStatus() async throws {
        client.enqueueJSON(200, ["serverVersion": "2.37.1", "isInit": true, "authMethods": ["local", "openid"]])
        let info = try await provider.probe()
        XCTAssertEqual(info.version, "2.37.1")
        XCTAssertTrue(info.supportsPasswordLogin)
        XCTAssertFalse(info.setupRequired)
        XCTAssertEqual(client.last?.url.path, "/status")
    }

    func testLoginRequestsTokensInBodyAndStoresRefreshToken() async throws {
        let fresh = AudiobookshelfProvider(server: makeServer(.audiobookshelf, base: "https://abs.example.com"), client: client, credentials: creds, appVersion: "1.0")
        client.enqueueJSON(200, ["user": ["id": "u1", "username": "ada", "accessToken": "acc", "refreshToken": "ref", "mediaProgress": []],
                                 "userDefaultLibraryId": "lib9", "serverSettings": [:]])
        let r = try await fresh.login(username: "ada", password: "pw", deviceName: "iPhone")
        XCTAssertEqual(r.credential, Credential(accessToken: "acc", refreshToken: "ref"))
        XCTAssertEqual(r.server.userID, "u1")
        XCTAssertEqual(r.server.libraryID, "lib9")
        let req = try XCTUnwrap(client.last)
        XCTAssertEqual(req.url.path, "/login")
        XCTAssertEqual(req.headers["x-return-tokens"], "true")
        XCTAssertEqual(req.bodyJSON?["password"] as? String, "pw")
        XCTAssertEqual(try creds.credential(for: fresh.server.id)?.refreshToken, "ref")
    }

    func testExpiredAccessTokenIsRefreshedOnce() async throws {
        try creds.save(Credential(accessToken: "old", refreshToken: "ref"), for: server.id)
        client.router = { req in
            if req.url.path == "/auth/refresh" {
                XCTAssertEqual(req.headers["x-refresh-token"], "ref")
                return .json(200, ["user": ["id": "u1", "username": "ada", "accessToken": "new", "refreshToken": "ref2"]])
            }
            if req.url.path == "/api/libraries" {
                return req.headers["Authorization"] == "Bearer new" ? .json(200, ["libraries": [["id": "lib1", "name": "Audiobooks", "mediaType": "book"]]]) : .json(401, ["message": "expired"])
            }
            return nil
        }
        let libs = try await provider.libraries()
        XCTAssertEqual(libs.map(\.id), ["lib1"])
        XCTAssertEqual(libs.first?.kind, .audiobook)
        XCTAssertEqual(client.requests.map(\.url.path), ["/api/libraries", "/auth/refresh", "/api/libraries"])
        XCTAssertEqual(try creds.credential(for: server.id), Credential(accessToken: "new", refreshToken: "ref2"))
    }

    func testRefreshFailureIsUnauthorized() async throws {
        try creds.save(Credential(accessToken: "old", refreshToken: "ref"), for: server.id)
        client.router = { req in req.url.path == "/auth/refresh" ? HTTPResponse(status: 401) : .json(401, ["message": "expired"]) }
        do { _ = try await provider.libraries(); XCTFail() } catch { XCTAssertEqual(error as? APIError, .unauthorized) }
    }

    func testBrowseUsesPagesAndMinifiedShape() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["results": [["id": "li1", "mediaType": "book", "updatedAt": 1760000000000,
                                              "media": ["metadata": ["title": "Dune", "authorName": "Frank Herbert", "narratorName": "Scott Brick", "seriesName": "Dune #1"],
                                                        "duration": 75000, "numAudioFiles": 3, "coverPath": "/x.jpg"],
                                              "userMediaProgress": ["progress": 0.25]]],
                                 "total": 120, "limit": 50, "page": 2])
        let page = try await provider.browse(query: nil, kind: nil, sort: .title, offset: 100, limit: 50)
        XCTAssertEqual(page.total, 120)
        let item = try XCTUnwrap(page.items.first)
        XCTAssertEqual(item.title, "Dune")
        XCTAssertEqual(item.authors, ["Frank Herbert"])
        XCTAssertEqual(item.narrators, ["Scott Brick"])
        XCTAssertEqual(item.progress, 0.25)
        XCTAssertEqual(item.coverURL?.path, "/api/items/li1/cover")
        XCTAssertEqual(item.updatedAt, Date(timeIntervalSince1970: 1760000000))
        let q = try XCTUnwrap(client.last?.url.queryDict)
        XCTAssertEqual(q["page"], "2")
        XCTAssertEqual(q["limit"], "50")
        XCTAssertEqual(q["sort"], "media.metadata.title")
        XCTAssertEqual(client.last?.url.path, "/api/libraries/lib1/items")
    }

    func testSearchUsesTheSearchRoute() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["book": [["libraryItem": ["id": "li2", "mediaType": "book", "media": ["metadata": ["title": "Emma"], "duration": 10]]]], "authors": []])
        let page = try await provider.browse(query: "emma", kind: nil, sort: .title, offset: 0, limit: 20)
        XCTAssertEqual(page.items.first?.title, "Emma")
        XCTAssertEqual(client.last?.url.path, "/api/libraries/lib1/search")
        XCTAssertEqual(client.last?.url.queryDict["q"], "emma")
    }

    func testDetailTracksChaptersAndCurrentTime() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["id": "li1", "mediaType": "book", "media": [
            "metadata": ["title": "Dune", "authors": [["id": "a", "name": "Frank Herbert"]], "narrators": ["Scott Brick"], "description": "Spice", "series": [["id": "s", "name": "Dune", "sequence": "1"]]],
            "duration": 1000, "coverPath": "/c.jpg",
            "tracks": [["index": 1, "startOffset": 0, "duration": 600, "title": "01.mp3", "contentUrl": "/x/1", "mimeType": "audio/mpeg", "ino": "i1"],
                       ["index": 2, "startOffset": 600, "duration": 400, "title": "02.mp3", "contentUrl": "/x/2", "mimeType": "audio/mpeg", "ino": "i2"]],
            "chapters": [["id": 0, "start": 0, "end": 500, "title": "One"], ["id": 1, "start": 500, "end": 1000, "title": "Two"]],
        ], "userMediaProgress": ["currentTime": 650, "progress": 0.65, "lastUpdate": 1760000000000]])
        let d = try await provider.detail(id: "li1")
        XCTAssertEqual(d.item.authors, ["Frank Herbert"])
        XCTAssertEqual(d.item.narrators, ["Scott Brick"])
        XCTAssertEqual(d.item.seriesSequence, "1")
        XCTAssertEqual(d.tracks.map(\.id), ["i1", "i2"])
        XCTAssertEqual(d.tracks[1].url.path, "/api/items/li1/file/i2")
        XCTAssertEqual(d.chapters.map(\.title), ["One", "Two"])
        XCTAssertEqual(d.position?.absolutePosition, 650)
        XCTAssertEqual(d.position?.trackIndex, 1)
        XCTAssertEqual(d.position?.positionInTrack, 50)
        XCTAssertEqual(d.description, "Spice")
        XCTAssertEqual(client.last?.url.queryDict["expanded"], "1")
    }

    func testPlaySessionSyncAndClose() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        let detail = MediaDetail(item: MediaItem(id: "li1", title: "Dune", kind: .audiobook, durationSeconds: 1000), assetID: "li1")
        client.enqueueJSON(200, ["id": "sess1", "audioTracks": [["index": 0, "startOffset": 0, "duration": 1000, "title": "t", "contentUrl": "/public/session/sess1/track/0", "mimeType": "audio/mp4"]]])
        let session = try await provider.startPlayback(detail)
        XCTAssertEqual(session.sessionID, "sess1")
        let play = try XCTUnwrap(client.last)
        XCTAssertEqual(play.url.path, "/api/items/li1/play")
        XCTAssertEqual(play.bodyJSON?["mediaPlayer"] as? String, "AVPlayer")
        XCTAssertEqual((play.bodyJSON?["deviceInfo"] as? [String: Any])?["deviceId"] as? String, "dev-1")
        let tracks = provider.sessionTracks(from: ["audioTracks": [["index": 0, "startOffset": 0, "duration": 1000, "contentUrl": "/public/session/sess1/track/0"]]])
        XCTAssertEqual(tracks.first?.url.absoluteString, "https://abs.example.com/public/session/sess1/track/0")

        let update = ProgressUpdate(serverID: server.id, itemID: "li1", assetID: "li1", absolutePosition: 321, duration: 1000, listened: 15)
        client.enqueue(HTTPResponse(status: 200))
        _ = try await provider.reportProgress(update, session: session)
        XCTAssertEqual(client.last?.url.path, "/api/session/sess1/sync")
        XCTAssertEqual(client.last?.bodyJSON?["currentTime"] as? Double, 321)
        XCTAssertEqual(client.last?.bodyJSON?["timeListened"] as? Double, 15)

        client.enqueue(HTTPResponse(status: 200))
        client.enqueue(HTTPResponse(status: 200))
        try await provider.stopPlayback(update, session: session)
        XCTAssertEqual(client.last?.url.path, "/api/session/sess1/close")
    }

    func testProgressWithoutSessionPatchesMeProgress() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        let update = ProgressUpdate(serverID: server.id, itemID: "li1", assetID: "li1", absolutePosition: 500, duration: 1000, isFinished: false)
        client.enqueue(HTTPResponse(status: 200))
        _ = try await provider.reportProgress(update, session: nil)
        XCTAssertEqual(client.last?.method, .patch)
        XCTAssertEqual(client.last?.url.path, "/api/me/progress/li1")
        XCTAssertEqual(client.last?.bodyJSON?["progress"] as? Double, 0.5)
    }

    func testContinueListeningShelf() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueue(.json(200, [["id": "continue-listening", "label": "Continue Listening", "type": "book",
                                    "entities": [["id": "li3", "mediaType": "book", "media": ["metadata": ["title": "Emma"], "duration": 5], "userMediaProgress": ["progress": 0.5]]]],
                                   ["id": "recently-added", "entities": []]]))
        let items = try await provider.continueListening()
        XCTAssertEqual(items.map(\.id), ["li3"])
        XCTAssertEqual(items.first?.progress, 0.5)
    }
}
