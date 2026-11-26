import XCTest
@testable import BookdoeKit

#if canImport(Security)
import Security

/// Runs only where the Security framework exists (macOS / iOS CI jobs); the Linux
/// `swift test` run does not compile it.
final class KeychainMigrationTests: XCTestCase {
    private func legacyQuery(_ id: UUID) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: KeychainCredentialStore.legacyService,
         kSecAttrAccount as String: id.uuidString]
    }

    func testLegacyItemIsCopiedToNewServiceAndDeleted() throws {
        let id = UUID()
        let store = KeychainCredentialStore(service: "test.\(UUID().uuidString).credentials")
        defer {
            SecItemDelete(legacyQuery(id) as CFDictionary)
            try? store.remove(for: id)
        }
        let cred = Credential(accessToken: "legacy-tok", refreshToken: "legacy-ref")
        var add = legacyQuery(id)
        add[kSecValueData as String] = try JSONEncoder().encode(cred)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let addStatus = SecItemAdd(add as CFDictionary, nil)
        try XCTSkipIf(addStatus != errSecSuccess, "keychain unavailable in this environment (\(addStatus))")

        XCTAssertEqual(try store.credential(for: id), cred)
        // Legacy item is gone; the new service now holds it directly.
        XCTAssertEqual(SecItemCopyMatching(legacyQuery(id) as CFDictionary, nil), errSecItemNotFound)
        XCTAssertEqual(try store.credential(for: id), cred)
    }

    func testNoLegacyItemReturnsNil() throws {
        let store = KeychainCredentialStore(service: "test.\(UUID().uuidString).credentials")
        XCTAssertNil(try store.credential(for: UUID()))
    }

    func testDefaultServiceDerivesFromBundleIdentifier() {
        let base = Bundle.main.bundleIdentifier ?? "bookdoe"
        XCTAssertEqual(KeychainCredentialStore().service, "\(base).credentials")
    }
}
#endif
