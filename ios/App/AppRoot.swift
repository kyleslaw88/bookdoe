import SwiftUI
import BookdoeKit

/// Content of the phone window. Hosted by `PhoneSceneDelegate` in a `UIHostingController`.
struct AppRoot: View {
    @StateObject private var model = AppModel.shared
    @AppStorage(Appearance.storageKey) private var appearance = Appearance.system.rawValue
    @Environment(\.scenePhase) private var scenePhase

    /// Debug builds only: open a book file straight in the reader (used by the UI tests).
    @ViewBuilder private var rootContent: some View {
        #if DEBUG
        if let u = ProcessInfo.processInfo.environment["ALX_DOWNLOAD_DEMO"], let url = URL(string: u) {
            DownloadDemo(url: url)
        } else if let path = ProcessInfo.processInfo.environment["ALX_READER_DEMO"] {
            EbookReaderView(file: ReaderFile(url: URL(fileURLWithPath: path), format: path.hasSuffix(".pdf") ? "pdf" : "epub", title: "Demo",
                                             itemID: "demo", assetID: "demo", start: ProcessInfo.processInfo.environment["ALX_START"]),
                             bookmarks: BookmarksModel(book: BookRef(itemID: "demo", assetID: "demo"), source: MemoryBookmarkSource())) { _, _ in }
        } else {
            RootView()
        }
        #else
        RootView()
        #endif
    }

    var body: some View {
        rootContent
            .environmentObject(model)
            .preferredColorScheme(Appearance(rawValue: appearance)?.colorScheme)
            .task {
                #if DEBUG
                if ProcessInfo.processInfo.environment["ALX_DISCOVER_DEMO"] != nil { model.seedDiscoverDemo() }
                #endif
                await model.start()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { model.appDidBecomeActive() } else if phase == .background { model.appDidEnterBackground() }
            }
    }
}

/// User choice for light or dark; "system" follows the device setting.
enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark
    static let storageKey = "appearance"
    var id: String { rawValue }
    var label: String {
        switch self { case .system: return "System"; case .light: return "Light"; case .dark: return "Dark" }
    }
    var colorScheme: ColorScheme? {
        switch self { case .system: return nil; case .light: return .light; case .dark: return .dark }
    }
}
