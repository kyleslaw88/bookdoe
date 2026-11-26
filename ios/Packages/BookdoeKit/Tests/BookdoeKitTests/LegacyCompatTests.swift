import XCTest
@testable import BookdoeKit

final class LegacyCompatTests: XCTestCase {
    func testOldServerKindRawValueDecodesAsBookdoe() throws {
        let kinds = try JSONDecoder().decode([ServerKind].self, from: Data(#"["alexandria","bookdoe","jellyfin"]"#.utf8))
        XCTAssertEqual(kinds, [.bookdoe, .bookdoe, .jellyfin])
        XCTAssertThrowsError(try JSONDecoder().decode(ServerKind.self, from: Data(#""plex""#.utf8)))
    }

    func testNewServerKindEncodesAsBookdoe() throws {
        XCTAssertEqual(String(data: try JSONEncoder().encode([ServerKind.bookdoe]), encoding: .utf8), #"["bookdoe"]"#)
    }

    func testLegacyFileIsMovedToNewName() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let old = dir.appendingPathComponent("old.json"), new = dir.appendingPathComponent("new.json")
        try Data("x".utf8).write(to: old)
        LegacyFiles.migrate(from: old, to: new)
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.path))
        XCTAssertEqual(try Data(contentsOf: new), Data("x".utf8))
    }
}
