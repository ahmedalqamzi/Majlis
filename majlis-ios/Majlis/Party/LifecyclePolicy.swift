import Foundation

// Pure decision logic for hosting a party room across iOS app lifecycle
// transitions. No networking, no UIKit: everything here takes plain values
// in and returns a plain decision, so it is checkable with `swiftc` alone
// (see majlis-ios/checks/lifecycle_policy). PartyHost (not checkable this
// way; it drives NWListener/Engine) is the only caller.
//
// A "checkpoint" is the minimum the app needs to safely resume a paused
// room: which room, at which revision, how long ago it paused. It is not a
// snapshot of room state (the in-process Engine/manager keep that in
// memory); it only tells the app whether resuming that same process's
// memory is still safe, or whether the session must be treated as ended.
enum LifecyclePolicy {
    /// Longer than this, a paused room is not resumed: its rooms expire
    /// server-side on their own idle TTL regardless, but this bounds how
    /// long the app will claim "reconnecting" before explaining the room is
    /// gone instead of leaving players guessing indefinitely.
    static let maxPauseInterval: TimeInterval = 20 * 60

    struct Checkpoint: Equatable {
        let roomCode: String
        let revision: Int
        let pausedAt: TimeInterval  // MonotonicClock.now() at pause, seconds
    }

    /// A persisted checkpoint is untrusted input (UserDefaults can be edited
    /// outside the app, or simply hold a stale/corrupt value from an older
    /// build). Validates plausibility, not just field types: a room code
    /// matching party/core's CODE_RE (`^[A-Z2-9]{6}$`), a revision that could
    /// actually exist (rooms start at revision 1), and a finite non-negative
    /// pause time. An implausible checkpoint must never be treated as
    /// resumable or even as a real "the old room ended" explanation.
    private static func isPlausibleCode(_ code: String) -> Bool {
        let alphabet = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789")
        return code.count == 6 && code.allSatisfy(alphabet.contains)
    }

    static func isPlausible(_ cp: Checkpoint) -> Bool {
        guard isPlausibleCode(cp.roomCode), cp.revision >= 1 else { return false }
        return cp.pausedAt.isFinite && cp.pausedAt >= 0
    }

    /// Cold-launch forensics for a room actively hosted in the foreground
    /// when the process died. `Checkpoint` only ever exists once the app has
    /// actually backgrounded (onBackground is its only producer); a kill
    /// while still in the foreground leaves no checkpoint at all, so without
    /// a second, always-on record there is nothing on the next launch to
    /// explain that gap (blocker 4). `state` distinguishes a kill while
    /// actively playing from one that happened after backgrounding/pausing,
    /// for the cold-launch message only — it plays no part in resumability,
    /// which `Checkpoint`/`onForeground` already own.
    struct HostMarker: Equatable {
        /// `terminal` records a confirmed room.end/room.close the operator
        /// submitted while still in the foreground — distinct from `active`
        /// (still playable) and `paused` (backgrounded) — so a kill right
        /// after closing a room is reported on the next cold launch as "the
        /// room was closed", not "ended while actively hosting" (L02A
        /// blocker 4: "accepted pause/end/close mark active").
        enum State: String { case active, paused, terminal }
        let roomCode: String
        var revision: Int
        var state: State
    }

    static func isPlausible(_ m: HostMarker) -> Bool {
        isPlausibleCode(m.roomCode) && m.revision >= 1
    }

    enum ForegroundAction: Equatable {
        /// No room was hosted, or it was already ended; nothing to resume.
        case none
        /// Same process, same room: rebind the listener and resume normally.
        case rebind(Checkpoint)
        /// Checkpoint is too old or missing: don't silently reopen a
        /// different room under the old identity. End the old session and
        /// say so; players must rejoin a new room.
        case endExpired(Checkpoint)
    }

    static func onBackground(hostedRoomCode: String?, revision: Int, now: TimeInterval) -> Checkpoint? {
        guard let code = hostedRoomCode else { return nil }
        return Checkpoint(roomCode: code, revision: revision, pausedAt: now)
    }

    static func onForeground(_ checkpoint: Checkpoint?, now: TimeInterval) -> ForegroundAction {
        guard let cp = checkpoint else { return .none }
        let elapsed = now - cp.pausedAt
        if elapsed < 0 || elapsed > maxPauseInterval { return .endExpired(cp) }
        return .rebind(cp)
    }

    /// Process kill (or a cold launch finding a leftover checkpoint from a
    /// prior launch, which always means the in-process engine state is
    /// gone): restoring is never possible, since memory-only rooms die with
    /// the process. The app must end the old session explicitly rather than
    /// ever opening a *new* room and presenting it to returning players as
    /// if it were the old one.
    static func onColdLaunch(leftoverCheckpoint: Checkpoint?) -> ForegroundAction {
        guard let cp = leftoverCheckpoint else { return .none }
        return .endExpired(cp)
    }
}
