import BookdoeKit
import CarPlay
import XCTest
@testable import bookdoe

/// CarPlay template construction without a head unit: CPListTemplate / CPListItem
/// can be created and inspected in a plain test host.
@MainActor
final class CarPlayTemplatesTests: XCTestCase {

    private func items(_ sections: [CPListSection]) -> [CPListItem] {
        sections.flatMap { $0.items.compactMap { $0 as? CPListItem } }
    }

    private func books(_ n: Int, cover: URL? = nil) -> [CarPlayBookRow] {
        (0..<n).map { CarPlayBookRow(id: "\($0)", title: "Book \($0)", subtitle: "Author", coverURL: cover) }
    }

    func testListLimit() {
        XCTAssertEqual(CarPlayLibraryLoader.listLimit, 20)
    }

    func testRootHasThreeTabsInOrder() {
        let root = CarPlayTemplates.makeRoot()
        let tabs = root.tabBar.templates
        XCTAssertEqual(tabs.count, 3)
        XCTAssertTrue(tabs[0] === root.library)
        XCTAssertTrue(tabs[1] === root.continueListening)
        XCTAssertTrue(tabs[2] === root.podcasts)
        XCTAssertEqual([root.library.title, root.continueListening.title, root.podcasts.title],
                       ["Library", "Continue Listening", "Podcasts"])
        XCTAssertEqual(root.library.tabTitle, "Library")
        for list in [root.library, root.continueListening, root.podcasts] {
            XCTAssertNotNil(list.tabImage)
            XCTAssertTrue(items(list.sections).isEmpty)
        }
    }

    func testReadyRowsWithAndWithoutCovers() {
        let url = URL(string: "https://example.invalid/c.jpg")!
        let rows = [CarPlayBookRow(id: "a", title: "A", subtitle: "X · 43%", coverURL: url),
                    CarPlayBookRow(id: "b", title: "B")]
        let built = CarPlayTemplates.sections(CarPlayStateMapper.sections(for: .ready(items: rows)))
        let list = items(built.sections)
        XCTAssertEqual(list.map(\.text), ["A", "B"])
        XCTAssertEqual(list[0].detailText, "X · 43%")
        XCTAssertNil(list[1].detailText)
        XCTAssertNotNil(list[0].image, "a row with a cover shows the placeholder until it loads")
        XCTAssertNil(list[1].image)
        XCTAssertEqual(built.covers.count, 1)
        XCTAssertTrue(built.covers[0].0 === list[0])
        XCTAssertEqual(built.covers[0].1, url)
    }

    func testListAtTheLimitKeepsEveryRow() {
        let built = CarPlayTemplates.sections(CarPlayStateMapper.sections(for: .ready(items: books(CarPlayLibraryLoader.listLimit))))
        XCTAssertEqual(items(built.sections).count, 20)
    }

    func testPlaceholderRowsWithoutAHeadUnit() {
        let cases: [(CarPlayLoadState, String)] = [
            (.loading, CarPlayStateMapper.loadingText),
            (.empty, CarPlayStateMapper.emptyText),
            (.offline, CarPlayStateMapper.offlineText),
            (.error(message: "boom"), "boom"),
        ]
        for (state, text) in cases {
            let built = CarPlayTemplates.sections(CarPlayStateMapper.sections(for: state))
            XCTAssertEqual(items(built.sections).map(\.text), [text])
            XCTAssertNil(items(built.sections).first?.image)
            XCTAssertTrue(built.covers.isEmpty)
        }
    }

    func testUpdateSectionsAcceptsBuiltSections() {
        let root = CarPlayTemplates.makeRoot()
        let ready = CarPlayTemplates.sections(CarPlayStateMapper.sections(for: .ready(items: books(3))))
        root.library.updateSections(ready.sections)
        XCTAssertEqual(items(root.library.sections).map(\.text), ["Book 0", "Book 1", "Book 2"])
        let offline = CarPlayTemplates.sections(CarPlayStateMapper.sections(for: .offline))
        root.library.updateSections(offline.sections)
        XCTAssertEqual(items(root.library.sections).map(\.text), [CarPlayStateMapper.offlineText])
    }

    func testConfigureRunsForEachRow() {
        var seen: [String?] = []
        _ = CarPlayTemplates.sections(CarPlayStateMapper.sections(for: .ready(items: books(2)))) { row, _ in seen.append(row.id) }
        XCTAssertEqual(seen, ["0", "1"])
    }
}
