import Foundation
@testable import BookdoeKit

/// Records every request and answers from a queue or a routing closure.
final class MockHTTPClient: HTTPClient, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var requests: [HTTPRequest] = []
    private var queue: [HTTPResponse] = []
    var router: ((HTTPRequest) -> HTTPResponse?)?

    init(router: ((HTTPRequest) -> HTTPResponse?)? = nil) {
        self.router = router
    }

    func enqueue(_ response: HTTPResponse) {
        lock.lock(); defer { lock.unlock() }
        queue.append(response)
    }

    func enqueueJSON(_ status: Int = 200, _ obj: Any) {
        enqueue(.json(status, obj))
    }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        lock.lock(); defer { lock.unlock() }
        requests.append(request)
        if let r = router?(request) { return r }
        guard !queue.isEmpty else { throw APIError.network("no queued response for \(request.method.rawValue) \(request.url.path)") }
        return queue.removeFirst()
    }

    var last: HTTPRequest? { requests.last }
    func request(matching path: String) -> HTTPRequest? { requests.first { $0.url.path == path } }
}

extension HTTPResponse {
    static func json(_ status: Int = 200, _ obj: Any) -> HTTPResponse {
        HTTPResponse(status: status, headers: ["Content-Type": "application/json"], body: try! JSONSerialization.data(withJSONObject: obj, options: [.fragmentsAllowed]))
    }
}

extension URL {
    var queryDict: [String: String] {
        var out: [String: String] = [:]
        for item in URLComponents(url: self, resolvingAgainstBaseURL: false)?.queryItems ?? [] { out[item.name] = item.value ?? "" }
        return out
    }
}

func makeServer(_ kind: ServerKind, base: String = "https://books.example.com", libraryID: String? = nil, userID: String? = nil) -> ServerRecord {
    ServerRecord(name: "Test", kind: kind, baseURL: URL(string: base)!, username: "ada", userID: userID, libraryID: libraryID, deviceID: "dev-1")
}
