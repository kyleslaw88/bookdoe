import Foundation
import SwiftUI
import BookdoeKit

/// App-wide state: saved servers, the active provider, the player, downloads and the
/// progress queue. Views read it through the environment.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var servers: [ServerRecord] = []
    @Published private(set) var activeServer: ServerRecord?
    @Published var lastError: String?
    /// Bumped when a book's read state changes so Home reloads its Continue list.
    @Published private(set) var progressRevision = 0
    /// True when queued progress could not be delivered because the server rejected our sign-in.
    @Published private(set) var needsReauth = false
    /// The active server's `discover_chat` capability. False until it says true, so the Discover
    /// entry never shows for another server type, an older server, or one that cannot be reached.
    @Published private(set) var discoverChatAvailable = false
    /// True while a text field that needs the keyboard (Discover) is focused; the mini player steps aside.
    @Published var hidesMiniPlayer = false
    /// True while Discover is on screen: it draws the mini player itself, as a row under its message field, because
    /// the tab-level inset does not shrink Discover's content on iPhone and covered the field (#725).
    @Published var discoverShowsMiniPlayer = false
    /// The mini player's full-screen sheet; here so Discover's own mini player row can open it too.
    @Published var showPlayer = false

    let serverStore: ServerStore
    let credentials: CredentialStore
    let factory: ProviderFactory
    let progressQueue: ProgressQueue
    let downloads: DownloadManager
    let player: PlayerEngine

    private(set) var provider: MediaProvider?
    private var discoverServerID: UUID?
    #if DEBUG
    private var discoverDemo = false
    #endif

    static let appVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "dev"
    static let buildNumber = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "0"
    static let gitSHA = Bundle.main.infoDictionary?["GitSHA"] as? String ?? "unknown"

    init(serverStore: ServerStore, credentials: CredentialStore, client: HTTPClient, progressQueue: ProgressQueue, downloads: DownloadManager) {
        self.serverStore = serverStore
        self.credentials = credentials
        self.factory = ProviderFactory(client: client, credentials: credentials, appVersion: Self.appVersion)
        self.progressQueue = progressQueue
        self.downloads = downloads
        self.player = PlayerEngine(progressQueue: progressQueue)
        self.player.offlineDetail = { [downloads] sid, id in downloads.offlineDetail(serverID: sid, itemID: id) }
        self.player.localPositions = LocalPositionStore(fileURL: LocalPositionStore.defaultFileURL())
        refreshServers()
    }

    /// The one app-wide model. The phone WindowGroup and the CarPlay scene share it.
    static let shared: AppModel = AppModel.live()

    static func live() -> AppModel {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? FileManager.default.temporaryDirectory
        let downloadsRoot = support.appendingPathComponent("downloads", isDirectory: true)
        #if targetEnvironment(simulator)
        let credentials: CredentialStore = SimulatorFallbackCredentialStore(keychain: KeychainCredentialStore())
        #else
        let credentials: CredentialStore = KeychainCredentialStore()
        #endif
        return AppModel(serverStore: ServerStore(fileURL: ServerStore.defaultFileURL()),
                        credentials: credentials,
                        client: URLSessionHTTPClient(userAgent: "bookdoe-iOS/\(appVersion)"),
                        progressQueue: ProgressQueue(fileURL: ProgressQueue.defaultFileURL()),
                        downloads: DownloadManager(root: downloadsRoot, index: DownloadIndex(fileURL: support.appendingPathComponent("downloads.json"))))
    }

    func start() async {
        player.onProgress = { [weak self] update, session in
            guard let self else { return }
            Task { await self.progressQueue.enqueue(update); await self.flushProgress(session: session) }
        }
        await flushProgress(session: nil)
        await refreshDiscoverAvailability()
    }

    // MARK: servers

    func refreshServers() {
        servers = serverStore.servers
        activeServer = serverStore.active
        provider = activeServer.map(factory.make)
        if activeServer?.id != discoverServerID {
            // A different server: drop the old answer at once, then ask the new one.
            discoverChatAvailable = false
            discoverServerID = activeServer?.id
        }
        Task { await refreshDiscoverAvailability() }
    }

    /// Re-read the active server's capabilities (app launch, server switch).
    func refreshDiscoverAvailability() async {
        let asked = activeServer?.id
        #if DEBUG
        if discoverDemo { return }
        #endif
        let offered = await DiscoverAvailability.resolve(provider: provider)
        guard asked == activeServer?.id else { return }   // the server changed while we asked
        discoverChatAvailable = offered
    }

    func setActive(_ server: ServerRecord) {
        serverStore.setActive(id: server.id)
        refreshServers()
    }

    /// Probe, sign in and save a server. Returns the saved record.
    func addServer(kind: ServerKind, name: String, url: URL, username: String, password: String) async throws -> ServerRecord {
        var record = ServerRecord(name: name, kind: kind, baseURL: url, username: username)
        let p = factory.make(record)
        let result = try await p.login(username: username, password: password, deviceName: Self.deviceName)
        try credentials.save(result.credential, for: record.id)
        record = result.server
        serverStore.add(record)
        serverStore.setActive(id: record.id)
        refreshServers()
        Task { await flushProgress(session: nil) }
        return record
    }

    /// bookdoe pairing: start, then poll until the person approves.
    func startPairing(name: String, url: URL) async throws -> (ServerRecord, BookdoeProvider, DevicePairing) {
        let record = ServerRecord(name: name, kind: .bookdoe, baseURL: url, username: "")
        guard let p = factory.make(record) as? BookdoeProvider else { throw APIError.notSupported("pairing is bookdoe only") }
        return (record, p, try await p.startPairing(deviceName: Self.deviceName))
    }

    func finishPairing(record: ServerRecord, result: AuthResult) throws {
        try credentials.save(result.credential, for: record.id)
        serverStore.add(result.server)
        serverStore.setActive(id: record.id)
        refreshServers()
        Task { await flushProgress(session: nil) }
    }

    /// Save a server that was signed in outside `addServer` (single sign-on). The provider has
    /// already stored the credential; this records the server and makes it the active one.
    func addSignedIn(server record: ServerRecord) {
        serverStore.add(record)
        serverStore.setActive(id: record.id)
        refreshServers()
    }

    func removeServer(_ server: ServerRecord) async {
        if let p = provider, p.server.id == server.id { try? await p.logout() } else { try? await factory.make(server).logout() }
        try? credentials.remove(for: server.id)
        await progressQueue.clear(serverID: server.id)
        downloads.removeAll(serverID: server.id)
        serverStore.remove(id: server.id)
        refreshServers()
    }

    func updateServer(_ server: ServerRecord) {
        serverStore.update(server)
        refreshServers()
    }

    // MARK: progress

    /// Mark a book read or unread on the server. A finished book drops out of Continue and
    /// an unread one forgets its position. Stops playback if it is the book now playing and
    /// clears anything queued for it, so an old position cannot come back.
    func setFinished(_ finished: Bool, assetID: String, detail: MediaDetail?) async {
        guard let provider, let activeServer else { return }
        do {
            if player.current?.assetID == assetID { await player.stop(report: false) }
            await progressQueue.remove(serverID: activeServer.id, assetID: assetID)
            try await provider.setFinished(finished, assetID: assetID, detail: detail)
            progressRevision += 1
        } catch { report(error) }
    }

    func flushProgress(session: PlaybackSession?) async {
        guard let provider, let activeServer else { return }
        let result = await progressQueue.flush(serverID: activeServer.id, provider: provider, session: session)
        needsReauth = result.authRequired
        for (update, serverPos) in result.conflicts where update.assetID == player.current?.assetID {
            player.adopt(serverPosition: serverPos)
        }
    }

    /// App returned to the foreground: reactivate audio, recover a dead player, push queued progress.
    func appDidBecomeActive() {
        player.appDidBecomeActive()
        Task { await flushProgress(session: nil) }
    }

    /// App went to the background: keep the local position and try to push it before suspension.
    func appDidEnterBackground() {
        player.persistLocalPosition()
        Task { await flushProgress(session: nil) }
    }

    static var deviceName: String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "bookdoe app"
        #endif
    }

    func report(_ error: Error) {
        lastError = (error as? APIError)?.errorDescription ?? error.localizedDescription
    }
}

#if canImport(UIKit)
import UIKit
#endif

#if DEBUG
/// Debug builds only (ALX_DISCOVER_DEMO=1): a signed-in bookdoe server with Discover on, an empty chat and
/// something loaded in the mini player, all without a network, so the UI tests can check Discover's layout.
extension AppModel {
    private struct EmptyAnswers: HTTPClient {
        func send(_ request: HTTPRequest) async throws -> HTTPResponse {
            HTTPResponse(status: 200, body: Data(#"{"items":[],"messages":[]}"#.utf8))
        }
    }

    func seedDiscoverDemo() {
        let server = ServerRecord(name: "Demo", kind: .bookdoe, baseURL: URL(string: "https://demo.invalid")!, username: "demo")
        let creds = InMemoryCredentialStore()
        try? creds.save(Credential(accessToken: "demo"), for: server.id)
        discoverDemo = true
        activeServer = server
        provider = BookdoeProvider(server: server, client: EmptyAnswers(), credentials: creds, appVersion: "demo")
        discoverChatAvailable = true
        player.showDemo(MediaDetail(item: MediaItem(id: "demo-book", title: "Hiding the Past", kind: .audiobook), assetID: "demo"))
    }
}
#endif
