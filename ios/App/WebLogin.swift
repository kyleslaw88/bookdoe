import AuthenticationServices
import UIKit

/// Runs a sign-in page in the system's web authentication sheet and returns the app link the
/// page ends on (used for single sign-on, where the identity provider owns the login screen).
@MainActor
final class WebLogin: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?

    func run(url: URL, callbackScheme: String) async throws -> URL {
        try await withCheckedThrowingContinuation { cont in
            let session = ASWebAuthenticationSession(url: url, callbackURLScheme: callbackScheme) { callback, error in
                if let callback {
                    cont.resume(returning: callback)
                } else if let e = error as? ASWebAuthenticationSessionError, e.code == .canceledLogin {
                    cont.resume(throwing: CancellationError())
                } else {
                    cont.resume(throwing: error ?? CancellationError())
                }
            }
            session.presentationContextProvider = self
            // Not ephemeral: the identity provider's own session in Safari is reused, so a sign-in
            // there (password manager, passkey, "stay signed in") carries over.
            session.prefersEphemeralWebBrowserSession = false
            self.session = session
            if !session.start() { cont.resume(throwing: CancellationError()) }
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let windows = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        return windows.first(where: \.isKeyWindow) ?? windows.first ?? ASPresentationAnchor()
    }
}
