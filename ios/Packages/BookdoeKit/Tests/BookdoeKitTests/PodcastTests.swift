import XCTest
@testable import BookdoeKit

/// The #514 show response: seasons 1, 2, 10 and Extras, in the server's order.
let podcastFixture: [String: Any] = [
    "id": "show-1", "title": "Alpha Show", "cover_url": "/api/v1/podcasts/show-1/cover",
    "seasons": [
        ["id": "s1", "number": 1, "is_extras": false, "episodes": [
            ["id": "e1", "title": "One", "release_date": "2024-01-01", "duration_seconds": 600.0, "description": "<p>First &amp; best</p>",
             "missing": false, "played": true, "position_seconds": 600.0]]],
        ["id": "s2", "number": 2, "is_extras": false, "episodes": [
            ["id": "e2", "title": "Two", "release_date": "2024-03-01", "duration_seconds": 600.0, "description": "",
             "missing": false, "played": false, "position_seconds": 0.0]]],
        ["id": "s10", "number": 10, "is_extras": false, "episodes": []],
        ["id": "sx", "number": NSNull(), "is_extras": true, "episodes": [
            ["id": "e3", "title": "Recap", "release_date": "2024-05-01", "duration_seconds": 300.0, "description": "Bonus",
             "missing": true, "played": false, "position_seconds": 0.0]]],
    ],
]

final class PodcastShowViewModelTests: XCTestCase {
    private let base = URL(string: "https://books.example.com")!

    private func model() throws -> PodcastShowViewModel {
        PodcastShowViewModel(show: try XCTUnwrap(PodcastShow.parse(podcastFixture, baseURL: base)))
    }

    func testCoverIsTheDeviceReachableRoute() throws {
        XCTAssertEqual(try model().coverURL?.absoluteString, "https://books.example.com/api/player/v1/podcasts/shows/show-1/cover")
    }

    func testSeasonsAreNumberedInOrderWithExtrasLast() throws {
        XCTAssertEqual(try model().seasonTitles.map(\.title), ["Season 1", "Season 2", "Season 10", "Extras"])
    }

    func testRowsShowTitleDateDescriptionAndPlayedState() throws {
        var vm = try model()
        XCTAssertEqual(vm.rows, [PodcastEpisodeRow(id: "e1", title: "One", dateText: "2024-01-01", description: "First & best", played: true, playable: true)])
        vm.select(seasonID: "s2")
        XCTAssertEqual(vm.rows.first?.played, false)
    }

    func testExtrasSeasonRowsAndMissingEpisodeNotPlayable() throws {
        var vm = try model()
        vm.select(seasonID: "sx")
        XCTAssertEqual(vm.rows.map(\.title), ["Recap"])
        XCTAssertEqual(vm.rows.first?.playable, false)
    }

    func testSelectingUnknownSeasonKeepsSelection() throws {
        var vm = try model()
        vm.select(seasonID: "nope")
        XCTAssertEqual(vm.selectedSeasonID, "s1")
    }
}

@MainActor
private final class FakePlayer: EpisodePlayer {
    var loaded: [MediaDetail] = []
    func load(_ detail: MediaDetail) async { loaded.append(detail) }
}

@MainActor
final class PodcastPlaybackTests: XCTestCase {
    private func makeProvider(_ client: MockHTTPClient) -> BookdoeProvider {
        let creds = InMemoryCredentialStore()
        let server = makeServer(.bookdoe)
        try? creds.save(Credential(accessToken: "alxd_tok"), for: server.id)
        return BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1.0")
    }

    private func show() throws -> PodcastShow { try XCTUnwrap(PodcastShow.parse(podcastFixture, baseURL: URL(string: "https://books.example.com")!)) }

    func testShowIsFetchedFromThePlayerRouteWithTheDeviceToken() async throws {
        let client = MockHTTPClient()
        client.enqueueJSON(200, podcastFixture)
        let show = try await makeProvider(client).podcastShow(id: "show-1")
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts/shows/show-1")
        XCTAssertEqual(client.last?.headers["Authorization"], "Bearer alxd_tok")
        XCTAssertEqual(show.seasons.count, 4)
    }

    func testShowListUsesThePlayerRoute() async throws {
        let client = MockHTTPClient()
        client.enqueueJSON(200, ["shows": [["id": "show-1", "title": "Alpha Show", "cover_url": "/x", "season_count": 3, "has_extras": true]]])
        let shows = try await makeProvider(client).podcastShows()
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts")
        XCTAssertEqual(shows, [PodcastShowRef(id: "show-1", title: "Alpha Show")])
    }

    func testTappingEpisodeLoadsItIntoPlayerAtStoredPositionWithShowName() async throws {
        let client = MockHTTPClient()
        client.enqueueJSON(200, ["episode_id": "e2", "progress": ["position_seconds": 123.5, "client_updated_at": 1.0, "played": false]])
        let player = FakePlayer()
        let s = try show()
        try await PodcastPlayback.play(s.seasons[1].episodes[0], of: s, provider: makeProvider(client), player: player)
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts/episodes/e2/progress")
        let d = try XCTUnwrap(player.loaded.first)
        XCTAssertEqual(d.position?.absolutePosition, 123.5)
        XCTAssertEqual(d.position?.positionInTrack, 123.5)
        XCTAssertEqual(d.tracks.first?.url.absoluteString, "https://books.example.com/api/player/v1/podcasts/episodes/e2/audio")
        XCTAssertEqual(d.item.title, "Two")          // lock-screen title
        XCTAssertEqual(d.item.authorLine, "Alpha Show")   // lock-screen artist
    }

    func testNoSavedPositionStartsAtZero() async throws {
        let client = MockHTTPClient()
        client.enqueueJSON(200, ["episode_id": "e2", "progress": NSNull()])
        let player = FakePlayer()
        let s = try show()
        try await PodcastPlayback.play(s.seasons[1].episodes[0], of: s, provider: makeProvider(client), player: player)
        XCTAssertEqual(player.loaded.first?.position?.absolutePosition, 0)
    }

    func testPositionChangeIsWrittenToThePodcastProgressEndpoint() async throws {
        let client = MockHTTPClient()
        client.enqueueJSON(200, ["episode_id": "e2", "progress": NSNull()])
        let player = FakePlayer()
        let provider = makeProvider(client)
        let s = try show()
        try await PodcastPlayback.play(s.seasons[1].episodes[0], of: s, provider: provider, player: player)
        let detail = try XCTUnwrap(player.loaded.first)
        let stamp = Date(timeIntervalSince1970: 1_700_000_000)
        client.enqueueJSON(200, ["episode_id": "e2", "applied": true, "progress": ["position_seconds": 42.0, "client_updated_at": 1_700_000_000, "played": false]])
        let update = ProgressUpdate(serverID: provider.server.id, itemID: detail.item.id, assetID: detail.assetID, trackID: "e2",
                                    positionInTrack: 42, absolutePosition: 42, clientUpdatedAt: stamp)
        let report = try await provider.reportProgress(update, session: nil)
        XCTAssertTrue(report.applied)
        let req = try XCTUnwrap(client.last)
        XCTAssertEqual(req.method, .put)
        XCTAssertEqual(req.url.path, "/api/player/v1/podcasts/episodes/e2/progress")
        let body = try JSONSerialization.jsonObject(with: try XCTUnwrap(req.body)) as? [String: Any]
        XCTAssertEqual(body?["position_seconds"] as? Double, 42)
        XCTAssertEqual(body?["client_updated_at"] as? Double, 1_700_000_000)
    }

    func testStaleWriteReturnsServerPosition() async throws {
        let client = MockHTTPClient()
        client.enqueueJSON(200, ["episode_id": "e2", "applied": false, "progress": ["position_seconds": 900.0, "client_updated_at": 5.0, "played": false]])
        let update = ProgressUpdate(serverID: UUID(), itemID: "e2", assetID: "podcast:e2", absolutePosition: 10)
        let report = try await makeProvider(client).reportProgress(update, session: nil)
        XCTAssertFalse(report.applied)
        XCTAssertEqual(report.serverPosition?.absolutePosition, 900)
    }
}
