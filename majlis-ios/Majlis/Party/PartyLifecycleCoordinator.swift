import Foundation
import SwiftUI
import UIKit

// Wires LifecyclePolicy's pure pause/resume/kill decisions (see
// LifecyclePolicy.swift) to the actual app lifecycle and PartyHost. This
// class is the only caller of LifecyclePolicy mentioned in its doc comment;
// it owns the one piece LifecyclePolicy deliberately has no opinion about —
// where the checkpoint is persisted across process death.
//
// Not checkable with standalone swiftc (UIKit/SwiftUI observable object);
// see LifecyclePolicy's own check for the decision logic this drives.
@MainActor
final class PartyLifecycleCoordinator: ObservableObject {
    enum Status: Equatable {
        case stopped
        case starting
        case hosting(host: String, port: UInt16, code: String)
        /// Backgrounded while playing: the room itself was told to
        /// room.pause (game clock/actions frozen server-side, per
        /// party/core's reduceRoom) and the network listener was torn down
        /// via PartyHost.suspendNetworking. Still within LifecyclePolicy's
        /// pause window; resumeHosting() must be called explicitly (no
        /// auto-reconnect) to rebind and send room.resume.
        case pausedResumable(code: String)
        case reconnecting
        case ended(reason: String)
        case failed(String)
        /// Resuming a paused room failed (rebind, or an explicit room.resume
        /// rejected/erroring) — distinct from `.failed`, which otherwise
        /// reads as a dead end the UI can only restart from. The checkpoint
        /// for `code` is still intact (resumeHosting never clears it ahead
        /// of a confirmed success), so the host can retry the same resume,
        /// not just abandon the room and start a new one (L02A blocker 5).
        case resumeFailed(code: String, message: String)
    }

    @Published private(set) var status: Status = .stopped

    private var host: PartyHost?
    private let makeHost: () throws -> PartyHost
    private var revision = 0
    private var hostedRoomCode: String?
    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid
    /// Set synchronously at the top of `onBackground()`, before any async
    /// work starts; cleared once that call's pause+suspend chain actually
    /// finishes (or bails out early). See `onBackground()`'s doc comment for
    /// the race this closes.
    private var backgroundingInFlight = false
    /// `isIdleTimerDisabled` right before Party last claimed it (nil once
    /// Party does not currently hold it). Restored verbatim when ownership
    /// is given back, instead of hard-coding `false` — which would always
    /// stomp on whatever another tab/screen wanted, rather than the value
    /// that actually held before Party started hosting (L02A blocker 7).
    private var idleTimerPriorState: Bool?
    /// Identifies the current session/operation epoch. Bumped exactly where
    /// a prior async callback must stop being allowed to mutate shared state:
    /// entering `startHosting` (a new room is about to exist) and
    /// `endSession` (this room is about to be gone). Every async operation
    /// that can still be in flight when one of those happens — start/create,
    /// background pause+suspend, resume/rebind, the room.resume submit, and
    /// the background-task expiration handler — captures this value
    /// synchronously before kicking off its network call and re-checks it in
    /// its completion before touching `status`/checkpoint/marker/idle
    /// ownership/`hostedRoomCode`. A mismatch means a newer session or an
    /// explicit end already happened; the stale completion releases only the
    /// OS/host resource it personally owns (its own background-task id, or
    /// the listener/room binding its own rebind/forget call produced) and
    /// otherwise does nothing — it must never resurrect `.ended`, attach a
    /// listener, claim the idle timer, or write a marker for a session that
    /// has moved on (L02A blocker: stale suspend-completion-after-End).
    private var sessionGeneration = 0
    /// Identifies the current *background operation* within a session —
    /// distinct from `sessionGeneration`, which `resumeHosting()` does not
    /// bump because resuming continues the same session. Without this, a
    /// background→foreground→resume cycle followed by a SECOND background
    /// operation (still the same `sessionGeneration`) leaves the FIRST
    /// operation's background-task expiration handler still passing its
    /// generation check; it would then end the second operation's live
    /// background-task id and resurrect `.reconnecting` out from under it.
    /// Bumped at the top of `onBackground()`, right alongside
    /// `backgroundingInFlight`; every completion that chain can still
    /// produce (expiration handler, state/submitAction/suspendNetworking
    /// completions) re-checks it exactly like `sessionGeneration`.
    private var backgroundOperation = 0
    /// `false` for exactly as long as the operation identified by
    /// `backgroundOperation` is still in flight; flipped to `true` the
    /// moment it settles (normally or via its own expiration), after which
    /// its queued expiration closure must become permanently inert even if
    /// `backgroundOperation` hasn't moved on yet (nothing else has
    /// backgrounded since).
    private var backgroundOperationSettled = true
    /// Set alongside detaching `host` in `endSession()`, to the generation
    /// that detach belongs to; cleared only once that detach's real
    /// `stop()` completion actually lands. Lets a duplicate `endSession()`
    /// call made while that stop is still pending recognize "a terminal end
    /// for THIS generation already owns the eventual confirmation" and
    /// no-op, instead of hitting the `host == nil` branch and declaring
    /// `.ended`/clearing the marker ahead of the real confirmation.
    private var pendingEndGeneration: Int?
    /// Latest `onBackground`/`onForeground` (or `onViewHidden`/`onViewVisible`)
    /// call, tracked independently of `status` because the backgrounding
    /// chain below only moves `status` once its own async work finishes —
    /// a return-to-foreground raised while that chain is still running must
    /// not be lost, and the eventual completion must resolve to what the
    /// user is actually looking at now, not a reconnecting dead end nobody
    /// will ever clear.
    private enum Visibility { case foreground, background }
    private var visibilityIntent: Visibility = .foreground

    private static let checkpointKey = "party.lifecycle.checkpoint"
    private static let hostMarkerKey = "party.lifecycle.hostmarker"

    private func claimIdleTimer() {
        if idleTimerPriorState == nil { idleTimerPriorState = UIApplication.shared.isIdleTimerDisabled }
        UIApplication.shared.isIdleTimerDisabled = true
    }

    private func restoreIdleTimer() {
        guard let prior = idleTimerPriorState else { return }
        UIApplication.shared.isIdleTimerDisabled = prior
        idleTimerPriorState = nil
    }

    /// `makeHost` builds a fresh PartyHost (new Engine, new Server) on
    /// demand. Needed because Server.shutdown (called by endSession via
    /// host.stop) is terminal — it flips party/core's RoomManager.stopped
    /// permanently with no way back, so starting a *new* room after a game
    /// ends must construct a new PartyHost rather than reuse the one whose
    /// server is now permanently stopped.
    ///
    /// Does not throw: `makeHost()` reads bundled resources and can fail
    /// (missing PartyContent/PartyWeb, a corrupt pack); OperatorView used to
    /// wrap this initializer in `try!`, crashing the whole app on launch for
    /// that recoverable case instead of showing the diagnostic the brief
    /// requires (L02A blocker 7). A failure here is reported the same way a
    /// later `startHosting`/`resumeHosting` failure is: `.failed`, with
    /// `host` left `nil` so the next `startHosting` retries `makeHost()`.
    init(makeHost: @escaping () throws -> PartyHost) {
        self.makeHost = makeHost
        do {
            host = try makeHost()
        } catch {
            host = nil
            status = .failed("could not start: \(error)")
        }
        // A leftover checkpoint/host marker only exists here if the process
        // was killed while hosting (normal quit/background always clears
        // both via endSession, and an explicit resumeHosting() clears the
        // checkpoint on success). Validate plausibility first: untrusted
        // persisted data (a stale value from an older build, or a corrupted
        // default) must not be reported as a real ended room. Per
        // LifecyclePolicy.onColdLaunch, a plausible leftover checkpoint is
        // always ended, never silently reopened as a new room under the old
        // code — memory-only room state cannot have survived the process
        // restart regardless of how long ago it paused. The host marker is
        // the only record of a kill that happened before the app ever
        // backgrounded (no checkpoint is ever written for that case); it is
        // consulted only when no checkpoint already explains the gap.
        let leftoverCheckpoint = Self.loadCheckpoint()
        let leftoverMarker = Self.loadHostMarker()
        Self.clearCheckpoint()
        Self.clearHostMarker()
        if host != nil {
            if let leftover = leftoverCheckpoint,
               case .endExpired = LifecyclePolicy.onColdLaunch(leftoverCheckpoint: leftover) {
                status = .ended(reason: "The previous hosted room (\(leftover.roomCode)) ended when the app closed.")
            } else if let marker = leftoverMarker {
                // The message distinguishes what the kill actually
                // interrupted (L02A blocker 4: the old text claimed "while
                // actively hosting" even for a marker left in .paused or
                // .terminal state).
                let detail: String
                switch marker.state {
                case .active: detail = "ended when the app closed while actively hosting"
                case .paused: detail = "ended when the app closed while backgrounded and paused"
                case .terminal: detail = "was already closed when the app closed"
                }
                status = .ended(reason: "The previous hosted room (\(marker.roomCode)) \(detail).")
            }
        }
    }

    func startHosting(body: [String: Any]) {
        // See makeHost's doc comment: a session that ended (or one whose
        // PartyHost was never successfully created) used a now
        // permanently-stopped — or missing — Server; get a live one before
        // hosting again.
        let needsFreshHost: Bool
        if case .ended = status { needsFreshHost = true } else { needsFreshHost = host == nil }
        if needsFreshHost {
            do { host = try makeHost() } catch {
                status = .failed("could not start a new session: \(error)")
                return
            }
        }
        guard let host else {
            status = .failed("could not start a new session")
            return
        }
        // A new session is starting: anything still in flight for whatever
        // came before (a slow resume/rebind, a backgrounding chain) is now
        // stale and must not touch this room once its callback lands.
        // `backgroundingInFlight` belongs to that old epoch too — reset it
        // here rather than leaving it permanently stuck `true`, which would
        // otherwise block `onBackground()` for this brand-new session
        // forever (its own stale completion is never allowed to clear it;
        // see the generation guard there).
        sessionGeneration += 1
        let gen = sessionGeneration
        backgroundingInFlight = false
        backgroundOperationSettled = true
        status = .starting
        host.start { [weak self] result in
            guard let self, self.sessionGeneration == gen else { return }
            Task { @MainActor in
                guard self.sessionGeneration == gen else { return }
                switch result {
                case .failure(let err):
                    self.status = .failed(String(describing: err))
                case .success(let (h, p)):
                    guard let host = self.host else { return }
                    host.createRoom(body: body) { [weak self] created in
                        guard let self, self.sessionGeneration == gen else { return }
                        Task { @MainActor in
                            guard self.sessionGeneration == gen else { return }
                            switch created {
                            case .failure(let err):
                                self.status = .failed(String(describing: err))
                            case .success(let (code, revision)):
                                self.hostedRoomCode = code
                                self.revision = revision
                                self.status = .hosting(host: h, port: p, code: code)
                                Self.saveHostMarker(LifecyclePolicy.HostMarker(roomCode: code, revision: revision, state: .active))
                                self.claimIdleTimer()
                            }
                        }
                    }
                }
            }
        }
    }

    /// UIApplicationDelegate/scenePhase hook: app is about to go to the
    /// background or lock. If a room is actively playing, send it
    /// room.pause first — party/core's reduceRoom then freezes it (deepcopy,
    /// phase="paused") and refuses every non-room action with "paused" until
    /// resumed, so round/game state and player identities/scores are
    /// untouched and no gameplay action can land while backgrounded. Only
    /// then tear down network serving (PartyHost.suspendNetworking) and
    /// persist the checkpoint, and ask iOS for a little extra time to do
    /// both before suspension.
    func onBackground() {
        // Guards on `status == .hosting`, not just `hostedRoomCode`/`host`
        // being set, because `hostedRoomCode` is intentionally NOT cleared
        // by finishBackgrounding — resumeHosting needs it to still match the
        // checkpoint. Without this status check, a real OS background
        // (which moves status to .reconnecting) followed by onViewHidden()
        // from a SwiftUI .onDisappear that fires afterward (e.g. the
        // operator's tab switch completing after the OS already backgrounded
        // the app) would see the still-set hostedRoomCode/host and run the
        // whole pause+suspendNetworking sequence a second time (L02A blocker
        // 7: duplicate suspension).
        //
        // `status` only actually changes once the async host.state()/
        // submitAction() chain below completes (on MainActor, but in a
        // separately scheduled Task). A second onBackground()/onViewHidden()
        // call made synchronously back-to-back with the first (e.g. the OS
        // background notification and the view's .onDisappear firing in the
        // same run-loop turn) would therefore still see `status == .hosting`
        // and start a second, independent pause+suspend chain before the
        // first one has had a chance to move `status` away. `backgroundingInFlight`
        // closes that race by being set synchronously, before any async work
        // starts, so the second call's guard sees it immediately.
        guard case .hosting = status, !backgroundingInFlight, let code = hostedRoomCode, let host else { return }
        backgroundingInFlight = true
        visibilityIntent = .background
        let gen = sessionGeneration
        backgroundOperation += 1
        let opID = backgroundOperation
        backgroundOperationSettled = false
        let now = currentTime()
        backgroundTask = UIApplication.shared.beginBackgroundTask { [weak self] in
            Task { @MainActor in
                // `sessionGeneration` alone is not enough here: resumeHosting()
                // deliberately does not bump it (same session continuing), so
                // a background→foreground→resume cycle followed by a second
                // background operation would still match on generation. Gate
                // on this operation's own identity and "not already settled"
                // too, so a retired handler whose task already ended (or
                // whose operation moved on) can never touch the current
                // operation's background-task id/state/ownership — it does
                // nothing at all once stale.
                guard let self, self.sessionGeneration == gen,
                      self.backgroundOperation == opID, !self.backgroundOperationSettled else { return }
                // Background time ran out before our pause+suspend chain
                // finished: we don't know whether room.pause/suspendNetworking
                // landed, so report the truthful in-between state (still
                // reachable via resumeHosting if a checkpoint exists, not a
                // silently-lost session) and release ownership rather than
                // leaving `backgroundingInFlight` stuck `true` forever.
                self.backgroundOperationSettled = true
                self.backgroundingInFlight = false
                self.endBackgroundTask(self.backgroundTask)
                self.status = .reconnecting
            }
        }
        let myBgTask = backgroundTask
        host.state(code: code) { [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                guard self.sessionGeneration == gen else {
                    // A newer session or an explicit end already happened;
                    // only release this call's own background-task slot.
                    self.endBackgroundTask(myBgTask)
                    return
                }
                switch result {
                case .failure:
                    // The room is already gone (ended/closed/expired, or an
                    // unauthorized/stale state) — nothing to pause, nothing
                    // safe to checkpoint as resumable.
                    self.hostedRoomCode = nil
                    self.backgroundingInFlight = false
                    self.backgroundOperationSettled = true
                    self.endBackgroundTask(myBgTask)
                case .success(let state):
                    if let revision = state["revision"] as? Int { self.revision = revision }
                    if (state["phase"] as? String) == "playing" {
                        host.submitAction(code: code, type: "room.pause") { [weak self] actionResult in
                            guard let self else { return }
                            Task { @MainActor in
                                guard self.sessionGeneration == gen else {
                                    self.endBackgroundTask(myBgTask)
                                    return
                                }
                                if case .success(let r) = actionResult, r.accepted {
                                    self.revision = r.revision
                                    self.finishBackgrounding(code: code, now: now, gen: gen, bgTask: myBgTask)
                                } else {
                                    // room.pause was rejected or errored: the room's
                                    // actual phase is unknown/unchanged, so writing a
                                    // paused marker or a resumable checkpoint here
                                    // (finishBackgrounding's job) would claim a pause
                                    // that never actually landed. Leaving status at
                                    // `.reconnecting` here would be truthful about the
                                    // pause but leaves the host's listener/server
                                    // running unattended with no checkpoint to ever
                                    // resume from and no UI escape from the spinner —
                                    // a dead end. Drive the same confirmed terminal-end
                                    // path `endSession()` already uses elsewhere
                                    // instead: it retires this generation's host
                                    // synchronously (so a replacement session can get
                                    // a fresh one immediately), actually stops serving,
                                    // and only confirms `.ended`/clears the
                                    // marker/checkpoint once that stop genuinely
                                    // completes — never fabricating a pause, and never
                                    // touching a newer generation that may have already
                                    // superseded this one.
                                    self.endBackgroundTask(myBgTask)
                                    self.endSession(reason: "Couldn't confirm the room was paused while the app was in the background — the room was ended. Players will need to rejoin a new room.")
                                }
                            }
                        }
                    } else {
                        // lobby/paused/ended/closed: nothing to freeze; a
                        // lobby or already-paused room has no running clock,
                        // and ended/closed will be caught as endExpired-like
                        // on return via the state check in resumeHosting.
                        self.finishBackgrounding(code: code, now: now, gen: gen, bgTask: myBgTask)
                    }
                }
            }
        }
    }

    private func finishBackgrounding(code: String, now: TimeInterval, gen: Int, bgTask: UIBackgroundTaskIdentifier) {
        guard sessionGeneration == gen else {
            // Stale: an End or a new session already superseded this
            // backgrounding attempt. Release only our own background-task
            // slot; never touch status/checkpoint/marker/idle ownership or
            // `backgroundingInFlight` — those belong to whatever generation
            // is current now, and this cleanup must not stop or reuse the
            // current host (L02A blocker: late-completion resurrection).
            endBackgroundTask(bgTask)
            return
        }
        guard let cp = LifecyclePolicy.onBackground(hostedRoomCode: code, revision: revision, now: now),
              let host else {
            backgroundingInFlight = false
            backgroundOperationSettled = true
            endBackgroundTask(bgTask)
            return
        }
        Self.saveCheckpoint(cp)
        Self.saveHostMarker(LifecyclePolicy.HostMarker(roomCode: code, revision: revision, state: .paused))
        restoreIdleTimer()
        host.suspendNetworking { [weak self] in
            Task { @MainActor in
                guard let self, self.sessionGeneration == gen else {
                    self?.endBackgroundTask(bgTask)
                    return
                }
                self.backgroundOperationSettled = true
                self.backgroundingInFlight = false
                self.endBackgroundTask(bgTask)
                // The user may have already returned to the foreground
                // while this suspend was in flight (onForeground() running
                // early found no checkpoint yet and no-opped). Resolve to
                // what they're actually looking at now instead of a
                // `.reconnecting` state nothing will ever clear.
                self.status = self.visibilityIntent == .foreground
                    ? .pausedResumable(code: code)
                    : .reconnecting
            }
        }
    }

    /// UIApplicationDelegate/scenePhase hook: app returned to the
    /// foreground. Within the pause window, this only moves to
    /// `.pausedResumable` — it does not reconnect by itself. Resuming
    /// (rebinding the listener, possibly at a new Wi-Fi address, and telling
    /// the room room.resume) is an explicit action the host must take
    /// (resumeHosting()); the checkpoint is kept until that attempt actually
    /// succeeds or the session is explicitly ended, so a failed/incomplete
    /// resume can always be retried rather than silently losing the room.
    func onForeground() {
        // Recorded even if there's no checkpoint yet below (a background
        // pause+suspend chain may still be running) so that chain's own
        // completion can resolve to resumable instead of a stuck
        // `.reconnecting` once it finishes — see `finishBackgrounding`.
        visibilityIntent = .foreground
        guard let cp = Self.loadCheckpoint() else { return }
        let now = currentTime()
        switch LifecyclePolicy.onForeground(cp, now: now) {
        case .none:
            break
        case .rebind:
            status = .pausedResumable(code: cp.roomCode)
        case .endExpired:
            Self.clearCheckpoint()
            endSession(reason: "Away too long — the room (\(cp.roomCode)) was ended. Players will need to rejoin a new room.")
        }
    }

    /// OperatorView's hosting screen `.onDisappear` hook: the operator
    /// switched away from the Party tab (or the screen is otherwise no
    /// longer visible) while still in the foreground. Hosting a room is
    /// meant to stop when the operator isn't looking at it, not merely stop
    /// polling while the listener/server keep running unattended (L02A
    /// blocker 7: "tab/visibility-based hosting ownership"). Reuses the same
    /// pure LifecyclePolicy decision and the same effects `onBackground()`
    /// already drives (room.pause + suspendNetworking + checkpoint), rather
    /// than duplicating that logic for a second "paused" trigger. Real OS
    /// backgrounding already moves `status` away from `.hosting` first (via
    /// its own onBackground call), so by the time SwiftUI's `.onDisappear`
    /// for a no-longer-visible hostingScreen fires afterward, `onBackground`
    /// here is a guarded no-op (the `status == .hosting` guard added above
    /// no longer matches) — it never double-pauses the same background
    /// transition.
    func onViewHidden() {
        onBackground()
    }

    /// OperatorView's hosting screen `.onAppear` hook: the operator switched
    /// back to the Party tab while still in the foreground. Mirrors
    /// `onForeground()` so the same pause-window / already-too-long decision
    /// applies whether the gap was real backgrounding or just switching tabs.
    func onViewVisible() {
        onForeground()
    }

    /// Explicit host-initiated resume from `.pausedResumable`. Rebinds the
    /// listener first (the Wi-Fi address may have changed while
    /// backgrounded), then re-checks the room's actual phase before sending
    /// room.resume — the room could have expired or been closed/ended by
    /// its own idle TTL while the app was away even within the pause
    /// window — rather than assuming the paused snapshot is still valid.
    /// The checkpoint is only cleared once resume (or an explicit end) is
    /// confirmed, never cleared ahead of a rebind that might still fail;
    /// calling this again after a failure is safe and simply retries.
    func resumeHosting() {
        // Retryable from either `.pausedResumable` (first attempt) or a
        // previously-failed retry (L02A blocker 5): both leave the same
        // checkpoint in place, so the same code path drives a retry.
        let code: String
        switch status {
        case .pausedResumable(let c): code = c
        case .resumeFailed(let c, _): code = c
        default: return
        }
        guard let cp = Self.loadCheckpoint(), cp.roomCode == code else {
            endSession(reason: "The paused room (\(code)) could not be resumed.")
            return
        }
        guard let host else {
            status = .resumeFailed(code: code, message: "hosting is not available")
            return
        }
        // Resuming continues the SAME session/room — it does not bump the
        // generation itself — but it's still a multi-step async chain that
        // an End (or a brand-new startHosting) can race against, per
        // ACCEPTANCE-SEQUENCE row 3: capture now, re-check at every
        // completion before touching shared state.
        let gen = sessionGeneration
        status = .starting
        host.rebind { [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                guard self.sessionGeneration == gen else {
                    // End or a new session already happened. `host` here is
                    // the specific instance this resumeHosting() call
                    // captured, never `self.host` (which already points
                    // elsewhere, or nil, by now) — so retiring it can never
                    // touch the current/replacement host. On success, the
                    // rebind may have bound a live listener/room token on
                    // this now-abandoned instance; forget(code:) only drops
                    // the token via the bridge, it does NOT close the
                    // NWListener/Server the rebind just (re)established, so
                    // follow it with a full stop() to actually release that
                    // listener. On failure nothing may have bound, but stop()
                    // is safe/idempotent and covers any partial bind attempt
                    // either way (L02A: stale-rebind-listener-teardown).
                    if case .success = result {
                        host.forget(code: code) { _ in host.stop {} }
                    } else {
                        host.stop {}
                    }
                    return
                }
                switch result {
                case .failure(let err):
                    // Checkpoint is left intact: the caller (UI) can call
                    // resumeHosting() again from .resumeFailed.
                    self.status = .resumeFailed(code: code, message: String(describing: err))
                case .success(let (h, p)):
                    guard let host = self.host else { return }
                    host.state(code: code) { [weak self] stateResult in
                        guard let self else { return }
                        Task { @MainActor in
                            self.finishResuming(code: code, host: h, port: p, stateResult: stateResult, gen: gen)
                        }
                    }
                }
            }
        }
    }

    private func finishResuming(code: String, host h: String, port p: UInt16,
                                 stateResult: Result<[String: Any], PartyHost.HostError>, gen: Int) {
        guard sessionGeneration == gen else {
            // Stale: End or a new session superseded this resume attempt
            // while the state fetch was in flight. Nothing of this
            // function's own to release; just don't touch current state.
            return
        }
        guard case .success(let state) = stateResult, let phase = state["phase"] as? String,
              phase != "ended", phase != "closed" else {
            endSession(reason: "The room (\(code)) ended while the app was away.")
            return
        }
        guard phase == "paused" else {
            // Already playing/lobby (e.g. the pause action on the way into
            // the background never actually landed): nothing more to resume.
            Self.clearCheckpoint()
            hostedRoomCode = code
            status = .hosting(host: h, port: p, code: code)
            Self.saveHostMarker(LifecyclePolicy.HostMarker(roomCode: code, revision: revision, state: .active))
            claimIdleTimer()
            return
        }
        guard let host else {
            status = .resumeFailed(code: code, message: "hosting is not available")
            return
        }
        host.submitAction(code: code, type: "room.resume") { [weak self] actionResult in
            guard let self else { return }
            Task { @MainActor in
                guard self.sessionGeneration == gen else { return }
                switch actionResult {
                case .success(let r) where r.accepted:
                    self.revision = r.revision
                    Self.clearCheckpoint()
                    self.hostedRoomCode = code
                    self.status = .hosting(host: h, port: p, code: code)
                    Self.saveHostMarker(LifecyclePolicy.HostMarker(roomCode: code, revision: r.revision, state: .active))
                    self.claimIdleTimer()
                case .success, .failure:
                    // Rejected or failed room.resume: leave the checkpoint
                    // and the rebound listener in place and surface the
                    // failure as retryable, not a dead end (L02A blocker 5).
                    self.status = .resumeFailed(code: code, message: "could not resume room \(code)")
                }
            }
        }
    }

    /// Lets the operator UI drive room lifecycle/round actions (OperatorView)
    /// without that view ever touching `host`/`server` directly — the only
    /// path stays the same in-process PartyHost.submitAction bridge
    /// item3/4's lifecycle code already uses for room.pause/room.resume.
    /// `roundId` binds the action to the round the operator was actually
    /// looking at (party/core's own stale-round protection then refuses it
    /// if the room has moved on), mirroring party/web/js/host.js's `act()`.
    func submitAction(type: String, payload: [String: Any] = [:], roundId: String? = nil,
                       completion: @escaping (Result<(accepted: Bool, revision: Int, error: String?), PartyHost.HostError>) -> Void) {
        guard let code = hostedRoomCode, let host else {
            completion(.failure(.internalError))
            return
        }
        // The caller still gets the actual network result either way; only
        // the marker write is generation-gated, so a late operator-action
        // completion can't record state for a room that has already ended
        // or been replaced (ACCEPTANCE-SEQUENCE: "markers track CONFIRMED
        // room state").
        let gen = sessionGeneration
        host.submitAction(code: code, type: type, payload: payload, roundId: roundId) { [weak self] result in
            if case .success(let r) = result, r.accepted, self?.sessionGeneration == gen {
                // The marker must reflect what the room actually is now, not
                // just "an action landed" (L02A blocker 4: "accepted
                // pause/end/close mark active" regardless of which action it
                // was). room.pause can also be submitted through this same
                // wrapper — the operator's manual Pause button in
                // OperatorView calls submitAction(type: "room.pause")
                // directly, it does not go through
                // onBackground/finishBackgrounding — so that case must mark
                // .paused here too, not fall into the default .active.
                let state: LifecyclePolicy.HostMarker.State
                switch type {
                case "room.end", "room.close": state = .terminal
                case "room.pause": state = .paused
                default: state = .active
                }
                Self.saveHostMarker(LifecyclePolicy.HostMarker(roomCode: code, revision: r.revision, state: state))
            }
            completion(result)
        }
    }

    /// Operator-view state polling (OperatorView), same in-process bridge as
    /// submitAction above.
    func fetchState(completion: @escaping (Result<[String: Any], PartyHost.HostError>) -> Void) {
        guard let code = hostedRoomCode, let host else {
            completion(.failure(.internalError))
            return
        }
        host.state(code: code, completion: completion)
    }

    func endSession(reason: String) {
        // Clearing happens only once the end is actually confirmed (below),
        // never ahead of it: a process kill between calling endSession() and
        // host.stop()'s completion must still leave a truthful marker behind
        // for the next cold launch (L02A blocker 4 — the old code cleared
        // both here, before forget/stop ever ran).
        restoreIdleTimer()
        let code = hostedRoomCode
        hostedRoomCode = nil
        backgroundingInFlight = false
        backgroundOperationSettled = true
        if let endingHost = host {
            // This End ends the CURRENT generation; bump past it so every
            // callback still holding the old value — a late suspend
            // completion, a resume/rebind in flight — now reads as stale
            // and leaves status/checkpoint/marker alone (the exact bug this
            // correction fixes).
            sessionGeneration += 1
            let gen = sessionGeneration
            // Retire this host's reference NOW, synchronously, rather than
            // waiting for stop()'s completion below. Real PartyHost.stop() ->
            // Server.shutdown is terminal: nothing can ever serve on this
            // instance again. `startHosting` only constructs a fresh host via
            // `makeHost()` when `host == nil`; leaving `self.host` pointing at
            // this (now terminal-bound) instance until stop() completes would
            // let a `startHosting` call made in that window reuse it instead of
            // getting a live host for the replacement room (L02A:
            // fresh-host-during-delayed-stop). `status` itself is NOT set here —
            // it only becomes `.ended` once stop() actually confirms below, so a
            // kill in between still leaves a truthful pending/marker state, and
            // a `startHosting` racing ahead of that confirmation is unaffected
            // either way since it bumps `sessionGeneration` again itself.
            host = nil
            // Records that THIS generation's end is the one owning the
            // eventual confirmation below — `host == nil` alone can't tell a
            // detached-but-pending end apart from "never had a host"/"a
            // prior end already confirmed", which is exactly what let a
            // duplicate endSession() call declare `.ended` and clear the
            // marker ahead of the real stop() (L02A: duplicate-End-race).
            pendingEndGeneration = gen
            endingHost.forget(code: code) { _ in }
            endingHost.stop { [weak self] in
                Task { @MainActor in
                    guard let self else { return }
                    // If a NEWER session has already started by the time
                    // this stop() completion lands (gen moved again past
                    // `gen`), clearing the checkpoint/marker here would
                    // erase that newer session's own state instead of this
                    // ended one's. `pendingEndGeneration` being something
                    // other than `gen` means a later endSession() call for
                    // this same generation already owns the confirmation
                    // (not possible today since endSession only reaches
                    // this branch once per generation, but kept symmetric
                    // with the duplicate-call guard below).
                    guard self.sessionGeneration == gen, self.pendingEndGeneration == gen else { return }
                    self.pendingEndGeneration = nil
                    Self.clearCheckpoint()
                    Self.clearHostMarker()
                    self.status = .ended(reason: reason)
                }
            }
            return
        }
        // `host` is nil here either because this session never had one, or
        // because an end for the CURRENT generation already detached it and
        // is waiting on `stop()` above to confirm. Only the latter has a
        // pending confirmation that owns the eventual marker/checkpoint/
        // status transition; a duplicate call during that window must leave
        // it alone rather than racing ahead and declaring terminal
        // confirmation before the real stop lands.
        if let pending = pendingEndGeneration, pending == sessionGeneration {
            return
        }
        sessionGeneration += 1
        Self.clearCheckpoint()
        Self.clearHostMarker()
        status = .ended(reason: reason)
    }

    private func endBackgroundTask(_ identifier: UIBackgroundTaskIdentifier) {
        if identifier != .invalid {
            UIApplication.shared.endBackgroundTask(identifier)
        }
        if backgroundTask == identifier {
            backgroundTask = .invalid
        }
    }

    private func currentTime() -> TimeInterval {
        Double(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1_000_000_000
    }

    // Checkpoints are persisted in UserDefaults, not the room state itself
    // (which lives only in the Engine's in-process memory and cannot survive
    // the process). A monotonic pausedAt only means something within the
    // same process's clock origin, so this value is only ever read back
    // within the same process lifetime (foreground/background), never
    // across a process restart — onColdLaunch always treats a leftover
    // value found at init as expired regardless of its age.
    private static func saveCheckpoint(_ cp: LifecyclePolicy.Checkpoint) {
        let dict: [String: Any] = ["roomCode": cp.roomCode, "revision": cp.revision, "pausedAt": cp.pausedAt]
        UserDefaults.standard.set(dict, forKey: checkpointKey)
    }

    /// Returns nil for anything that isn't a plausible checkpoint (see
    /// LifecyclePolicy.isPlausible) as well as for a genuinely missing one,
    /// so every caller already treats "corrupt" the same as "absent" without
    /// having to check separately.
    private static func loadCheckpoint() -> LifecyclePolicy.Checkpoint? {
        guard let dict = UserDefaults.standard.dictionary(forKey: checkpointKey),
              let code = dict["roomCode"] as? String,
              let revision = dict["revision"] as? Int,
              let pausedAt = dict["pausedAt"] as? Double else {
            return nil
        }
        let cp = LifecyclePolicy.Checkpoint(roomCode: code, revision: revision, pausedAt: pausedAt)
        guard LifecyclePolicy.isPlausible(cp) else {
            clearCheckpoint()
            return nil
        }
        return cp
    }

    private static func clearCheckpoint() {
        UserDefaults.standard.removeObject(forKey: checkpointKey)
    }

    /// Always-on record of an actively/recently hosted room (L02A blocker
    /// 4), separate from `Checkpoint`: `Checkpoint` is written only on
    /// backgrounding, so a kill while still in the foreground (actively
    /// playing, screen on) would otherwise leave nothing on the next cold
    /// launch to explain the gap. Written at creation, updated on every
    /// accepted action/resume, cleared only after a confirmed end.
    private static func saveHostMarker(_ m: LifecyclePolicy.HostMarker) {
        let dict: [String: Any] = ["roomCode": m.roomCode, "revision": m.revision, "state": m.state.rawValue]
        UserDefaults.standard.set(dict, forKey: hostMarkerKey)
    }

    private static func loadHostMarker() -> LifecyclePolicy.HostMarker? {
        guard let dict = UserDefaults.standard.dictionary(forKey: hostMarkerKey),
              let code = dict["roomCode"] as? String,
              let revision = dict["revision"] as? Int,
              let stateRaw = dict["state"] as? String,
              let state = LifecyclePolicy.HostMarker.State(rawValue: stateRaw) else {
            return nil
        }
        let marker = LifecyclePolicy.HostMarker(roomCode: code, revision: revision, state: state)
        guard LifecyclePolicy.isPlausible(marker) else {
            clearHostMarker()
            return nil
        }
        return marker
    }

    private static func clearHostMarker() {
        UserDefaults.standard.removeObject(forKey: hostMarkerKey)
    }
}
