import XCTest
@testable import BookdoeKit

final class CarPlayStateMapperTests: XCTestCase {

    private func assertPlaceholder(_ sections: [CarPlaySectionModel], _ text: String,
                                   file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(sections.count, 1, file: file, line: line)
        XCTAssertEqual(sections.first?.items.count, 1, file: file, line: line)
        XCTAssertEqual(sections.first?.items.first?.text, text, file: file, line: line)
        XCTAssertEqual(sections.first?.items.first?.isPlaceholder, true, file: file, line: line)
        XCTAssertNil(sections.first?.items.first?.id, file: file, line: line)
    }

    private func assertPlaceholder(_ state: CarPlayLoadState, _ text: String,
                                   file: StaticString = #filePath, line: UInt = #line) {
        assertPlaceholder(CarPlayStateMapper.sections(for: state), text, file: file, line: line)
    }

    func testLoadingState() { assertPlaceholder(.loading, "Loading\u{2026}") }
    func testEmptyState() { assertPlaceholder(.empty, "No books in library") }
    func testOfflineState() { assertPlaceholder(.offline, "Offline \u{2014} tap to retry") }
    func testErrorState() { assertPlaceholder(.error(message: "Connection failed"), "Connection failed") }

    func testReadyStateWithItems() {
        let items = [
            CarPlayBookRow(id: "b1", title: "Dune", subtitle: "Frank Herbert"),
            CarPlayBookRow(id: "b2", title: "Foundation", subtitle: "Isaac Asimov"),
        ]
        let sections = CarPlayStateMapper.sections(for: .ready(items: items), header: "Library")
        XCTAssertEqual(sections.count, 1)
        XCTAssertEqual(sections[0].header, "Library")
        XCTAssertEqual(sections[0].items.count, 2)
        XCTAssertEqual(sections[0].items[0], CarPlayRowModel(id: "b1", text: "Dune", detailText: "Frank Herbert"))
        XCTAssertEqual(sections[0].items[1].text, "Foundation")
        XCTAssertFalse(sections[0].items[1].isPlaceholder)
    }

    func testReadyStateWithNoItemsFallsBackToEmptyPlaceholder() {
        assertPlaceholder(.ready(items: []), "No books in library")
    }

    func testEmptyTextIsPerTab() {
        assertPlaceholder(CarPlayStateMapper.sections(for: .empty, emptyText: "Nothing in progress"), "Nothing in progress")
    }

    func testNetworkErrorsMapToOffline() {
        XCTAssertEqual(CarPlayStateMapper.state(for: APIError.network("timed out")), .offline)
        XCTAssertEqual(CarPlayStateMapper.state(for: URLError(.notConnectedToInternet)), .offline)
    }

    func testOtherErrorsMapToErrorWithDescription() {
        XCTAssertEqual(CarPlayStateMapper.state(for: APIError.notFound), .error(message: "Not found on the server."))
    }
}

final class CarPlayContinueMapperTests: XCTestCase {
    private let show = "The Unexplainable Disappearance of Mars Patel"
    private var items: [MediaItem] {
        [MediaItem(id: "ep4", title: "S2 E4: Aurora", authors: [show], kind: .audiobook, durationSeconds: 1019.3,
                   coverURL: URL(string: "https://x.test/api/player/v1/podcasts/shows/show1/cover"), progress: 244.0 / 1019.3,
                   assetID: "podcast:ep4", isEpisode: true, showID: "show1"),
         MediaItem(id: "w1", title: "Dune", authors: ["Frank Herbert"], kind: .audiobook, progress: 0.4),
         MediaItem(id: "w2", title: "Emma", authors: ["Jane Austen"], kind: .ebook, progress: 0.1)]
    }

    func testEpisodesAreIncludedFirstWithShowCoverAndPercent() {
        let rows = CarPlayContinueMapper.rows(items, limit: 20)
        XCTAssertEqual(rows.map(\.title), ["S2 E4: Aurora", "Dune"])   // ebook dropped, order kept
        XCTAssertEqual(rows[0].id, "podcast:ep4")
        XCTAssertEqual(rows[0].subtitle, "\(show) · 24%")
        XCTAssertEqual(rows[0].coverURL?.path, "/api/player/v1/podcasts/shows/show1/cover")
        XCTAssertEqual(rows[1].id, "w1")
    }

    func testRowIDTellsEpisodesFromBooksWithoutState() {
        let rows = CarPlayContinueMapper.rows(items, limit: 20)
        XCTAssertEqual(CarPlayContinueMapper.episodeID(fromRowID: rows[0].id ?? ""), "ep4")
        XCTAssertNil(CarPlayContinueMapper.episodeID(fromRowID: "w1"))
    }

    func testLimitCapsRows() {
        XCTAssertEqual(CarPlayContinueMapper.rows(items, limit: 1).map(\.id), ["podcast:ep4"])
    }
}
