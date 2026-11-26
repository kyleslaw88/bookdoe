import XCTest
@testable import BookdoeKit

final class CertificatePinningTests: XCTestCase {
    // Test host only. The production host is never in source; it is supplied at build time
    // through the BookdoePinnedHosts Info.plist key.
    private let policy = PinningPolicy(
        pinnedHosts: ["example.com"],
        pins: [
            "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4=",
            "mEflZT5enoR1FuXLgYYGqnVEoZvmf9c2bVBpiOjYQ0c=",
        ]
    )
    private let good = "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4="
    private let backup = "mEflZT5enoR1FuXLgYYGqnVEoZvmf9c2bVBpiOjYQ0c="

    func testPinnedHostWithMatchingPinAccepted() {
        XCTAssertEqual(policy.decide(host: "example.com", chainSPKIHashes: ["x", good]), .accept)
    }

    func testPinnedHostWithBackupPinAccepted() {
        XCTAssertEqual(policy.decide(host: "example.com", chainSPKIHashes: [backup]), .accept)
    }

    func testPinnedHostWithMismatchedPinRejected() {
        XCTAssertEqual(policy.decide(host: "example.com", chainSPKIHashes: ["nope"]), .reject)
        XCTAssertEqual(policy.decide(host: "example.com", chainSPKIHashes: []), .reject)
    }

    func testSelfHostedHostUsesDefaultHandling() {
        XCTAssertEqual(policy.decide(host: "books.example.org", chainSPKIHashes: ["nope"]), .defaultHandling)
    }

    func testHostMatchIsCaseInsensitive() {
        XCTAssertEqual(policy.decide(host: "Example.COM", chainSPKIHashes: ["nope"]), .reject)
    }

    func testEmptyConfigPinsNothing() {
        let unset = PinningPolicy(pinnedHosts: PinningPolicy.parseHosts(""), pins: policy.pins)
        XCTAssertEqual(unset.decide(host: "example.com", chainSPKIHashes: ["nope"]), .defaultHandling)
    }

    func testParseHostsIsCommaSeparatedAndNormalised() {
        XCTAssertEqual(PinningPolicy.parseHosts(" Example.com, ,api.example.com "), ["example.com", "api.example.com"])
    }

    func testFromBundleWithoutKeyPinsNothing() {
        XCTAssertTrue(PinningPolicy.fromBundle().pinnedHosts.isEmpty)
    }
}
