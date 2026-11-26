import XCTest
@testable import BookdoeKit

final class JellyfinProviderTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: JellyfinProvider!
    let server = makeServer(.jellyfin, base: "https://jf.example.com/jellyfin", libraryID: "lib1", userID: "user1")

    override func setUp() {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        provider = JellyfinProvider(server: server, client: client, credentials: creds, appVersion: "1.0")
    }

    func testProbe() async throws {
        client.enqueueJSON(200, ["ServerName": "Stable Demo", "Version": "12.2.0", "Id": "srv"])
        let info = try await provider.probe()
        XCTAssertEqual(info.name, "Stable Demo")
        XCTAssertEqual(info.version, "12.2.0")
        XCTAssertEqual(client.last?.url.absoluteString, "https://jf.example.com/jellyfin/System/Info/Public")
    }

    func testLoginUsesPwFieldAndMediaBrowserHeader() async throws {
        let fresh = JellyfinProvider(server: makeServer(.jellyfin, base: "https://jf.example.com"), client: client, credentials: creds, appVersion: "1.0")
        client.enqueueJSON(200, ["AccessToken": "jftok", "ServerId": "s", "User": ["Id": "u9", "Name": "demo"], "SessionInfo": [:]])
        client.enqueueJSON(200, ["Items": [["Id": "v1", "Name": "Movies", "CollectionType": "movies"], ["Id": "v2", "Name": "Audiobooks", "CollectionType": "books"]]])
        let r = try await fresh.login(username: "demo", password: "secret", deviceName: "iPhone")
        XCTAssertEqual(r.credential.accessToken, "jftok")
        XCTAssertEqual(r.server.userID, "u9")
        XCTAssertEqual(r.server.libraryID, "v2", "the books view is picked by default")
        let login = try XCTUnwrap(client.request(matching: "/Users/AuthenticateByName"))
        XCTAssertEqual(login.bodyJSON?["Pw"] as? String, "secret")
        XCTAssertEqual(login.bodyJSON?["Username"] as? String, "demo")
        let auth = try XCTUnwrap(login.headers["Authorization"])
        XCTAssertTrue(auth.hasPrefix("MediaBrowser "))
        XCTAssertTrue(auth.contains("Client=\"bookdoe\"") && auth.contains("DeviceId=\"dev-1\""))
        XCTAssertFalse(auth.contains("Token="))
        XCTAssertEqual(try creds.credential(for: fresh.server.id)?.accessToken, "jftok")
        let views = try XCTUnwrap(client.request(matching: "/UserViews"))
        XCTAssertTrue(views.headers["Authorization"]?.contains("Token=\"jftok\"") ?? false)
    }

    func testBrowseHidesChildrenOfListedFolders() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["Items": [
            ["Id": "folder1", "Name": "Dune", "Type": "Folder", "ChildCount": 2],
            ["Id": "file1", "Name": "Dune Part 1", "Type": "AudioBook", "ParentId": "folder1", "RunTimeTicks": 6_000_000_000],
            ["Id": "single", "Name": "Emma", "Type": "AudioBook", "ParentId": "lib1", "RunTimeTicks": 36_000_000_000, "Artists": ["Jane Austen"],
             "UserData": ["PlaybackPositionTicks": 18_000_000_000, "Played": false], "ImageTags": ["Primary": "abc"]],
        ], "TotalRecordCount": 3])
        let page = try await provider.browse(query: nil, kind: nil, sort: .added, offset: 0, limit: 50)
        XCTAssertEqual(page.items.map(\.id), ["folder1", "single"])
        let emma = page.items[1]
        XCTAssertEqual(emma.durationSeconds, 3600)
        XCTAssertEqual(emma.progress, 0.5)
        XCTAssertEqual(emma.authors, ["Jane Austen"])
        XCTAssertEqual(emma.coverURL?.path, "/jellyfin/Items/single/Images/Primary")
        let q = try XCTUnwrap(client.last?.url.queryDict)
        XCTAssertEqual(q["parentId"], "lib1")
        XCTAssertEqual(q["userId"], "user1")
        XCTAssertEqual(q["sortBy"], "DateCreated")
        XCTAssertEqual(q["startIndex"], "0")
        XCTAssertEqual(q["recursive"], "true")
    }

    func testBrowseListsNestedPlainBookFolderNotContainers() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["Items": [
            ["Id": "media", "Name": "media", "Type": "Folder", "ParentId": "lib1", "ChildCount": 1],
            ["Id": "author", "Name": "Author", "Type": "Folder", "ParentId": "media", "ChildCount": 1],
            ["Id": "book", "Name": "Book", "Type": "Folder", "ParentId": "author", "ChildCount": 3],
            ["Id": "f1", "Name": "01", "Type": "Audio", "ParentId": "book"],
            ["Id": "f2", "Name": "02", "Type": "Audio", "ParentId": "book"],
            ["Id": "f3", "Name": "03", "Type": "Audio", "ParentId": "book"],
        ], "TotalRecordCount": 6])
        let page = try await provider.browse(query: nil, kind: nil, sort: .title, offset: 0, limit: 50)
        XCTAssertEqual(page.items.map(\.id), ["book"])
    }

    func testBrowseListsTaggedAlbumAndHidesItsTracks() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["Items": [
            ["Id": "album", "Name": "Dune", "Type": "MusicAlbum", "ParentId": "lib1", "AlbumArtists": [["Name": "Frank Herbert"]], "ChildCount": 2],
            ["Id": "t1", "Name": "One", "Type": "Audio", "ParentId": "album", "Album": "Dune", "Artists": ["Frank Herbert"]],
            ["Id": "t2", "Name": "Two", "Type": "Audio", "ParentId": "album", "Album": "Dune", "Artists": ["Frank Herbert"]],
        ], "TotalRecordCount": 3])
        let page = try await provider.browse(query: nil, kind: nil, sort: .title, offset: 0, limit: 50)
        XCTAssertEqual(page.items.map(\.id), ["album"])
        XCTAssertEqual(page.items[0].authors, ["Frank Herbert"])
    }

    func testBrowseHidesLibraryRootFolderButKeepsLooseFiles() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["Items": [
            ["Id": "lib1", "Name": "media", "Type": "Folder", "ChildCount": 1],
            ["Id": "loose", "Name": "Emma", "Type": "Audio", "ParentId": "lib1"],
        ], "TotalRecordCount": 2])
        let page = try await provider.browse(query: nil, kind: nil, sort: .title, offset: 0, limit: 50)
        XCTAssertEqual(page.items.map(\.id), ["loose"])
    }

    func testDetailOfFolderOrdersChildrenAndFindsPosition() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueueJSON(200, ["Id": "folder1", "Name": "Dune", "Type": "Folder", "Overview": "Spice"])
        client.enqueueJSON(200, ["Items": [
            ["Id": "c1", "Name": "Part 1", "Type": "AudioBook", "IndexNumber": 1, "RunTimeTicks": 6_000_000_000, "MediaSources": [["Id": "m", "Container": "mp3"]], "UserData": ["PlaybackPositionTicks": 0]],
            ["Id": "c2", "Name": "Part 2", "Type": "AudioBook", "IndexNumber": 2, "RunTimeTicks": 4_000_000_000, "UserData": ["PlaybackPositionTicks": 1_000_000_000, "LastPlayedDate": "2026-10-08T10:00:00Z"]],
        ]])
        let d = try await provider.detail(id: "folder1")
        XCTAssertEqual(d.tracks.count, 2)
        XCTAssertEqual(d.tracks[1].startOffset, 600)
        XCTAssertEqual(d.totalDuration, 1000)
        XCTAssertEqual(d.tracks[0].mimeType, "audio/mp3")
        XCTAssertEqual(d.tracks[0].url.path, "/jellyfin/Audio/c1/universal")
        XCTAssertEqual(d.tracks[0].url.queryDict["api_key"], "t")
        XCTAssertEqual(d.position?.trackIndex, 1)
        XCTAssertEqual(d.position?.positionInTrack, 100)
        XCTAssertEqual(d.position?.absolutePosition, 700)
        XCTAssertEqual(d.position?.percent, 0.7)
        XCTAssertNotNil(d.position?.updatedAt)
        XCTAssertEqual(d.description, "Spice")
    }

    func testPlaybackReportsUseTicksAndSession() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        let detail = MediaDetail(item: MediaItem(id: "single", title: "Emma", kind: .audiobook), tracks: [Track(id: "single", index: 0, title: "Emma", url: URL(string: "https://x/y")!, durationSeconds: 100)], assetID: "single")
        client.enqueue(HTTPResponse(status: 204))
        let session = try await provider.startPlayback(detail)
        XCTAssertNotNil(session.sessionID)
        XCTAssertEqual(client.last?.url.path, "/jellyfin/Sessions/Playing")
        XCTAssertEqual(client.last?.bodyJSON?["ItemId"] as? String, "single")

        let update = ProgressUpdate(serverID: server.id, itemID: "single", assetID: "single", trackID: "single", positionInTrack: 12.5, absolutePosition: 12.5, duration: 100)
        client.enqueue(HTTPResponse(status: 204))
        let r = try await provider.reportProgress(update, session: session)
        XCTAssertTrue(r.applied)
        XCTAssertEqual(client.last?.url.path, "/jellyfin/Sessions/Playing/Progress")
        XCTAssertEqual(client.last?.bodyJSON?["PositionTicks"] as? Int64, 125_000_000)
        XCTAssertEqual(client.last?.bodyJSON?["PlaySessionId"] as? String, session.sessionID)

        client.enqueue(HTTPResponse(status: 204))
        try await provider.stopPlayback(update, session: session)
        XCTAssertEqual(client.last?.url.path, "/jellyfin/Sessions/Playing/Stopped")

        var finished = update; finished.isFinished = true
        client.enqueue(HTTPResponse(status: 204))
        client.enqueueJSON(200, ["Played": true])
        _ = try await provider.reportProgress(finished, session: session)
        XCTAssertEqual(client.last?.url.path, "/jellyfin/UserPlayedItems/single")
    }

    func testLogoutRemovesCredentialEvenIfServerFails() async throws {
        try creds.save(Credential(accessToken: "t"), for: server.id)
        client.enqueue(HTTPResponse(status: 500))
        try await provider.logout()
        XCTAssertNil(try creds.credential(for: server.id))
        XCTAssertEqual(client.last?.url.path, "/jellyfin/Sessions/Logout")
    }
}
