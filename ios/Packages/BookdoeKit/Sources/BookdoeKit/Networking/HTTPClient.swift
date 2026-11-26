import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum HTTPMethod: String, Sendable {
    case get = "GET"
    case post = "POST"
    case put = "PUT"
    case patch = "PATCH"
    case delete = "DELETE"
}

public struct HTTPRequest: Equatable, Sendable {
    public var method: HTTPMethod
    public var url: URL
    public var headers: [String: String]
    public var body: Data?
    public var timeout: TimeInterval
    /// False to get a redirect answer itself (status and Location) instead of following it.
    public var followRedirects: Bool

    public init(method: HTTPMethod = .get, url: URL, headers: [String: String] = [:], body: Data? = nil, timeout: TimeInterval = 30, followRedirects: Bool = true) {
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body
        self.timeout = timeout
        self.followRedirects = followRedirects
    }

    /// JSON body convenience; sets Content-Type.
    public static func json(_ method: HTTPMethod, _ url: URL, headers: [String: String] = [:], body: Any) throws -> HTTPRequest {
        var h = headers
        h["Content-Type"] = "application/json"
        let data = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        return HTTPRequest(method: method, url: url, headers: h, body: data)
    }

    public var bodyJSON: [String: Any]? {
        guard let body else { return nil }
        return (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
    }
}

public struct HTTPResponse: Equatable, Sendable {
    public var status: Int
    public var headers: [String: String]
    public var body: Data

    public init(status: Int, headers: [String: String] = [:], body: Data = Data()) {
        self.status = status
        self.headers = headers
        self.body = body
    }

    public var isSuccess: Bool { (200..<300).contains(status) }

    public func json() throws -> Any {
        try JSONSerialization.jsonObject(with: body, options: [.fragmentsAllowed])
    }

    public func jsonObject() throws -> [String: Any] {
        guard let obj = try json() as? [String: Any] else { throw APIError.decoding("expected a JSON object") }
        return obj
    }

    public func jsonArray() throws -> [Any] {
        guard let arr = try json() as? [Any] else { throw APIError.decoding("expected a JSON array") }
        return arr
    }

    public func header(_ name: String) -> String? {
        headers.first { $0.key.lowercased() == name.lowercased() }?.value
    }
}

public enum APIError: Error, Equatable, LocalizedError, Sendable {
    case unauthorized
    case forbidden(String?)
    case notFound
    case rateLimited(retryAfter: Int?)
    case server(status: Int, message: String?)
    case decoding(String)
    case network(String)
    case notSupported(String)
    case setupRequired

    public var errorDescription: String? {
        switch self {
        case .unauthorized: return "Sign in again: the server rejected the credential."
        case .forbidden(let m): return m ?? "The server refused this request."
        case .notFound: return "Not found on the server."
        case .rateLimited(let s): return "Too many requests; try again in \(s ?? 60) seconds."
        case .server(let status, let m): return m ?? "Server error (HTTP \(status))."
        case .decoding(let m): return "Unexpected answer from the server (\(m))."
        case .network(let m): return "Network problem: \(m)"
        case .notSupported(let m): return m
        case .setupRequired: return "This server has not finished its first-run setup. Open it in a browser first."
        }
    }

    /// Map a non-2xx response to an error; `detail` is the bookdoe/FastAPI shape.
    public static func from(_ response: HTTPResponse) -> APIError {
        var message: String?
        if let obj = try? response.jsonObject() {
            message = (obj["detail"] as? String) ?? (obj["message"] as? String) ?? (obj["error"] as? String)
        }
        switch response.status {
        case 401: return .unauthorized
        case 403: return .forbidden(message)
        case 404: return .notFound
        case 429: return .rateLimited(retryAfter: response.header("Retry-After").flatMap { Int($0) })
        default: return .server(status: response.status, message: message)
        }
    }
}

/// The one seam every provider talks through, so tests swap in a recorder.
public protocol HTTPClient: Sendable {
    func send(_ request: HTTPRequest) async throws -> HTTPResponse
}

/// Answers every redirect with "do not follow", so the 3xx response comes back as the result.
private final class RedirectBlocker: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

public struct URLSessionHTTPClient: HTTPClient {
    public let session: URLSession
    public let userAgent: String
    /// Same configuration (and so the same cookies) as `session`, but it never follows a redirect.
    private let plainSession: URLSession

    public init(session: URLSession = URLSessionHTTPClient.makePinnedSession(), userAgent: String = "bookdoe-iOS") {
        self.session = session
        self.userAgent = userAgent
        self.plainSession = URLSession(configuration: session.configuration, delegate: RedirectBlocker(), delegateQueue: nil)
    }

    public func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        var req = URLRequest(url: request.url, timeoutInterval: request.timeout)
        req.httpMethod = request.method.rawValue
        req.httpBody = request.body
        req.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        for (k, v) in request.headers { req.setValue(v, forHTTPHeaderField: k) }
        do {
            // dataTask + continuation rather than URLSession.data(for:), which
            // swift-corelibs-foundation (Linux CI) does not provide.
            let (data, resp): (Data, URLResponse) = try await withCheckedThrowingContinuation { cont in
                let task = (request.followRedirects ? session : plainSession).dataTask(with: req) { data, resp, error in
                    if let error { cont.resume(throwing: error) } else if let resp { cont.resume(returning: (data ?? Data(), resp)) } else { cont.resume(throwing: APIError.network("empty response")) }
                }
                task.resume()
            }
            guard let http = resp as? HTTPURLResponse else { throw APIError.network("no HTTP response") }
            var headers: [String: String] = [:]
            for (k, v) in http.allHeaderFields {
                if let ks = k as? String, let vs = v as? String { headers[ks] = vs }
            }
            return HTTPResponse(status: http.statusCode, headers: headers, body: data)
        } catch let e as APIError {
            throw e
        } catch {
            throw APIError.network(error.localizedDescription)
        }
    }
}

// MARK: - URL helpers

extension URL {
    /// Append path components and query items without double slashes.
    public func appending(path: String, query: [String: String?] = [:]) -> URL {
        var comps = URLComponents(url: self, resolvingAgainstBaseURL: false) ?? URLComponents()
        let base = comps.path.hasSuffix("/") ? String(comps.path.dropLast()) : comps.path
        let extra = path.hasPrefix("/") ? path : "/" + path
        comps.path = base + extra
        let items = query.compactMap { k, v -> URLQueryItem? in v.map { URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { comps.queryItems = (comps.queryItems ?? []) + items.sorted { $0.name < $1.name } }
        return comps.url ?? self
    }

    /// Resolve a server-relative URL ("/public/session/...") against the base.
    public func resolving(_ relative: String) -> URL? {
        if let abs = URL(string: relative), abs.scheme != nil { return abs }
        return appending(path: relative)
    }
}

// MARK: - JSON helpers

enum JSON {
    static func string(_ obj: [String: Any], _ key: String) -> String? {
        if let s = obj[key] as? String { return s }
        if let n = obj[key] as? NSNumber { return n.stringValue }
        return nil
    }

    static func double(_ obj: [String: Any], _ key: String) -> Double? {
        if let d = obj[key] as? Double { return d }
        if let n = obj[key] as? NSNumber { return n.doubleValue }
        if let s = obj[key] as? String { return Double(s) }
        return nil
    }

    static func int(_ obj: [String: Any], _ key: String) -> Int? {
        if let i = obj[key] as? Int { return i }
        if let n = obj[key] as? NSNumber { return n.intValue }
        if let s = obj[key] as? String { return Int(s) }
        return nil
    }

    static func bool(_ obj: [String: Any], _ key: String) -> Bool? {
        if let b = obj[key] as? Bool { return b }
        if let n = obj[key] as? NSNumber { return n.boolValue }
        return nil
    }

    static func object(_ obj: [String: Any], _ key: String) -> [String: Any]? { obj[key] as? [String: Any] }
    static func array(_ obj: [String: Any], _ key: String) -> [[String: Any]] { (obj[key] as? [[String: Any]]) ?? [] }

    static func date(seconds: Double?) -> Date? { seconds.map { Date(timeIntervalSince1970: $0) } }

    static func date(iso: String?) -> Date? {
        guard let iso else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: iso) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: iso)
    }
}
