import Foundation
#if canImport(Security)
import Security
#endif

/// Where per-server credentials live. The app uses the Keychain; tests use memory.
public protocol CredentialStore: Sendable {
    func credential(for serverID: UUID) throws -> Credential?
    func save(_ credential: Credential, for serverID: UUID) throws
    func remove(for serverID: UUID) throws
}

public enum CredentialStoreError: Error, Equatable, LocalizedError {
    case keychain(status: Int32)
    case encoding

    public var errorDescription: String? {
        switch self {
        case .encoding: return "The sign-in could not be encoded for secure storage."
        case .keychain(let status):
            #if canImport(Security)
            let text = SecCopyErrorMessageString(status, nil) as String? ?? "unknown error"
            #else
            let text = "unknown error"
            #endif
            return "The Keychain refused to store the sign-in (\(text), code \(status))."
        }
    }
}

public final class InMemoryCredentialStore: CredentialStore, @unchecked Sendable {
    private var items: [UUID: Credential] = [:]
    private let lock = NSLock()

    public init() {}

    public func credential(for serverID: UUID) throws -> Credential? {
        lock.lock(); defer { lock.unlock() }
        return items[serverID]
    }

    public func save(_ credential: Credential, for serverID: UUID) throws {
        lock.lock(); defer { lock.unlock() }
        items[serverID] = credential
    }

    public func remove(for serverID: UUID) throws {
        lock.lock(); defer { lock.unlock() }
        items[serverID] = nil
    }
}

#if canImport(Security)
/// Generic-password Keychain items, one per server, accessible after first unlock so
/// background audio and downloads keep working while the phone is locked.
public struct KeychainCredentialStore: CredentialStore {
    public let service: String
    /// Service id that builds before F-6 wrote to. Source evidence: on origin/main before this
    /// change, `KeychainCredentialStore.init` defaulted to this literal, and `AppModel` calls
    /// `KeychainCredentialStore()` with no argument, so every shipped build used it. Not yet
    /// confirmed on a physical TestFlight device. Items found here are copied to `service` once
    /// and deleted (see `migrateLegacy`), so users should not need to sign in again.
    static let legacyService = "com.example.alexandria.credentials"

    /// The service defaults to `<Bundle.main.bundleIdentifier>.credentials`, so it always
    /// follows the bundle id CI injects (BUNDLE_ID secret) and no id is committed.
    public init(service: String? = nil) {
        let base = Bundle.main.bundleIdentifier ?? "bookdoe"
        self.service = service ?? "\(base).credentials"
    }

    private func legacyQuery(_ serverID: UUID) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: Self.legacyService,
         kSecAttrAccount as String: serverID.uuidString]
    }

    /// One-time migration: copy an item stored under the legacy service to the current
    /// service, then delete the legacy item. Returns the migrated credential, if any.
    func migrateLegacy(_ serverID: UUID) -> Credential? {
        guard service != Self.legacyService else { return nil }
        var q = legacyQuery(serverID)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data,
              let cred = try? JSONDecoder().decode(Credential.self, from: data) else { return nil }
        guard (try? save(cred, for: serverID)) != nil else { return nil }
        SecItemDelete(legacyQuery(serverID) as CFDictionary)
        return cred
    }

    private func query(_ serverID: UUID) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: serverID.uuidString]
    }

    public func credential(for serverID: UUID) throws -> Credential? {
        var q = query(serverID)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        if status == errSecItemNotFound { return migrateLegacy(serverID) }
        guard status == errSecSuccess, let data = out as? Data else { throw CredentialStoreError.keychain(status: status) }
        return try JSONDecoder().decode(Credential.self, from: data)
    }

    public func save(_ credential: Credential, for serverID: UUID) throws {
        guard let data = try? JSONEncoder().encode(credential) else { throw CredentialStoreError.encoding }
        let attrs: [String: Any] = [kSecValueData as String: data,
                                    kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query(serverID) as CFDictionary, attrs as CFDictionary)
        if status == errSecItemNotFound {
            var add = query(serverID)
            for (k, v) in attrs { add[k] = v }
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw CredentialStoreError.keychain(status: status) }
    }

    public func remove(for serverID: UUID) throws {
        let status = SecItemDelete(query(serverID) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw CredentialStoreError.keychain(status: status) }
    }
}

#if targetEnvironment(simulator)
/// Simulator-only wrapper for UI test builds that are not code signed: the Keychain answers
/// errSecMissingEntitlement (-34018) there, so saves fall back to memory. This type does not
/// exist in device builds, so devices never get weaker storage.
public final class SimulatorFallbackCredentialStore: CredentialStore, @unchecked Sendable {
    private let keychain: CredentialStore
    private let memory = InMemoryCredentialStore()
    private static let missingEntitlement: Int32 = -34018

    public init(keychain: CredentialStore) { self.keychain = keychain }

    public func credential(for serverID: UUID) throws -> Credential? {
        if let c = try? memory.credential(for: serverID) { return c }
        do { return try keychain.credential(for: serverID) }
        catch CredentialStoreError.keychain(status: Self.missingEntitlement) { return nil }
    }

    public func save(_ credential: Credential, for serverID: UUID) throws {
        do { try keychain.save(credential, for: serverID) }
        catch CredentialStoreError.keychain(status: Self.missingEntitlement) { try memory.save(credential, for: serverID) }
    }

    public func remove(for serverID: UUID) throws {
        try? memory.remove(for: serverID)
        do { try keychain.remove(for: serverID) }
        catch CredentialStoreError.keychain(status: Self.missingEntitlement) {}
    }
}
#endif
#endif
