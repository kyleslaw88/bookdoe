import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Security) && canImport(CryptoKit)
import Security
import CryptoKit
#endif

/// Pure pinning policy, kept free of Security so it is unit-testable on any platform.
public struct PinningPolicy: Sendable {
    public enum Decision: Equatable, Sendable {
        case defaultHandling   // host is not pinned (self-hosted servers)
        case accept            // pinned host, a chain key matched a pin
        case reject            // pinned host, nothing matched: fail closed
    }

    public let pinnedHosts: Set<String>
    /// Base64 SHA-256 of SubjectPublicKeyInfo. Any one match in the chain passes.
    public let pins: Set<String>

    public init(pinnedHosts: Set<String>, pins: Set<String>) {
        self.pinnedHosts = pinnedHosts
        self.pins = pins
    }

    /// The production certificate is served with Google Trust Services certificates that
    /// renew about every 90 days, so the pins are on the CA keys, not the leaf.
    /// Pin 1: GTS intermediate WE1. Pin 2 (backup): GTS Root R4.
    /// Rotation: ship a build with the new CA key added here before the old CA is retired.
    public static let caPins: Set<String> = [
        "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4=",
        "mEflZT5enoR1FuXLgYYGqnVEoZvmf9c2bVBpiOjYQ0c=",
    ]

    /// Pinned hosts come from the Info.plist key BookdoePinnedHosts (comma separated),
    /// which the release workflow sets from a repo secret. Empty or missing = no pinning.
    public static func fromBundle(_ bundle: Bundle = .main) -> PinningPolicy {
        let raw = bundle.object(forInfoDictionaryKey: "BookdoePinnedHosts") as? String ?? ""
        return PinningPolicy(pinnedHosts: parseHosts(raw), pins: caPins)
    }

    /// "Host1, host2" -> ["host1", "host2"]; blank entries are dropped.
    public static func parseHosts(_ raw: String) -> Set<String> {
        Set(raw.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
            .filter { !$0.isEmpty })
    }

    public func decide(host: String, chainSPKIHashes: [String]) -> Decision {
        guard pinnedHosts.contains(host.lowercased()) else { return .defaultHandling }
        return chainSPKIHashes.contains(where: { pins.contains($0) }) ? .accept : .reject
    }
}

extension URLSessionHTTPClient {
    /// A session whose delegate enforces the given policy (default: the bundle's pinned hosts).
    /// The delegate can only be supplied at session creation.
    public static func makePinnedSession(policy: PinningPolicy = .fromBundle()) -> URLSession {
        #if canImport(Security) && canImport(CryptoKit)
        return URLSession(configuration: .default, delegate: CertificatePinningDelegate(policy: policy), delegateQueue: nil)
        #else
        return URLSession(configuration: .default)
        #endif
    }
}

#if canImport(Security) && canImport(CryptoKit)
final class CertificatePinningDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    let policy: PinningPolicy

    init(policy: PinningPolicy) { self.policy = policy }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let host = challenge.protectionSpace.host
        guard policy.pinnedHosts.contains(host.lowercased()) else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        // Normal system trust evaluation must pass first; the pin is an extra check.
        var error: CFError?
        guard SecTrustEvaluateWithError(trust, &error) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let chain = (SecTrustCopyCertificateChain(trust) as? [SecCertificate]) ?? []
        let hashes = chain.flatMap(Self.spkiHashes)
        switch policy.decide(host: host, chainSPKIHashes: hashes) {
        case .accept: completionHandler(.useCredential, URLCredential(trust: trust))
        case .defaultHandling: completionHandler(.performDefaultHandling, nil)
        case .reject: completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    // ASN.1 SPKI prefixes for the EC key sizes used by the pinned chain (P-256, P-384).
    private static let p256Header: [UInt8] = [0x30,0x59,0x30,0x13,0x06,0x07,0x2a,0x86,0x48,0xce,0x3d,0x02,0x01,0x06,0x08,0x2a,0x86,0x48,0xce,0x3d,0x03,0x01,0x07,0x03,0x42,0x00]
    private static let p384Header: [UInt8] = [0x30,0x76,0x30,0x10,0x06,0x07,0x2a,0x86,0x48,0xce,0x3d,0x02,0x01,0x06,0x05,0x2b,0x81,0x04,0x00,0x22,0x03,0x62,0x00]

    static func spkiHashes(_ cert: SecCertificate) -> [String] {
        guard let key = SecCertificateCopyKey(cert),
              let raw = SecKeyCopyExternalRepresentation(key, nil) as Data? else { return [] }
        return [p256Header, p384Header].map { header in
            Data(SHA256.hash(data: Data(header) + raw)).base64EncodedString()
        }
    }
}
#endif
