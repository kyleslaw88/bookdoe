import XCTest
@testable import BookdoeKit

final class BookdoeProviderTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: BookdoeProvider!
    let server = makeServer(.bookdoe)

    override func setUp() {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        provider = BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1.2")
    }

    func capabilities(passwordLogin: Bool = true, setupRequired: Bool = false) -> [String: Any] {
        ["server": "bookdoe", "version": "0.9.1", "api_version": "1", "setup_required": setupRequired,
         "auth": ["device_pairing": true, "password_login": passwordLogin, "auth_mode": "local", "poll_interval": 5, "code_lifetime": 900]]
    }

    func testProbeReadsCapabilities() async throws {
        client.enqueueJSON(200, capabilities())
        let info = try await provider.probe()
        XCTAssertEqual(info.name, "bookdoe")
        XCTAssertEqual(info.version, "0.9.1")
        XCTAssertTrue(info.supportsPasswordLogin)
        XCTAssertTrue(info.supportsDevicePairing)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/capabilities")
        XCTAssertNil(client.last?.headers["Authorization"])
    }

    func testLoginMintsDeviceTokenAndUpdatesServer() async throws {
        client.enqueueJSON(200, capabilities())
        client.enqueueJSON(200, ["token": "alxd_abc", "token_type": "Bearer", "device_id": "d1", "name": "Ada's iPhone",
                                 "user": ["id": "u1", "username": "ada", "account_type": "admin", "display_name": "Ada"]])
        let result = try await provider.login(username: "ada", password: "pw", deviceName: "Ada's iPhone")
        XCTAssertEqual(result.credential.accessToken, "alxd_abc")
        XCTAssertEqual(result.server.userID, "u1")
        XCTAssertEqual(result.displayName, "Ada")
        let body = client.last?.bodyJSON
        XCTAssertEqual(body?["username"] as? String, "ada")
        XCTAssertEqual(body?["password"] as? String, "pw")
        XCTAssertEqual(body?["platform"] as? String, "ios")
        XCTAssertEqual(body?["client_version"] as? String, "1.2")
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/auth/login")
    }

    func testLoginRefusedWhenServerNeedsSetupOrHasNoPasswordLogin() async {
        client.enqueueJSON(200, capabilities(setupRequired: true))
        do { _ = try await provider.login(username: "k", password: "p", deviceName: "d"); XCTFail() } catch { XCTAssertEqual(error as? APIError, .setupRequired) }
        client.enqueueJSON(200, capabilities(passwordLogin: false))
        do { _ = try await provider.login(username: "k", password: "p", deviceName: "d"); XCTFail() } catch {
            if case .notSupported = error as? APIError {} else { XCTFail("\(error)") }
        }
    }

    func testPairingFlow() async throws {
        client.enqueueJSON(201, ["device_code": "dc", "user_code": "GHKM-7T4P", "verification_uri_complete": "https://books.example.com/app#pair/GHKM-7T4P", "interval": 5, "expires_in": 900])
        let pairing = try await provider.startPairing(deviceName: "iPad")
        XCTAssertEqual(pairing.userCode, "GHKM-7T4P")
        client.enqueueJSON(400, ["error": "authorization_pending"])
        client.enqueueJSON(400, ["error": "slow_down"])
        client.enqueueJSON(403, ["error": "access_denied"])
        client.enqueueJSON(200, ["token": "alxd_paired", "device_id": "d2", "user": ["id": "u1", "username": "ada"]])
        var poll = try await provider.pollPairing(pairing); XCTAssertEqual(poll, .pending)
        poll = try await provider.pollPairing(pairing); XCTAssertEqual(poll, .slowDown)
        poll = try await provider.pollPairing(pairing); XCTAssertEqual(poll, .denied)
        poll = try await provider.pollPairing(pairing)
        guard case .approved(let r) = poll else { return XCTFail() }
        XCTAssertEqual(r.credential.accessToken, "alxd_paired")
        XCTAssertEqual(client.last?.bodyJSON?["device_code"] as? String, "dc")
    }

    func testBrowseSendsBearerAndMapsCompactWorks() async throws {
        try creds.save(Credential(accessToken: "alxd_tok"), for: server.id)
        client.enqueueJSON(200, ["items": [[
            "id": "w1", "title": "Dune", "subtitle": NSNull(), "authors": [["id": "a1", "name": "Frank Herbert"]],
            "narrators": [["id": "n1", "name": "Scott Brick"]], "series": [["id": "s1", "name": "Dune", "position": "1"]],
            "has_cover": true, "cover_url": "/api/player/v1/library/w1/cover",
            "assets": [["id": "as1", "kind": "audiobook", "format": "m4b", "readable": true, "duration_seconds": 75000]],
            "formats": ["audiobook"], "updated_at": 1760000000.0,
        ]], "total": 1, "limit": 50, "offset": 0, "server_time": 1760000001.0])
        let page = try await provider.browse(query: "dune", kind: .audiobook, sort: .title, offset: 0, limit: 50)
        XCTAssertEqual(page.total, 1)
        let item = try XCTUnwrap(page.items.first)
        XCTAssertEqual(item.title, "Dune")
        XCTAssertEqual(item.authors, ["Frank Herbert"])
        XCTAssertEqual(item.narrators, ["Scott Brick"])
        XCTAssertEqual(item.series, "Dune")
        XCTAssertEqual(item.kind, .audiobook)
        XCTAssertEqual(item.durationSeconds, 75000)
        XCTAssertEqual(item.coverURL?.path, "/api/player/v1/library/w1/cover")
        XCTAssertEqual(item.coverURL?.queryDict["size"], "384")
        XCTAssertNil(page.nextOffset)
        let req = try XCTUnwrap(client.last)
        XCTAssertEqual(req.headers["Authorization"], "Bearer alxd_tok")
        XCTAssertEqual(req.url.queryDict["q"], "dune")
        XCTAssertEqual(req.url.queryDict["kind"], "audiobook")
        XCTAssertEqual(req.url.queryDict["limit"], "50")
    }

    func testBrowseWithoutCredentialIsUnauthorized() async {
        do { _ = try await provider.browse(query: nil, kind: nil, sort: .title, offset: 0, limit: 10); XCTFail() } catch { XCTAssertEqual(error as? APIError, .unauthorized) }
        XCTAssertTrue(client.requests.isEmpty)
    }

    func testDetailBuildsTracksChaptersAndPosition() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["id": "w1", "title": "Dune", "authors": [["id": "a", "name": "F"]], "narrators": [], "series": [], "has_cover": false, "cover_url": NSNull(),
                                 "assets": [["id": "as1", "kind": "audiobook", "format": "mp3", "readable": true, "duration_seconds": NSNull()]], "formats": ["audiobook"],
                                 "description": "Spice.", "updated_at": 1.0, "progress": [], "favorite": false])
        client.enqueueJSON(200, ["work_id": "w1", "title": "Dune", "asset_id": "as1", "kind": "audiobook", "format": "mp3",
                                 "files": [["id": "f1", "title": "Part 1", "part_number": 1, "size_bytes": 1000, "duration_seconds": 600, "url": "/api/v1/library/w1/files/f1"],
                                           ["id": "f2", "title": "Part 2", "part_number": 2, "size_bytes": 1000, "duration_seconds": 400, "url": "/api/v1/library/w1/files/f2"]],
                                 "chapters": [], "duration_seconds": 1000, "download_eligible": true,
                                 "progress": ["file_id": "f2", "position": 100, "location": NSNull(), "percent": 0.7, "updated_at": 5.0, "device_id": "x", "client_updated_at": 4.0, "duration": 400]])
        let d = try await provider.detail(id: "w1")
        XCTAssertEqual(d.assetID, "as1")
        XCTAssertEqual(d.tracks.count, 2)
        XCTAssertEqual(d.tracks[1].startOffset, 600)
        XCTAssertEqual(d.tracks[0].url.absoluteString, "https://books.example.com/api/v1/library/w1/files/f1")
        XCTAssertEqual(d.totalDuration, 1000)
        XCTAssertEqual(d.effectiveChapters.map(\.title), ["Part 1", "Part 2"])
        XCTAssertEqual(d.position?.trackIndex, 1)
        XCTAssertEqual(d.position?.positionInTrack, 100)
        XCTAssertEqual(d.position?.absolutePosition, 700)
        XCTAssertEqual(d.description, "Spice.")
        XCTAssertEqual(d.locate(absolute: 650).index, 1)
        XCTAssertEqual(d.locate(absolute: 650).offset, 50)
    }

    func testEbookDetailExposesDownloadURL() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["id": "w2", "title": "Emma", "authors": [], "narrators": [], "series": [], "has_cover": false,
                                 "assets": [["id": "e1", "kind": "ebook", "format": "epub", "readable": true]], "formats": ["ebook"], "updated_at": 1.0])
        client.enqueueJSON(200, ["work_id": "w2", "asset_id": "e1", "kind": "ebook", "format": "epub", "files": [["id": "f", "url": "/api/v1/library/w2/files/f"]],
                                 "download_url": "/api/v1/library/w2/files/f", "progress": ["location": "epubcfi(/6/4)", "percent": 0.2, "updated_at": 9.0]])
        let d = try await provider.detail(id: "w2")
        XCTAssertEqual(d.item.kind, .ebook)
        XCTAssertEqual(d.ebookFormat, "epub")
        XCTAssertEqual(d.ebookDownloadURL?.path, "/api/v1/library/w2/files/f")
        XCTAssertEqual(d.position?.location, "epubcfi(/6/4)")
    }

    func testWorkWithAudioAndEbookKeepsBothFormatsAndSeparateProgress() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["id": "w3", "title": "The Moonstone", "authors": [], "narrators": [], "series": [], "has_cover": false,
                                 "assets": [["id": "a1", "kind": "audiobook", "format": "m4b", "readable": true], ["id": "e1", "kind": "ebook", "format": "epub", "readable": true]],
                                 "formats": ["audiobook", "ebook"], "updated_at": 1.0, "progress": []])
        client.enqueueJSON(200, ["work_id": "w3", "asset_id": "a1", "kind": "audiobook", "format": "m4b",
                                 "files": [["id": "f1", "title": "Book", "size_bytes": 1000, "duration_seconds": 600, "url": "/api/v1/library/w3/files/f1"]],
                                 "chapters": [], "duration_seconds": 600, "progress": ["file_id": "f1", "position": 60, "percent": 0.1, "updated_at": 5.0]])
        client.enqueueJSON(200, ["work_id": "w3", "asset_id": "e1", "kind": "ebook", "format": "epub", "files": [["id": "ef", "url": "/api/v1/library/w3/files/ef"]],
                                 "download_url": "/api/v1/library/w3/files/ef", "progress": ["location": "epubcfi(/6/8)", "percent": 0.5, "updated_at": 9.0]])
        let d = try await provider.detail(id: "w3")
        XCTAssertTrue(d.hasAudio)
        XCTAssertTrue(d.hasEbook)
        XCTAssertEqual(d.assetID, "a1")
        XCTAssertEqual(d.ebookAssetID, "e1")
        XCTAssertEqual(d.readingAssetID, "e1")
        XCTAssertEqual(d.ebookDownloadURL?.path, "/api/v1/library/w3/files/ef")
        XCTAssertEqual(d.ebookFormat, "epub")
        XCTAssertEqual(d.position?.absolutePosition, 60)
        XCTAssertNil(d.position?.location)
        XCTAssertEqual(d.readingPosition?.location, "epubcfi(/6/8)")
        XCTAssertEqual(d.readingPosition?.percent, 0.5)
        let plan = DownloadPlan(serverID: server.id, detail: d, headers: [:])
        XCTAssertEqual(plan.files.count, 2)
    }

    func testProgressReportSendsBatchItemAndHandlesStale() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        let at = Date(timeIntervalSince1970: 1760000000)
        let update = ProgressUpdate(serverID: server.id, itemID: "w1", assetID: "as1", trackID: "f2", trackIndex: 1, positionInTrack: 120,
                                    absolutePosition: 720, duration: 400, percent: 0.72, clientUpdatedAt: at)
        client.enqueueJSON(200, ["items": [["asset_id": "as1", "applied": true]], "server_time": 1.0])
        var r = try await provider.reportProgress(update, session: nil)
        XCTAssertTrue(r.applied)
        let body = try XCTUnwrap(client.last?.bodyJSON)
        let items = try XCTUnwrap(body["items"] as? [[String: Any]])
        XCTAssertEqual(items.first?["file_id"] as? String, "f2")
        XCTAssertEqual(items.first?["position"] as? Double, 120)
        XCTAssertEqual(items.first?["device_id"] as? String, "dev-1")
        XCTAssertEqual(items.first?["client_updated_at"] as? Double, 1760000000)
        XCTAssertEqual(client.last?.method, .put)

        client.enqueueJSON(200, ["items": [["asset_id": "as1", "applied": false, "file_id": "f1", "position": 30, "percent": 0.1, "updated_at": 2.0, "client_updated_at": 1760000500]]])
        r = try await provider.reportProgress(update, session: nil)
        XCTAssertFalse(r.applied)
        XCTAssertEqual(r.serverPosition?.trackID, "f1")
        XCTAssertEqual(r.serverPosition?.positionInTrack, 30)

        client.enqueueJSON(200, ["items": [["asset_id": "as1", "applied": false, "error": "not_found"]]])
        do { _ = try await provider.reportProgress(update, session: nil); XCTFail() } catch { XCTAssertEqual(error as? APIError, .notFound) }
    }

    func testContinueListeningMapsFlatRows() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["items": [["asset_id": "as1", "kind": "audiobook", "file_id": "f1", "position": 5, "percent": 0.4, "updated_at": 3.0,
                                            "work_id": "w1", "title": "Dune", "has_cover": true, "authors": "Frank Herbert, Brian Herbert"]]])
        let items = try await provider.continueListening()
        XCTAssertEqual(items.first?.id, "w1")
        XCTAssertEqual(items.first?.authors, ["Frank Herbert", "Brian Herbert"])
        XCTAssertEqual(items.first?.progress, 0.4)
        XCTAssertNotNil(items.first?.coverURL)
    }

    private var mixedContinue: [String: Any] {
        ["items": [
            ["kind": "podcast", "episode_id": "ep4", "show_id": "show1", "show_title": "The Unexplainable Disappearance of Mars Patel",
             "title": "S2 E4: Aurora", "duration_seconds": 1019.3, "season_number": 2, "is_extras": false, "position_seconds": 244.0, "updated_at": 1794800355.57],
            ["asset_id": "as1", "kind": "audiobook", "percent": 0.4, "updated_at": 3.0, "work_id": "w1", "title": "Dune", "has_cover": true, "authors": "Frank Herbert"],
            ["kind": "podcast", "title": "No ids", "position_seconds": 5.0],
            ["kind": "podcast", "episode_id": "ep9", "show_id": "show1", "show_title": "Mars", "title": "S1 E1", "position_seconds": 10.0],
        ]]
    }

    func testContinueListeningKeepsOrderAndMapsEpisodes() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, mixedContinue)
        let items = try await provider.continueListening()
        XCTAssertEqual(items.map(\.id), ["ep4", "w1", "ep9"])
        let ep = items[0]
        XCTAssertTrue(ep.isEpisode)
        XCTAssertEqual(ep.title, "S2 E4: Aurora")
        XCTAssertEqual(ep.authors, ["The Unexplainable Disappearance of Mars Patel"])
        XCTAssertEqual(ep.showID, "show1")
        XCTAssertEqual(ep.coverURL?.path, "/api/player/v1/podcasts/shows/show1/cover")
        XCTAssertEqual(try XCTUnwrap(ep.progress), 244.0 / 1019.3, accuracy: 0.0001)
        XCTAssertEqual(ep.assetID, "podcast:ep4")
        XCTAssertFalse(items[1].isEpisode)
        XCTAssertNil(items[2].progress)
    }

    func testContinueEpisodeDetailResumesAtStoredPosition() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, mixedContinue)
        let ep = try await provider.continueListening()[0]
        client.enqueueJSON(200, ["progress": ["position_seconds": 244.0]])
        let detail = try await provider.continueEpisodeDetail(ep)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts/episodes/ep4/progress")
        XCTAssertEqual(detail.position?.absolutePosition, 244.0)
        XCTAssertEqual(detail.assetID, "podcast:ep4")
        XCTAssertEqual(detail.tracks.first?.url.path, "/api/player/v1/podcasts/episodes/ep4/audio")
        XCTAssertEqual(detail.item.authors, ["The Unexplainable Disappearance of Mars Patel"])
    }

    func testErrorMapping() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueue(HTTPResponse(status: 429, headers: ["Retry-After": "30"], body: Data()))
        do { _ = try await provider.detail(id: "x"); XCTFail() } catch { XCTAssertEqual(error as? APIError, .rateLimited(retryAfter: 30)) }
        client.enqueueJSON(403, ["detail": "Your account is awaiting approval"])
        do { _ = try await provider.detail(id: "x"); XCTFail() } catch { XCTAssertEqual(error as? APIError, .forbidden("Your account is awaiting approval")) }
    }
}
