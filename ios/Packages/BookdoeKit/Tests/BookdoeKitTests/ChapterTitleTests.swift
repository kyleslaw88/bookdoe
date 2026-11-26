import XCTest
@testable import BookdoeKit

final class ChapterTitleTests: XCTestCase {
    func testNumericTitlesBecomeChapterNumbers() {
        XCTAssertEqual(Chapter(title: "018", start: 0).displayTitle(index: 17), "Chapter 18")
        XCTAssertEqual(Chapter(title: "001", start: 0).displayTitle(index: 0), "Chapter 1")
    }
    func testEmptyTitlesUseTheirPosition() {
        XCTAssertEqual(Chapter(title: "  ", start: 0).displayTitle(index: 4), "Chapter 5")
    }
    func testRealNamesAreKept() {
        XCTAssertEqual(Chapter(title: "Prologue", start: 0).displayTitle(index: 0), "Prologue")
        XCTAssertEqual(Chapter(title: "Chapter 3: The Storm", start: 0).displayTitle(index: 2), "Chapter 3: The Storm")
    }
}
