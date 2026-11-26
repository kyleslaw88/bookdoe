import BookdoeKit
import CarPlay
import UIKit

/// CarPlay scene delegate that owns the `CPInterfaceController`.
///
/// The root template is a `CPTabBarTemplate(templates:)` with three
/// `CPListTemplate(title:sections:)` tabs, Library, Continue Listening and Podcasts, each
/// with `tabTitle` and an SF Symbol `tabImage`. Tabs are identified by the stored
/// template references. After connecting, each tab's `CarPlayLoadState` is loaded
/// by `CarPlayLibraryLoader`, mapped by `CarPlayStateMapper`, converted to
/// `CPListSection`/`CPListItem` and applied with `updateSections(_:)`.
///
/// The delegate is wired up via `project.yml` and `AppDelegate`. No
/// `ObservableObject` or `@Published`: it is a plain `NSObject` subclass.
@MainActor
final class CarPlaySceneDelegate: NSObject, CPTemplateApplicationSceneDelegate, CPTabBarTemplateDelegate {

    /// The active interface controller, set in `didConnect` and cleared
    /// in `didDisconnectInterfaceController`.
    var interfaceController: CPInterfaceController?

    private(set) var libraryList: CPListTemplate?
    private(set) var continueList: CPListTemplate?
    private(set) var podcastList: CPListTemplate?
    private var loadTask: Task<Void, Never>?
    /// In-flight cover loads per list; cancelled when the list is replaced (#475).
    private var coverTasks: [ObjectIdentifier: [Task<Void, Never>]] = [:]

    // MARK: - CPTemplateApplicationSceneDelegate

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didConnect interfaceController: CPInterfaceController
    ) {
        self.interfaceController = interfaceController

        let root = CarPlayTemplates.makeRoot()
        let libraryList = root.library
        let continueList = root.continueListening
        let podcastList = root.podcasts
        self.libraryList = libraryList
        self.continueList = continueList
        self.podcastList = podcastList

        installSpeedButton()

        let tabBar = root.tabBar
        tabBar.delegate = self
        Task { @MainActor in
            // The iOS 27 SDK imports this as async throws (see #470); older SDKs ignore the extra keywords with a warning.
            try? await interfaceController.setRootTemplate(tabBar, animated: false)
            self.reload()
        }
    }

    func templateApplicationScene(
        _ templateApplicationScene: CPTemplateApplicationScene,
        didDisconnectInterfaceController interfaceController: CPInterfaceController
    ) {
        loadTask?.cancel()
        loadTask = nil
        coverTasks.values.forEach { $0.forEach { $0.cancel() } }
        coverTasks.removeAll()
        libraryList = nil
        continueList = nil
        podcastList = nil
        self.interfaceController = nil
    }

    // MARK: - Now Playing speed button

    /// Adds the system playback-rate button to the Now Playing screen. A tap moves the shared
    /// `PlayerEngine` to the next speed, the same path as the phone's speed menu and the lock screen;
    /// the engine publishes the rate in the now-playing info, which the button and the elapsed time follow.
    private func installSpeedButton() {
        let button = CPNowPlayingPlaybackRateButton { _ in
            Task { @MainActor in
                let player = AppModel.shared.player
                player.rate = PlaybackSpeed.next(after: player.rate)
            }
        }
        CPNowPlayingTemplate.shared.updateNowPlayingButtons([button])
    }

    // MARK: - CPTabBarTemplateDelegate

    /// Refreshes Continue Listening when its tab is selected, since progress moves
    /// while listening. The tab bar stays the root: this never calls `setRootTemplate`.
    func tabBarTemplate(_ tabBarTemplate: CPTabBarTemplate, didSelect selectedTemplate: CPTemplate) {
        guard let continueList, selectedTemplate === continueList else { return }
        Task {
            apply(await CarPlayLibraryLoader.continueListeningState(model: AppModel.shared), to: continueList,
                  emptyText: Self.continueEmptyText)
        }
    }

    // MARK: - Loading

    private static let continueEmptyText = "Nothing in progress"

    /// Show the loading row on both tabs, then load and apply each tab's state.
    private func reload() {
        guard let libraryList, let continueList, let podcastList else { return }
        apply(.loading, to: libraryList)
        applyPodcast([Self.loadingSection], to: podcastList)
        apply(.loading, to: continueList, emptyText: Self.continueEmptyText)
        loadTask?.cancel()
        loadTask = Task {
            let model = AppModel.shared
            // CarPlay can connect before the phone scene starts; `start()` wires
            // progress reporting. Calling it again is harmless.
            await model.start()
            let library = await CarPlayLibraryLoader.libraryState(model: model)
            guard !Task.isCancelled else { return }
            apply(library, to: libraryList)
            let continueListening = await CarPlayLibraryLoader.continueListeningState(model: model)
            guard !Task.isCancelled else { return }
            apply(continueListening, to: continueList, emptyText: Self.continueEmptyText)
            await loadPodcastShows(into: podcastList)
        }
    }

    // MARK: - Podcasts (#523): shows -> seasons -> episodes, three levels under the tab bar

    private static let loadingSection = CarPlaySectionModel(items: [CarPlayRowModel(text: CarPlayStateMapper.loadingText, isPlaceholder: true)])

    private func applyPodcast(_ sections: [CarPlaySectionModel], to list: CPListTemplate, cover: URL? = nil,
                              handler: ((String) -> Void)? = nil) {
        let (cpSections, covers) = CarPlayTemplates.sections(sections) { row, item in
            guard let id = row.id, let handler else { return }
            item.handler = { _, completion in
                Task { @MainActor in
                    handler(id)
                    completion()
                }
            }
        }
        cancelCovers(for: list)
        list.updateSections(cpSections)
        startCovers(covers, for: list)
    }

    private func loadPodcastShows(into list: CPListTemplate) async {
        guard let provider = AppModel.shared.provider as? BookdoeProvider else {
            applyPodcast([CarPlaySectionModel(items: [CarPlayRowModel(text: CarPlayLibraryLoader.signInMessage, isPlaceholder: true)])], to: list)
            return
        }
        do {
            let shows = try await provider.podcastShows()
            guard !Task.isCancelled else { return }
            applyPodcast(CarPlayPodcastMapper.showSections(shows), to: list) { [weak self] id in
                self?.openShow(id: id, title: shows.first { $0.id == id }?.title ?? "", provider: provider)
            }
        } catch {
            applyPodcast([CarPlaySectionModel(items: [CarPlayRowModel(text: CarPlayStateMapper.offlineText, isPlaceholder: true)])], to: list)
        }
    }

    private func openShow(id: String, title: String, provider: BookdoeProvider) {
        let list = CPListTemplate(title: title, sections: [])
        applyPodcast([Self.loadingSection], to: list)
        interfaceController?.pushTemplate(list, animated: true, completion: nil)
        Task { @MainActor in
            do {
                let show = try await provider.podcastShow(id: id)
                applyPodcast(CarPlayPodcastMapper.seasonSections(show), to: list) { [weak self] seasonID in
                    guard let season = show.seasons.first(where: { $0.id == seasonID }) else { return }
                    self?.openSeason(season, of: show, provider: provider)
                }
            } catch {
                applyPodcast([CarPlaySectionModel(items: [CarPlayRowModel(text: CarPlayStateMapper.offlineText, isPlaceholder: true)])], to: list)
            }
        }
    }

    private func openSeason(_ season: PodcastSeason, of show: PodcastShow, provider: BookdoeProvider) {
        let list = CPListTemplate(title: season.title, sections: [])
        applyPodcast(CarPlayPodcastMapper.episodeSections(season), to: list) { episodeID in
            guard let episode = season.episodes.first(where: { $0.id == episodeID }) else { return }
            Task { @MainActor in await CarPlayLibraryLoader.playEpisode(episode, of: show, provider: provider, model: AppModel.shared) }
        }
        interfaceController?.pushTemplate(list, animated: true, completion: nil)
    }

    private func apply(_ state: CarPlayLoadState, to list: CPListTemplate,
                       emptyText: String = CarPlayStateMapper.emptyText) {
        let (sections, covers) = CarPlayTemplates.sections(
            CarPlayStateMapper.sections(for: state, emptyText: emptyText)) { row, item in
            setHandler(on: item, for: row, offline: state == .offline)
        }
        cancelCovers(for: list)
        list.updateSections(sections)
        startCovers(covers, for: list)
    }

    private func cancelCovers(for list: CPListTemplate) {
        coverTasks.removeValue(forKey: ObjectIdentifier(list))?.forEach { $0.cancel() }
    }

    /// Load covers off the main thread and set them on the rows as they arrive.
    private func startCovers(_ covers: [(CPListItem, URL)], for list: CPListTemplate) {
        guard let provider = AppModel.shared.provider else { return }
        let maxSize = CPListItem.maximumImageSize
        coverTasks[ObjectIdentifier(list)] = covers.map { item, url in
            Task {
                guard let image = await CoverLoader.shared.image(for: url, provider: provider, maxSize: maxSize),
                      !Task.isCancelled else { return }
                item.setImage(image)
            }
        }
    }

    /// Book rows start playback; the offline placeholder retries the load.
    private func setHandler(on item: CPListItem, for row: CarPlayRowModel, offline: Bool) {
        if let id = row.id {
            item.handler = { _, completion in
                Task { @MainActor in
                    await CarPlayLibraryLoader.play(id: id, model: AppModel.shared)
                    completion()
                }
            }
        } else if offline {
            item.handler = { [weak self] _, completion in
                Task { @MainActor in
                    self?.reload()
                    completion()
                }
            }
        }
    }
}
