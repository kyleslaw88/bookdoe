import Foundation

/// Decides which periodic player times become the playback position. It keeps a user seek from
/// racing the regression guard: a tick queued before the seek must not undo it, and once the
/// seek lands the player's own time is the truth. Positions are absolute seconds.
public struct PositionTracker: Equatable, Sendable {
    public enum Decision: Equatable, Sendable {
        case accept
        case ignore
    }

    /// Two regressed ticks this close together (seconds) count as one consistent position.
    public static let consistencyWindow: Double = 5
    /// Consecutive consistent regressed ticks that make the player's position the truth.
    public static let consistentTicksToAccept = 2

    public private(set) var position: Double
    /// The last position a tick (or a landed seek) confirmed.
    public private(set) var lastGood: Double
    /// The position the user last chose, until a tick after that seek has been accepted.
    public private(set) var userTarget: Double?
    public private(set) var pendingSeek: Int?

    private var seekCounter = 0
    private var suspect: Double?
    private var suspectCount = 0

    public init(position: Double = 0) {
        self.position = position
        self.lastGood = position
    }

    public var isSeeking: Bool { pendingSeek != nil }

    /// Where recovery should resume: the user's last choice if they seeked since, else the
    /// furthest confirmed position.
    public var recoveryPosition: Double { userTarget ?? max(lastGood, position) }

    /// A user seek (scrub, skip, lock screen, CarPlay, server adopt) starts. Ticks are ignored
    /// until `finishSeek` is called with the returned token.
    @discardableResult
    public mutating func beginSeek(to target: Double) -> Int {
        seekCounter += 1
        pendingSeek = seekCounter
        position = target
        lastGood = target
        userTarget = target
        clearSuspect()
        return seekCounter
    }

    /// The player finished (or abandoned) a seek. A superseded seek is ignored. When it
    /// completed, `playerPosition` (the item's current time) replaces the target.
    public mutating func finishSeek(token: Int, finished: Bool, playerPosition: Double?) {
        guard pendingSeek == token else { return }
        pendingSeek = nil
        guard finished, let playerPosition, playerPosition.isFinite else { return }
        position = playerPosition
        lastGood = playerPosition
        userTarget = playerPosition
        clearSuspect()
    }

    /// Set the position without a seek (the player moved on to the next track by itself).
    public mutating func advance(to absolute: Double) {
        position = absolute
        lastGood = absolute
        userTarget = nil
        clearSuspect()
    }

    /// Judge a periodic tick. On `.accept`, `position` is the new position.
    public mutating func tick(_ absolute: Double) -> Decision {
        if pendingSeek != nil { return .ignore }
        guard absolute.isFinite else { return .ignore }
        if PositionGuard.isSuspiciousRegression(previous: position, new: absolute) {
            if let s = suspect, absolute >= s, absolute - s <= Self.consistencyWindow {
                suspectCount += 1
            } else {
                suspectCount = 1
            }
            suspect = absolute
            guard suspectCount >= Self.consistentTicksToAccept else { return .ignore }
        }
        clearSuspect()
        position = absolute
        lastGood = absolute
        userTarget = nil
        return .accept
    }

    private mutating func clearSuspect() {
        suspect = nil
        suspectCount = 0
    }
}
