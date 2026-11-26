import Foundation
import AVFoundation
import MediaPlayer
import BookdoeKit

/// AVQueuePlayer-backed audiobook playback with lock-screen controls, background
/// audio and periodic progress reports through the ProgressQueue.
@MainActor
final class PlayerEngine: ObservableObject {
    @Published private(set) var current: MediaDetail?
    @Published private(set) var isPlaying = false
    @Published private(set) var trackIndex = 0
    @Published private(set) var positionInTrack: Double = 0
    /// The last speed chosen, kept across books and launches.
    @Published var rate: Float = {
        let saved = UserDefaults.standard.float(forKey: PlayerEngine.rateKey)
        return saved >= 0.5 && saved <= 3 ? saved : 1.0
    }() {
        didSet {
            UserDefaults.standard.set(rate, forKey: Self.rateKey)
            if isPlaying { player.rate = rate }
            updateNowPlaying()
        }
    }
    /// Why playback is not happening, in words: set when automatic recovery did not work (the UI
    /// shows it with a Retry), a track failed, or audio is busy.
    @Published private(set) var playbackError: String?
    static let rateKey = "player.rate"

    @Published var sleepTimerEnds: Date?

    var onProgress: ((ProgressUpdate, PlaybackSession?) -> Void)?

    private var player = AVQueuePlayer()
    var localPositions: LocalPositionStore?
    private let progressQueue: ProgressQueue
    private var timeObserver: Any?
    private var provider: MediaProvider?
    private var serverID: UUID?
    private var session: PlaybackSession?
    private var lastReportAt = Date.distantPast
    private var lastReportedPosition: Double = 0
    private var endObserver: NSObjectProtocol?
    private var itemsByIndex: [Int: AVPlayerItem] = [:]
    private var listenedSinceReport: Double = 0
    private var headers: [String: String] = [:]
    private var statusObservers: [NSKeyValueObservation] = []
    private var tracker = PositionTracker()
    private var recoveryAttempts = 0
    private var advanceTask: Task<Void, Never>?
    /// The downloaded copy of an episode for the active server, set by AppModel (the engine does not know the DownloadManager).
    var offlineDetail: ((UUID, String) -> MediaDetail?)?

    init(progressQueue: ProgressQueue) {
        self.progressQueue = progressQueue
        configureSession()
        configureRemoteCommands()
        attachTimeObserver()
        NotificationCenter.default.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.recover(reason: "Audio services were reset") }
        }
        NotificationCenter.default.addObserver(forName: .AVPlayerItemFailedToPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            let err = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
            Task { @MainActor in self?.itemFailed(note.object as? AVPlayerItem, error: err) }
        }
        endObserver = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            Task { @MainActor in self?.itemEnded(note.object as? AVPlayerItem) }
        }
    }

    var absolutePosition: Double {
        guard let d = current, let t = d.tracks.first(where: { $0.index == trackIndex }) else { return positionInTrack }
        return t.startOffset + positionInTrack
    }

    var duration: Double { current?.totalDuration ?? 0 }

    var currentChapter: Chapter? {
        guard let d = current else { return nil }
        let pos = absolutePosition
        return d.effectiveChapters.last { $0.start <= pos }
    }

    var currentChapterIndex: Int? {
        guard let d = current else { return nil }
        let pos = absolutePosition
        return d.effectiveChapters.lastIndex { $0.start <= pos }
    }

    /// The current chapter's name for display ("Chapter 18" for a bare "018").
    var currentChapterTitle: String? {
        guard let d = current, let i = currentChapterIndex else { return nil }
        return d.effectiveChapters[i].displayTitle(index: i)
    }

    // MARK: load and control

    #if DEBUG
    /// Shows `detail` in the mini player without loading any audio (ALX_DISCOVER_DEMO).
    func showDemo(_ detail: MediaDetail) { current = detail }
    #endif

    func load(_ detail: MediaDetail, provider: MediaProvider, serverID: UUID, autoplay: Bool = true) async {
        await stop(report: true)
        current = detail
        self.provider = provider
        self.serverID = serverID
        var detail = detail
        // bookdoe needs no session; Jellyfin and Audiobookshelf get one so their
        // progress reports are attributed. A failure here must not block playback.
        session = try? await provider.startPlayback(detail)
        headers = (try? provider.mediaHeaders()) ?? [:]
        playbackError = nil
        recoveryAttempts = 0
        buildItems(for: detail)
        // Resume from this device's copy when it is newer than the server's (reports can be
        // stuck in the queue after a failed sign-in or a crash).
        let local = localPositions?.position(serverID: serverID, assetID: detail.assetID)
        let start = LocalPosition.resolve(server: detail.position, local: local) ?? PlaybackPosition()
        detail.position = start
        current = detail
        seek(toTrack: start.trackIndex, offset: start.positionInTrack)
        updateNowPlaying()
        loadArtwork(for: detail, provider: provider)
        if autoplay { play() }
    }

    /// Fetch the cover off the main thread and publish it as Now Playing artwork
    /// (CarPlay Now Playing and the lock screen). Ignored if the item changed meanwhile.
    private func loadArtwork(for detail: MediaDetail, provider: MediaProvider) {
        artworkTask?.cancel()
        guard let url = detail.item.coverURL else { return }
        let id = detail.item.id
        artworkTask = Task { [weak self] in
            let bound = CGSize(width: 600, height: 600)
            guard let image = await CoverLoader.shared.image(for: url, provider: provider, maxSize: bound),
                  !Task.isCancelled, let self, self.current?.item.id == id, let cg = image.cgImage else { return }
            self.setArtwork(cg, size: image.size)
        }
    }

    private var artworkTask: Task<Void, Never>?

    private func buildItems(for detail: MediaDetail) {
        statusObservers = []
        itemsByIndex = [:]
        for t in detail.tracks { itemsByIndex[t.index] = makeItem(t) }
    }

    private func attachTimeObserver() {
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 1, preferredTimescale: 10), queue: .main) { [weak self] time in
            Task { @MainActor in self?.tick(time) }
        }
    }

    // MARK: failure and recovery

    private func itemStatusChanged(_ item: AVPlayerItem) {
        guard itemsByIndex[trackIndex] === item else { return }
        switch item.status {
        case .readyToPlay:
            recoveryAttempts = 0
            if playbackError != nil { playbackError = nil }
            learnDuration(from: item)
        case .failed: itemFailed(item, error: item.error)
        default: break
        }
    }

    /// When the server gave no duration for this track, take it from the loaded item so the total,
    /// progress bar and lock-screen duration are real.
    private func learnDuration(from item: AVPlayerItem) {
        let index = trackIndex
        guard let d = current, let t = d.tracks.first(where: { $0.index == index }), (t.durationSeconds ?? 0) <= 0 else { return }
        Task { @MainActor [weak self] in
            var seconds = item.duration.seconds
            if !(seconds.isFinite && seconds > 0), let loaded = try? await item.asset.load(.duration) { seconds = loaded.seconds }
            guard let self, seconds.isFinite, seconds > 0, let cur = self.current, cur.assetID == d.assetID else { return }
            let updated = cur.learningDuration(seconds, ofTrack: index)
            guard updated != cur else { return }
            self.current = updated
            self.updateNowPlaying()
        }
    }

    private func itemFailed(_ item: AVPlayerItem?, error: Error?) {
        guard current != nil, let item, itemsByIndex[trackIndex] === item else { return }
        recover(reason: error?.localizedDescription ?? "Playback failed")
    }

    /// Rebuild the player and items and resume at the last known good position. The first
    /// attempt is automatic; if it fails again the error is shown with a Retry.
    private func recover(reason: String) {
        guard let d = current else { return }
        let wasPlaying = isPlaying
        if recoveryAttempts >= 1 {
            player.pause()
            isPlaying = false
            playbackError = reason
            updateNowPlaying()
            return
        }
        recoveryAttempts += 1
        let resumeAt = tracker.recoveryPosition
        if let timeObserver { player.removeTimeObserver(timeObserver) }
        timeObserver = nil
        player.pause()
        player.removeAllItems()
        player = AVQueuePlayer()
        attachTimeObserver()
        try? configureAudioSession()
        buildItems(for: d)
        playbackError = nil
        isPlaying = false
        seek(toAbsolute: resumeAt)
        if wasPlaying { play() }
    }

    /// Called from the Retry button.
    func retry() {
        recoveryAttempts = 0
        let wasError = playbackError != nil
        recover(reason: playbackError ?? "Playback failed")
        if wasError { play() }
    }

    /// Call when the app returns to the foreground: reactivate the audio session and
    /// rebuild if the player died while suspended.
    func appDidBecomeActive() {
        guard current != nil else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        if player.currentItem == nil || player.currentItem?.status == .failed || player.error != nil {
            recoveryAttempts = 0
            recover(reason: player.error?.localizedDescription ?? "Playback stopped while the app was in the background")
        }
    }

    /// Save the current position on this device (called on a timer-driven report and on background).
    func persistLocalPosition() {
        guard let d = current, let serverID else { return }
        let track = d.tracks.first { $0.index == trackIndex }
        let total = d.totalDuration
        let pos = PlaybackPosition(trackID: track?.id, trackIndex: trackIndex, positionInTrack: positionInTrack, absolutePosition: absolutePosition,
                                   percent: total > 0 ? min(1, absolutePosition / total) : nil)
        localPositions?.save(LocalPosition(serverID: serverID, assetID: d.assetID, position: pos))
    }

    private func makeItem(_ t: Track) -> AVPlayerItem {
        let asset: AVURLAsset
        if t.url.isFileURL {
            asset = AVURLAsset(url: t.url)
        } else {
            asset = AVURLAsset(url: t.url, options: ["AVURLAssetHTTPHeaderFieldsKey": headers])
        }
        let item = AVPlayerItem(asset: asset)
        statusObservers.append(item.observe(\.status, options: [.new]) { [weak self] item, _ in
            Task { @MainActor in self?.itemStatusChanged(item) }
        })
        return item
    }

    func play() {
        if playbackError != nil { retry(); return }
        do {
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            playbackError = "Audio is busy (a call or another app is using it). Try again when it ends."
        }
        if current != nil, player.currentItem?.status == .failed, recoveryAttempts == 0 { recover(reason: "Playback failed") }
        player.play()
        player.rate = rate
        isPlaying = true
        updateNowPlaying()
    }

    func pause() {
        player.pause()
        isPlaying = false
        updateNowPlaying()
        report(force: true)
    }

    func togglePlayPause() { isPlaying ? pause() : play() }

    func skip(_ seconds: Double) {
        guard let d = current else { return }
        seek(toAbsolute: d.skipTarget(from: absolutePosition, by: seconds))
    }

    func seek(toAbsolute position: Double) {
        guard let d = current else { return }
        let total = d.totalDuration
        let loc = d.locate(absolute: total > 0 ? min(position, max(0, total - 1)) : max(0, position))
        seek(toTrack: loc.index, offset: loc.offset)
    }

    func seek(toTrack index: Int, offset: Double) {
        guard let d = current, let track = d.tracks.first(where: { $0.index == index }) else { return }
        let wasPlaying = isPlaying
        player.pause()
        player.removeAllItems()
        for t in d.tracks where t.index >= index {
            if itemsByIndex[t.index]?.status == .failed { itemsByIndex[t.index] = makeItem(t) }
            if let item = itemsByIndex[t.index] {
                if t.index != index { item.seek(to: .zero, completionHandler: nil) }  // a track played earlier would resume at its end
                if player.canInsert(item, after: nil) { player.insert(item, after: nil) }
            }
        }
        trackIndex = track.index
        positionInTrack = offset
        let token = tracker.beginSeek(to: track.startOffset + offset)
        let startOffset = track.startOffset
        player.seek(to: CMTime(seconds: offset, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            Task { @MainActor in self?.seekLanded(token: token, finished: finished, trackIndex: index, startOffset: startOffset) }
        }
        if wasPlaying { play() }
        updateNowPlaying()
        report(force: true)
    }

    func seek(toChapter chapter: Chapter) { seek(toAbsolute: chapter.start) }

    func stop(report shouldReport: Bool) async {
        guard current != nil else { return }
        if shouldReport, let update = makeUpdate(finished: false) {
            onProgress?(update, session)
            if let provider { try? await provider.stopPlayback(update, session: session) }
        }
        player.pause()
        player.removeAllItems()
        isPlaying = false
        current = nil
        MPRemoteCommandCenter.shared().nextTrackCommand.isEnabled = false
        session = nil
        itemsByIndex = [:]
        statusObservers = []
        playbackError = nil
        artworkTask?.cancel()
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        MPNowPlayingInfoCenter.default().playbackState = .stopped
    }

    /// Dismiss the player (mini player close): save the position on this device and report it, stop, and clear
    /// lock-screen and CarPlay Now Playing. The bar comes back when something is played again.
    func close() async {
        guard current != nil else { return }
        player.pause()
        isPlaying = false
        persistLocalPosition()
        await stop(report: true)
    }

    func adopt(serverPosition: PlaybackPosition) {
        guard let d = current else { return }
        if let tid = serverPosition.trackID, let t = d.tracks.first(where: { $0.id == tid }) {
            seek(toTrack: t.index, offset: serverPosition.positionInTrack)
        } else {
            seek(toAbsolute: serverPosition.absolutePosition)
        }
    }

    func setSleepTimer(minutes: Int?) {
        sleepTimerEnds = minutes.map { Date().addingTimeInterval(Double($0) * 60) }
    }

    // MARK: internals

    /// A seek completed: from now on the item's own time is the truth, not the requested target.
    private func seekLanded(token: Int, finished: Bool, trackIndex index: Int, startOffset: Double) {
        let seconds = player.currentTime().seconds
        let known = finished && index == trackIndex && seconds.isFinite
        tracker.finishSeek(token: token, finished: known, playerPosition: known ? startOffset + seconds : nil)
        guard known else { return }
        positionInTrack = seconds
        updateNowPlaying(positionOnly: true)
        report(force: true)
    }

    private func tick(_ time: CMTime) {
        guard let d = current, isPlaying else { return }
        // A dead or resetting player reports ~0; never let that overwrite the real position.
        guard player.currentItem?.status == .readyToPlay, player.currentItem === itemsByIndex[trackIndex] else { return }
        let seconds = time.seconds.isFinite ? time.seconds : 0
        let offset = d.tracks.first { $0.index == trackIndex }?.startOffset ?? 0
        guard tracker.tick(offset + seconds) == .accept else { return }
        let delta = max(0, seconds - positionInTrack)
        if delta < 5 { listenedSinceReport += delta }
        positionInTrack = seconds
        if let ends = sleepTimerEnds, Date() >= ends {
            sleepTimerEnds = nil
            pause()
            return
        }
        report(force: false)
        updateNowPlaying(positionOnly: true)
    }

    private func itemEnded(_ item: AVPlayerItem?) {
        guard let d = current, let item, itemsByIndex[trackIndex] === item else { return }
        if let next = d.tracks.first(where: { $0.index > trackIndex }) {
            trackIndex = next.index
            positionInTrack = 0
            tracker.advance(to: next.startOffset)
            report(force: true)
            updateNowPlaying()
        } else {
            isPlaying = false
            if let update = makeUpdate(finished: true) { onProgress?(update, session) }
            updateNowPlaying()
            advanceToNextEpisode(after: d, manual: false)
        }
    }

    /// A podcast episode ended (or the now-playing "next" button was pressed): ask the server what follows and play it, from
    /// the local file when it is downloaded. Stops quietly when there is none, the setting is off, or the user moved on.
    private func advancer() -> PodcastEpisodeAdvancer? {
        guard let source = provider as? PodcastNextSource, let provider, let serverID else { return nil }
        return PodcastEpisodeAdvancer(
            source: source,
            offline: { [weak self] in self?.offlineDetail?(serverID, $0) },
            currentItemID: { [weak self] in self?.current?.item.id },
            stop: { [weak self] in await self?.stop(report: $0) },
            load: { [weak self] in await self?.load($0, provider: provider, serverID: serverID) })
    }

    private func advanceToNextEpisode(after finished: MediaDetail, manual: Bool) {
        guard let advancer = advancer() else { return }
        advanceTask?.cancel()
        advanceTask = Task {
            if manual { await advancer.skip(finished) } else { await advancer.episodeEnded(finished) }
        }
    }

    /// Now-playing "next" for a podcast episode (lock screen, CarPlay): the next unplayed episode, whatever the setting says.
    func skipToNextEpisode() {
        guard let d = current, PodcastAutoAdvance.episodeID(fromAssetID: d.assetID) != nil else { return }
        advanceToNextEpisode(after: d, manual: true)
    }

    private func makeUpdate(finished: Bool) -> ProgressUpdate? {
        guard let d = current, let serverID else { return nil }
        let track = d.tracks.first { $0.index == trackIndex }
        let total = d.totalDuration
        let abs = absolutePosition
        let update = ProgressUpdate(serverID: serverID, itemID: d.item.id, assetID: d.assetID, trackID: track?.id, trackIndex: trackIndex,
                                    positionInTrack: positionInTrack, absolutePosition: abs, duration: total > 0 ? total : track?.durationSeconds,
                                    percent: total > 0 ? min(1, abs / total) : nil, clientUpdatedAt: Date(), sessionID: session?.sessionID,
                                    isFinished: finished || (total > 0 && abs >= total - 2), listened: listenedSinceReport)
        listenedSinceReport = 0
        return update
    }

    private func report(force: Bool) {
        let now = Date()
        guard force || (now.timeIntervalSince(lastReportAt) >= 15 && abs(absolutePosition - lastReportedPosition) >= 1) else { return }
        guard let update = makeUpdate(finished: false) else { return }
        lastReportAt = now
        lastReportedPosition = update.absolutePosition
        persistLocalPosition()
        onProgress?(update, session)
    }

    private func configureSession() {
        let s = AVAudioSession.sharedInstance()
        try? configureAudioSession()
        NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: s, queue: .main) { [weak self] note in
            guard let info = note.userInfo, let raw = info[AVAudioSessionInterruptionTypeKey] as? UInt, let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            Task { @MainActor in
                if type == .began { self?.pause() } else if let opt = info[AVAudioSessionInterruptionOptionKey] as? UInt, AVAudioSession.InterruptionOptions(rawValue: opt).contains(.shouldResume) { self?.play() }
            }
        }
    }

    private func configureAudioSession() throws {
        try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, policy: .longFormAudio)
    }

    private func configureRemoteCommands() {
        let c = MPRemoteCommandCenter.shared()
        c.playCommand.addTarget { [weak self] _ in Task { @MainActor in self?.play() }; return .success }
        c.pauseCommand.addTarget { [weak self] _ in Task { @MainActor in self?.pause() }; return .success }
        c.togglePlayPauseCommand.addTarget { [weak self] _ in Task { @MainActor in self?.togglePlayPause() }; return .success }
        c.nextTrackCommand.isEnabled = false   // on only while a podcast episode is loaded (updateNowPlaying)
        c.nextTrackCommand.addTarget { [weak self] _ in Task { @MainActor in self?.skipToNextEpisode() }; return .success }
        c.skipForwardCommand.preferredIntervals = [30]
        c.skipForwardCommand.addTarget { [weak self] _ in Task { @MainActor in self?.skip(30) }; return .success }
        c.skipBackwardCommand.preferredIntervals = [15]
        c.skipBackwardCommand.addTarget { [weak self] _ in Task { @MainActor in self?.skip(-15) }; return .success }
        c.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let e = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            Task { @MainActor in self?.seek(toAbsolute: e.positionTime) }
            return .success
        }
        c.changePlaybackRateCommand.supportedPlaybackRates = PlaybackSpeed.options.map { NSNumber(value: $0) }
        c.changePlaybackRateCommand.addTarget { [weak self] event in
            guard let e = event as? MPChangePlaybackRateCommandEvent else { return .commandFailed }
            Task { @MainActor in self?.rate = e.playbackRate }
            return .success
        }
    }

    private func updateNowPlaying(positionOnly: Bool = false) {
        guard let d = current else { return }
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        if !positionOnly {
            MPRemoteCommandCenter.shared().nextTrackCommand.isEnabled = PodcastAutoAdvance.episodeID(fromAssetID: d.assetID) != nil
            info[MPMediaItemPropertyTitle] = d.item.title
            info[MPMediaItemPropertyArtist] = d.item.authorLine
            info[MPMediaItemPropertyAlbumTitle] = currentChapter?.title ?? d.item.series ?? ""
            info[MPNowPlayingInfoPropertyMediaType] = MPNowPlayingInfoMediaType.audio.rawValue
            info[MPNowPlayingInfoPropertyIsLiveStream] = false
        }
        info[MPMediaItemPropertyPlaybackDuration] = duration
        info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = absolutePosition
        let rates = PlaybackSpeed.nowPlayingRates(rate: rate, isPlaying: isPlaying)
        info[MPNowPlayingInfoPropertyPlaybackRate] = rates.playback
        info[MPNowPlayingInfoPropertyDefaultPlaybackRate] = rates.defaultRate
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    func setArtwork(_ image: CGImage?, size: CGSize) {
        #if canImport(UIKit)
        guard let image else { return }
        let ui = UIImage(cgImage: image)
        var info = MPNowPlayingInfoCenter.default().nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyArtwork] = MPMediaItemArtwork(boundsSize: ui.size) { _ in ui }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
        #endif
    }
}

#if canImport(UIKit)
import UIKit
#endif
