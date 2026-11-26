import XCTest
@testable import BookdoeKit

final class PKCETests: XCTestCase {
    func testSHA256KnownVectors() {
        func hex(_ b: [UInt8]) -> String { b.map { String(format: "%02x", $0) }.joined() }
        XCTAssertEqual(hex(PKCE.sha256(Array("abc".utf8))), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        XCTAssertEqual(hex(PKCE.sha256([])), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        // 56 bytes: the padding falls into a second block.
        XCTAssertEqual(hex(PKCE.sha256(Array("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq".utf8))),
                       "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1")
    }

    func testChallengeMatchesTheRFC7636Example() {
        XCTAssertEqual(PKCE.challenge(for: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
    }

    func testVerifierIsLongRandomHex() {
        let a = PKCE.makeVerifier(), b = PKCE.makeVerifier()
        XCTAssertEqual(a.count, 84)
        XCTAssertNotEqual(a, b)
        XCTAssertTrue(a.allSatisfy { $0.isHexDigit })
    }
}

final class AudiobookshelfOpenIDTests: XCTestCase {
    var client: MockHTTPClient!
    var creds: InMemoryCredentialStore!
    var provider: AudiobookshelfProvider!
    let server = makeServer(.audiobookshelf, base: "https://abs.example.com")

    override func setUp() {
        client = MockHTTPClient()
        creds = InMemoryCredentialStore()
        provider = AudiobookshelfProvider(server: server, client: client, credentials: creds, appVersion: "1")
    }

    private let idp = "https://sso.example.com/application/o/authorize/?client_id=abc&scope=openid%20email%20profile&state=S123&redirect_uri=https%3A%2F%2Fabs.example.com%2Fauth%2Fopenid%2Fmobile-redirect&response_type=code&code_challenge=zzz&extra=1"

    func testProbeReportsSingleSignOn() async throws {
        client.enqueueJSON(200, ["serverVersion": "2.30.0", "isInit": true, "authMethods": ["local", "openid"], "authFormData": ["authOpenIDButtonText": "Login with Authentik"]])
        let info = try await provider.probe()
        XCTAssertTrue(info.supportsOpenID)
        XCTAssertTrue(info.supportsPasswordLogin)
        XCTAssertEqual(info.openIDButtonText, "Login with Authentik")
        client.enqueueJSON(200, ["serverVersion": "2.30.0", "isInit": true, "authMethods": ["openid"]])
        let only = try await provider.probe()
        XCTAssertTrue(only.supportsOpenID)
        XCTAssertFalse(only.supportsPasswordLogin)
    }

    func testBeginAsksTheServerWithoutFollowingAndRebuildsTheProviderURL() async throws {
        client.enqueue(HTTPResponse(status: 302, headers: ["location": idp]))
        let start = try await provider.beginOpenID(verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        let req = client.last!
        XCTAssertEqual(req.url.path, "/auth/openid")
        XCTAssertFalse(req.followRedirects)
        let q = req.url.queryDict
        XCTAssertEqual(q["code_challenge"], "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        XCTAssertEqual(q["code_challenge_method"], "S256")
        XCTAssertEqual(q["redirect_uri"], "audiobookshelf://oauth")
        XCTAssertEqual(q["client_id"], "Audiobookshelf-App")
        XCTAssertEqual(q["response_type"], "code")
        XCTAssertEqual(start.state, "S123")
        let out = start.authorizeURL.queryDict
        XCTAssertEqual(start.authorizeURL.host, "sso.example.com")
        XCTAssertTrue(start.authorizeURL.absoluteString.hasPrefix("https://sso.example.com/application/o/authorize/?"), "the trailing slash must survive: \(start.authorizeURL)")
        XCTAssertEqual(out["client_id"], "abc")
        XCTAssertEqual(out["scope"], "openid email profile")
        XCTAssertEqual(out["redirect_uri"], "https://abs.example.com/auth/openid/mobile-redirect")
        XCTAssertEqual(out["code_challenge"], "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        XCTAssertNil(out["extra"])
    }

    func testBeginRefusesAnInsecureProviderAndAMissingRedirect() async {
        client.enqueue(HTTPResponse(status: 302, headers: ["Location": idp.replacingOccurrences(of: "https://sso", with: "http://sso")]))
        do { _ = try await provider.beginOpenID(); XCTFail() } catch { if case .notSupported = error as? APIError {} else { XCTFail("\(error)") } }
        client.enqueueJSON(200, ["ok": true])
        do { _ = try await provider.beginOpenID(); XCTFail() } catch { XCTAssertNotNil(error as? APIError) }
    }

    func testCompleteExchangesTheCodeAndSavesTheSession() async throws {
        client.enqueue(HTTPResponse(status: 302, headers: ["Location": idp]))
        let start = try await provider.beginOpenID(verifier: "v")
        client.enqueueJSON(200, ["user": ["id": "u1", "username": "ada", "accessToken": "acc", "refreshToken": "ref"], "userDefaultLibraryId": "lib1"])
        let result = try await provider.completeOpenID(callback: URL(string: "audiobookshelf://oauth?code=C0DE&state=S123")!, start: start)
        let req = client.last!
        XCTAssertEqual(req.url.path, "/auth/openid/callback")
        XCTAssertEqual(req.url.queryDict["code"], "C0DE")
        XCTAssertEqual(req.url.queryDict["state"], "S123")
        XCTAssertEqual(req.url.queryDict["code_verifier"], "v")
        XCTAssertEqual(result.credential.accessToken, "acc")
        XCTAssertEqual(result.credential.refreshToken, "ref")
        XCTAssertEqual(result.server.libraryID, "lib1")
        XCTAssertEqual(result.server.username, "ada")
        XCTAssertEqual(try creds.credential(for: server.id)?.accessToken, "acc")
    }

    func testCompleteRejectsAWrongStateAnErrorAndNoCode() async throws {
        client.enqueue(HTTPResponse(status: 302, headers: ["Location": idp]))
        let start = try await provider.beginOpenID(verifier: "v")
        let before = client.requests.count
        for bad in ["audiobookshelf://oauth?code=x&state=OTHER", "audiobookshelf://oauth?error=access_denied&state=S123", "audiobookshelf://oauth?state=S123"] {
            do { _ = try await provider.completeOpenID(callback: URL(string: bad)!, start: start); XCTFail(bad) } catch { XCTAssertNotNil(error as? APIError, bad) }
        }
        XCTAssertEqual(client.requests.count, before, "nothing may be sent to the server for a bad callback")
        XCTAssertNil(try creds.credential(for: server.id))
    }
}
