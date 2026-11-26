import XCTest
@testable import BookdoeKit

final class CoverSizingTests: XCTestCase {
    func testDownscalesKeepingAspect() {
        let s = CoverSizing.fittedSize(source: (1000, 1500), max: (100, 100))
        XCTAssertEqual(s?.width, 67)
        XCTAssertEqual(s?.height, 100)
    }
    func testNeverUpscales() {
        let s = CoverSizing.fittedSize(source: (50, 60), max: (200, 200))
        XCTAssertEqual(s?.width, 50)
        XCTAssertEqual(s?.height, 60)
    }
    func testInvalidInputIsNil() {
        XCTAssertNil(CoverSizing.fittedSize(source: (0, 10), max: (100, 100)))
        XCTAssertNil(CoverSizing.fittedSize(source: (10, 10), max: (0, 100)))
    }
    func testCacheKeyIncludesBound() {
        let u = URL(string: "https://x/cover")!
        XCTAssertNotEqual(CoverSizing.cacheKey(url: u, maxWidth: 100, maxHeight: 100),
                          CoverSizing.cacheKey(url: u, maxWidth: 600, maxHeight: 600))
    }
    func testSquareArtGetsSquareFrame() {
        XCTAssertEqual(CoverSizing.heightFactor(source: (1400, 1400)), 1)
        XCTAssertEqual(CoverSizing.heightFactor(source: (3000, 2990)), 1)
    }
    func testPortraitAndUnknownArtKeepBookFrame() {
        XCTAssertEqual(CoverSizing.heightFactor(source: (1000, 1500)), 1.4)
        XCTAssertEqual(CoverSizing.heightFactor(source: (0, 0)), 1.4)
    }
}
