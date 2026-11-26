import XCTest
@testable import BookdoeKit

private final class FakeSource: PodcastNextSource, @unchecked Sendable {
    var next: [String: PodcastEpisode] = [:]
    var asked: [String] = []
    var loadedShows: [PodcastShow] = []

    func nextPodcastEpisode(after episodeID: String) async throws -> PodcastEpisode? {
        asked.append(episodeID)
        return next[episodeID]
    }

    func podcastEpisodeDetail(_ episode: PodcastEpisode, of show: PodcastShow) async throws -> MediaDetail {
        loadedShows.append(show)
        let item = MediaItem(id: episode.id, title: episode.title, authors: [show.title], kind: .audiobook, coverURL: show.coverURL)
        let track = Track(id: episode.id, index: 0, title: episode.title, url: URL(string: "https://x.test/\(episode.id)")!)
        return MediaDetail(item: item, tracks: [track],
                           position: PlaybackPosition(trackID: episode.id, trackIndex: 0, positionInTrack: episode.positionSeconds, absolutePosition: episode.positionSeconds),
                           assetID: "podcast:" + episode.id)
    }
}

final class PodcastAutoAdvanceTests: XCTestCase {
    private func finished(_ id: String = "e4") -> MediaDetail {
        let item = MediaItem(id: id, title: "E4", authors: ["Mars Patel"], kind: .audiobook, coverURL: URL(string: "https://x.test/cover"))
        return MediaDetail(item: item, tracks: [Track(id: id, index: 0, title: "E4", url: URL(string: "https://x.test/\(id)")!)], assetID: "podcast:" + id)
    }

    func testEpisodeEndLoadsTheNextAtItsSavedPosition() async throws {
        let src = FakeSource()
        src.next["e4"] = PodcastEpisode(id: "e5", title: "E5", positionSeconds: 77)
        let d = try await PodcastAutoAdvance.nextDetail(after: finished(), source: src, enabled: true)
        XCTAssertEqual(src.asked, ["e4"])
        XCTAssertEqual(d?.item.id, "e5")
        XCTAssertEqual(d?.assetID, "podcast:e5")
        XCTAssertEqual(d?.position?.absolutePosition, 77)
        XCTAssertEqual(d?.item.authorLine, "Mars Patel")
        XCTAssertEqual(d?.item.coverURL?.absoluteString, "https://x.test/cover")
    }

    func testNewNextEpisodeStartsAtZero() async throws {
        let src = FakeSource()
        src.next["e4"] = PodcastEpisode(id: "e5", title: "E5")
        let d = try await PodcastAutoAdvance.nextDetail(after: finished(), source: src, enabled: true)
        XCTAssertEqual(d?.position?.absolutePosition, 0)
    }

    func testNoNextEpisodeStops() async throws {
        let src = FakeSource()
        let d = try await PodcastAutoAdvance.nextDetail(after: finished(), source: src, enabled: true)
        XCTAssertNil(d)
        XCTAssertEqual(src.asked, ["e4"])
    }

    func testSettingOffStopsWithoutAskingTheServer() async throws {
        let src = FakeSource()
        src.next["e4"] = PodcastEpisode(id: "e5", title: "E5")
        let d = try await PodcastAutoAdvance.nextDetail(after: finished(), source: src, enabled: false)
        XCTAssertNil(d)
        XCTAssertTrue(src.asked.isEmpty)
    }

    func testSeasonBoundaryFollowsTheServersNext() async throws {
        let src = FakeSource()
        src.next["s1-last"] = PodcastEpisode(id: "s2-e1", title: "S2 E1")
        let d = try await PodcastAutoAdvance.nextDetail(after: finished("s1-last"), source: src, enabled: true)
        XCTAssertEqual(d?.item.id, "s2-e1")
    }

    func testBooksDoNotAdvance() async throws {
        let src = FakeSource()
        var book = finished()
        book.assetID = "asset-1"
        let d = try await PodcastAutoAdvance.nextDetail(after: book, source: src, enabled: true)
        XCTAssertNil(d)
        XCTAssertTrue(src.asked.isEmpty)
    }

    func testDownloadedNextPlaysTheLocalFile() async throws {
        let src = FakeSource()
        src.next["e4"] = PodcastEpisode(id: "e5", title: "E5", positionSeconds: 12)
        var local = try await src.podcastEpisodeDetail(PodcastEpisode(id: "e5", title: "E5"), of: PodcastShow(id: "s", title: "S"))
        local.tracks[0].url = URL(fileURLWithPath: "/tmp/e5.mp3")
        src.loadedShows = []
        let d = try await PodcastAutoAdvance.nextDetail(after: finished(), source: src, enabled: true) { $0 == "e5" ? local : nil }
        XCTAssertTrue(d?.tracks[0].url.isFileURL ?? false)
        XCTAssertEqual(d?.position?.absolutePosition, 12)
        XCTAssertTrue(src.loadedShows.isEmpty)
    }

    func testSettingDefaultsOn() {
        let defaults = UserDefaults(suiteName: "PodcastAutoAdvanceTests")!
        defaults.removePersistentDomain(forName: "PodcastAutoAdvanceTests")
        XCTAssertTrue(PodcastAutoAdvance.isEnabled(defaults))
        defaults.set(false, forKey: PodcastAutoAdvance.settingKey)
        XCTAssertFalse(PodcastAutoAdvance.isEnabled(defaults))
    }

    func testProviderParsesNextAndNull() async throws {
        let client = MockHTTPClient()
        let creds = InMemoryCredentialStore()
        let server = makeServer(.bookdoe)
        try? creds.save(Credential(accessToken: "alxd_tok"), for: server.id)
        let p = BookdoeProvider(server: server, client: client, credentials: creds, appVersion: "1.0")
        client.enqueueJSON(200, ["episode": ["id": "e5", "title": "E5", "position_seconds": 30.0, "played": false, "missing": false]])
        let e = try await p.nextPodcastEpisode(after: "e4")
        XCTAssertEqual(client.last?.url.path, "/api/player/v1/podcasts/episodes/e4/next")
        XCTAssertEqual(e?.id, "e5")
        XCTAssertEqual(e?.positionSeconds, 30)
        client.enqueueJSON(200, ["episode": NSNull()])
        let none = try await p.nextPodcastEpisode(after: "e5")
        XCTAssertNil(none)
    }
}

/// The engine's end-of-episode path (PlayerEngine.itemEnded / skipToNextEpisode delegate to PodcastEpisodeAdvancer).
@MainActor
final class PodcastEpisodeAdvancerTests: XCTestCase {
    private final class Player {
        var current: MediaDetail?
        var events: [String] = []
    }

    private func make(_ player: Player, src: FakeSource, enabled: Bool = true) -> PodcastEpisodeAdvancer {
        PodcastEpisodeAdvancer(source: src, isEnabled: { enabled }, currentItemID: { player.current?.item.id },
                               stop: { report in player.events.append("stop(report: \(report))"); player.current = nil },
                               load: { d in player.events.append("load(\(d.item.id))"); player.current = d })
    }

    private func episode(_ id: String) -> MediaDetail {
        let item = MediaItem(id: id, title: id, authors: ["Mars Patel"], kind: .audiobook)
        return MediaDetail(item: item, tracks: [Track(id: id, index: 0, title: id, url: URL(string: "https://x.test/\(id)")!)], assetID: "podcast:" + id)
    }

    func testEndLoadsNextWhichBecomesCurrentWithoutReportingTwice() async {
        let src = FakeSource(); src.next["e4"] = PodcastEpisode(id: "e5", title: "E5", positionSeconds: 9)
        let player = Player(); player.current = episode("e4")
        await make(player, src: src).episodeEnded(episode("e4"))
        XCTAssertEqual(player.current?.item.id, "e5")
        XCTAssertEqual(player.current?.position?.absolutePosition, 9)
        XCTAssertEqual(player.events, ["stop(report: false)", "load(e5)"])
    }

    func testSettingOffLoadsNothing() async {
        let src = FakeSource(); src.next["e4"] = PodcastEpisode(id: "e5", title: "E5")
        let player = Player(); player.current = episode("e4")
        await make(player, src: src, enabled: false).episodeEnded(episode("e4"))
        XCTAssertTrue(player.events.isEmpty)
        XCTAssertEqual(player.current?.item.id, "e4")
    }

    func testNoNextEpisodeLoadsNothing() async {
        let player = Player(); player.current = episode("e4")
        await make(player, src: FakeSource()).episodeEnded(episode("e4"))
        XCTAssertTrue(player.events.isEmpty)
    }

    func testUserMovedOnLoadsNothing() async {
        let src = FakeSource(); src.next["e4"] = PodcastEpisode(id: "e5", title: "E5")
        let player = Player(); player.current = episode("other")
        await make(player, src: src).episodeEnded(episode("e4"))
        XCTAssertTrue(player.events.isEmpty)
    }

    func testSkipLoadsNextAndLetsLoadReportThePosition() async {
        let src = FakeSource(); src.next["e4"] = PodcastEpisode(id: "e5", title: "E5")
        let player = Player(); player.current = episode("e4")
        // The setting is off, yet a manual next still skips.
        await make(player, src: src, enabled: false).skip(episode("e4"))
        XCTAssertEqual(player.current?.item.id, "e5")
        XCTAssertEqual(player.events, ["load(e5)"])  // no stop(report: false): load() reports the skipped episode's position
    }

    func testSkipIgnoresBooks() async {
        let src = FakeSource()
        var book = episode("b"); book.assetID = "asset-1"
        let player = Player(); player.current = book
        await make(player, src: src).skip(book)
        XCTAssertTrue(src.asked.isEmpty)
        XCTAssertTrue(player.events.isEmpty)
    }
}
