import CarPlay
import SwiftUI
import UIKit

/// UIKit entry point. Scenes are not owned by SwiftUI: CarPlay requires its
/// `CPTemplateApplicationScene` to get `CarPlaySceneDelegate`, so this delegate
/// hands out one configuration per scene role.
@main
final class AppDelegate: UIResponder, UIApplicationDelegate {

    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        if connectingSceneSession.role == .carTemplateApplication {
            let config = UISceneConfiguration(name: "CarPlay", sessionRole: connectingSceneSession.role)
            config.sceneClass = CPTemplateApplicationScene.self
            config.delegateClass = CarPlaySceneDelegate.self
            return config
        }
        let config = UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
        config.delegateClass = PhoneSceneDelegate.self
        return config
    }
}

/// Phone / iPad window: hosts the SwiftUI `AppRoot`.
final class PhoneSceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = UIHostingController(rootView: AppRoot())
        window.makeKeyAndVisible()
        self.window = window
    }
}
