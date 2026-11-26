import XCTest
@testable import BookdoeKit

final class CarPlayPodcastMapperTests: XCTestCase {
    private func show() throws -> PodcastShow {
        try XCTUnwrap(PodcastShow.parse(podcastFixture, baseURL: URL(string: "https://books.example.com")!))
    }

    func testShowListHasOneRowPerShow() {
        let sections = CarPlayPodcastMapper.showSections([PodcastShowRef(id: "a", title: "Alpha"), PodcastShowRef(id: "b", title: "Beta")])
        XCTAssertEqual(sections.count, 1)
        XCTAssertEqual(sections[0].items.map(\.text), ["Alpha", "Beta"])
        XCTAssertEqual(sections[0].items.map(\.id), ["a", "b"])
    }

    func testEmptyShowListIsAPlaceholder() {
        let item = CarPlayPodcastMapper.showSections([]).first?.items.first
        XCTAssertEqual(item?.text, CarPlayPodcastMapper.emptyShowsText)
        XCTAssertEqual(item?.isPlaceholder, true)
    }

    func testSeasonsAreNumberedInOrderWithExtrasLast() throws {
        let rows = try XCTUnwrap(CarPlayPodcastMapper.seasonSections(show()).first?.items)
        XCTAssertEqual(rows.map(\.text), ["Season 1", "Season 2", "Season 10", "Extras"])
        XCTAssertEqual(rows.map(\.id), ["s1", "s2", "s10", "sx"])
        XCTAssertEqual(rows.last?.detailText, "0 episodes")   // the only Extras episode is missing
        XCTAssertEqual(rows.first?.detailText, "1 episode")
    }

    func testEpisodeRowsShowDateAndPlayedAndSkipMissing() throws {
        let s = try show()
        let first = try XCTUnwrap(CarPlayPodcastMapper.episodeSections(s.seasons[0]).first?.items)
        XCTAssertEqual(first, [CarPlayRowModel(id: "e1", text: "One", detailText: "2024-01-01 · Played")])
        let extras = try XCTUnwrap(CarPlayPodcastMapper.episodeSections(s.seasons[3]).first?.items)
        XCTAssertEqual(extras.map(\.isPlaceholder), [true])
    }

    func testListsAreCapped() {
        let eps = (0..<250).map { PodcastEpisode(id: "e\($0)", title: "E\($0)") }
        let rows = CarPlayPodcastMapper.episodeSections(PodcastSeason(id: "s", number: 1, isExtras: false, episodes: eps))[0].items
        XCTAssertEqual(rows.count, CarPlayPodcastMapper.maxItems)
    }
}
