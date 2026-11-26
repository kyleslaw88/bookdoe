import XCTest
@testable import BookdoeKit

final class DownloadExtensionTests: XCTestCase {
    func testFilenameWins() {
        XCTAssertEqual(DownloadPlan.resolvedExtension(suggestedFilename: "Dead World.m4b", mimeType: "application/octet-stream", planned: "bin"), "m4b")
    }

    func testMimeWhenNoFilename() {
        XCTAssertEqual(DownloadPlan.resolvedExtension(suggestedFilename: nil, mimeType: "audio/mpeg", planned: "bin"), "mp3")
        XCTAssertEqual(DownloadPlan.resolvedExtension(suggestedFilename: "files", mimeType: "audio/mp4; charset=binary", planned: "bin"), "m4a")
    }

    func testBinFilenameIsNotTrusted() {
        XCTAssertEqual(DownloadPlan.resolvedExtension(suggestedFilename: "x.bin", mimeType: "audio/flac", planned: "bin"), "flac")
    }

    func testFallsBackToPlanned() {
        XCTAssertEqual(DownloadPlan.resolvedExtension(suggestedFilename: nil, mimeType: nil, planned: "mp3"), "mp3")
    }
}
