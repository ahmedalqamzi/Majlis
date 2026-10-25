import SwiftUI
import CoreImage.CIFilterBuiltins
import UIKit

// Native first-pair operator screen (L02A item 6): mode pick, host, QR +
// full address, round/lifecycle controls, player list with scores, explicit
// pause/resume, and now the full private operator controls for Who Am I and
// Majlis Feud — all through PartyLifecycleCoordinator's in-process bridge,
// never a second network hop. This mirrors the *operator's* slice of
// party/web/js/host.js (lines ~279-400 for the per-mode panels), not the
// player surface: per party/core/majlis-core.js's reduceRoom, only
// ROOM_ACTIONS (room.set_mode/start/pause/resume/end/reset/close,
// round.next) and the per-mode operator-only actions below are ever
// operator-submitted — player guesses/buzzes are player-submitted over the
// player HTTP surface this view never touches.
//
// Mode selection (L02A blocker 1): RoomManager.createRoom ignores any mode
// in the create body, so hosting always starts in the lobby with no mode
// selected; the operator must explicitly room.set_mode before room.start is
// accepted (reduceRoom rejects room.start with mode_not_selected otherwise).
//
// Not checkable with standalone swiftc in this sandbox (SwiftUI/UIKit; see
// PartyLifecycleCoordinator.swift's own note) — written and reasoned from
// the real wire shapes (party/core/majlis-core.js's projectRoom and the
// WhoAmI/Feud modules' own project() functions, already read this session)
// but not build-verified here. A genuine engine-exposed round/scoring
// "replay" or history feed was not found in party/core (projectRoom exposes
// only live phase/round/players/scores, no separate history log) — this
// view intentionally does not fabricate one; "replay" means a new
// round/game via round.next/room.reset/room.start.
@MainActor
struct OperatorView: View {
    @StateObject private var coordinator: PartyLifecycleCoordinator
    @State private var selectedMode = "who_am_i"
    @State private var state: [String: Any]?
    @State private var lastError: String?
    @State private var pollTimer: Timer?

    private static let modes = [("who_am_i", "Who Am I?"), ("feud", "Majlis Feud")]

    /// All mode-specific gameplay state (pendingGuesses, revealed,
    /// assignments, buzzer, control, answers, strikes, ...) sits nested
    /// under `game` in party/core/majlis-core.js's projectRoom; only
    /// mode/phase/roundId/players/playerLimits are top-level (L02A blocker 2
    /// — the panels below were reading these off `state` directly).
    private var game: [String: Any]? { state?["game"] as? [String: Any] }

    private func playerNames() -> [String: String] {
        var out: [String: String] = [:]
        for p in (state?["players"] as? [[String: Any]]) ?? [] {
            if let id = p["id"] as? String { out[id] = (p["name"] as? String) ?? id }
        }
        return out
    }

    init(makeHost: @escaping () throws -> PartyHost) {
        // No longer `try!`: PartyLifecycleCoordinator.init does not throw —
        // a failed makeHost() is reported as `.failed(...)` instead of
        // crashing the app on launch (L02A blocker 7).
        _coordinator = StateObject(wrappedValue: PartyLifecycleCoordinator(makeHost: makeHost))
    }

    var body: some View {
        VStack(spacing: 16) {
            switch coordinator.status {
            case .stopped, .failed:
                startScreen
            case .starting:
                ProgressView().tint(.white)
            case .reconnecting:
                // Unlike hostingScreen, this has no onAppear by default — a
                // tab return while already in .reconnecting (as opposed to a
                // real OS foreground/background transition, which the
                // top-level .onReceive below already handles) had no wired
                // route to coordinator.onForeground(), so the view was stuck
                // showing a spinner instead of reaching .pausedResumable/
                // .ended once the pause window closed (L02A blocker 7).
                ProgressView().tint(.white)
                    .onAppear { coordinator.onViewVisible() }
            case .hosting(let host, let port, let code):
                hostingScreen(host: host, port: port, code: code)
            case .pausedResumable(let code):
                pausedScreen(code: code)
            case .resumeFailed(let code, let message):
                resumeFailedScreen(code: code, message: message)
            case .ended(let reason):
                endedScreen(reason: reason)
            }
            if let lastError {
                Text(lastError).foregroundColor(.red).font(.footnote)
            }
        }
        .padding()
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didEnterBackgroundNotification)) { _ in
            coordinator.onBackground()
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)) { _ in
            coordinator.onForeground()
        }
    }

    private var startScreen: some View {
        VStack(spacing: 12) {
            if case .failed(let msg) = coordinator.status {
                Text("Could not start: \(msg)").foregroundColor(.red)
            }
            // Mode is picked in the lobby once the room exists (see
            // roomControls' "lobby" case below), not here: createRoom
            // ignores any mode passed in the create body (blocker 1).
            Button("Host a Room") {
                lastError = nil
                coordinator.startHosting(body: [:])
            }
            .buttonStyle(.borderedProminent)
        }
    }

    private func hostingScreen(host: String, port: UInt16, code: String) -> some View {
        let address = "http://\(host):\(port)/play.html?code=\(code)"
        // Scrollable: the per-mode panels below (whoAmIPanel/feudPanel) can
        // grow past the screen height once a room has several players and
        // pending guesses/answers (L02A blocker 2 — "missing
        // scroll/reachability verification for the large VStack").
        return ScrollView { VStack(spacing: 12) {
            Text(code).font(.system(size: 44, weight: .bold, design: .monospaced))
            if let qr = Self.qrImage(from: address) {
                Image(uiImage: qr).interpolation(.none).resizable()
                    .frame(width: 180, height: 180)
            }
            Text(address).font(.footnote).textSelection(.enabled)

            if let phase = state?["phase"] as? String {
                Text("Phase: \(phase)")
                // Round identity is party/core's own String id (e.g.
                // "g1r1"/"g1b1"), never numeric — a stale Int cast here
                // silently dropped every round label (L02A blocker 3).
                if let roundId = state?["roundId"] as? String { Text("Round: \(roundId)") }
            }

            if let players = state?["players"] as? [[String: Any]], !players.isEmpty {
                List(players.indices, id: \.self) { i in
                    let p = players[i]
                    HStack {
                        Text((p["name"] as? String) ?? "?")
                        if (p["connected"] as? Bool) != true { Text("(offline)").foregroundColor(.secondary) }
                        Spacer()
                        Text("\(p["score"] as? Int ?? 0)")
                    }
                }
                .frame(maxHeight: 160)
            }

            // room.set_mode is accepted by the reducer from "lobby" OR
            // "ended" (reduceRoom has no phase guard on it besides those
            // two) — restricting this to "lobby" left no way to pick a mode
            // again after room.end (L02A blocker 2).
            let currentPhase = state?["phase"] as? String
            if currentPhase == nil || currentPhase == "lobby" || currentPhase == "ended" {
                modePicker()
            }
            modePanel(phase: currentPhase)
            roomControls(phase: currentPhase)
        } }
        .onAppear {
            startPolling()
            coordinator.onViewVisible()
        }
        .onDisappear {
            stopPolling()
            coordinator.onViewHidden()
        }
    }

    /// The private operator affordances that ROOM_ACTIONS alone can't reach
    /// (L02A blocker 2): per-mode panels ported from party/web/js/host.js's
    /// whoAmIPanel/feudPanel. Rendered only while actually playing a round;
    /// lobby/paused/ended show nothing here (roomControls covers those).
    private func modePanel(phase: String?) -> some View {
        Group {
            // party/core never produces a room-level phase called
            // "round_end" (that string only ever appears as a per-mode
            // roundPhase nested under `game`) — the old check was dead code
            // that hid these panels for the entire "paused" phase too, which
            // is exactly when an operator most needs to see pending
            // guesses/the buzzer before resuming (L02A blocker 2).
            if phase == "playing" || phase == "paused" {
                switch state?["mode"] as? String {
                case "who_am_i": whoAmIPanel
                case "feud": feudPanel
                default: EmptyView()
                }
            }
        }
    }

    private var whoAmIPanel: some View {
        // All of the following were read straight off the top-level `state`
        // dict before, but party/core/majlis-core.js's WhoAmI project()
        // nests every one of them under `game`, and types pendingGuesses /
        // revealed as arrays of player-id strings, not a dict list / Bool
        // (L02A blocker 2).
        let roundId = state?["roundId"] as? String
        let roundPhase = game?["roundPhase"] as? String
        let pendingIds = (game?["pendingGuesses"] as? [String]) ?? []
        let guesses = (game?["guesses"] as? [String: String]) ?? [:]
        let assignments = (game?["assignments"] as? [String: [String: Any]]) ?? [:]
        let names = playerNames()
        return VStack(alignment: .leading, spacing: 8) {
            Text("Who Am I — round \(roundId ?? "?")").font(.headline)
            if !pendingIds.isEmpty {
                Text("Pending guesses").font(.subheadline)
                ForEach(pendingIds, id: \.self) { playerId in
                    HStack {
                        Text(names[playerId] ?? playerId)
                        if let g = guesses[playerId] { Text("\u{201c}\(g)\u{201d}").foregroundColor(.secondary) }
                        Spacer()
                        Button("Award") { send("whoami.award", payload: ["playerId": playerId], roundId: roundId) }
                            .buttonStyle(.borderedProminent)
                        Button("Dismiss") { send("whoami.dismiss", payload: ["playerId": playerId], roundId: roundId) }
                            .buttonStyle(.bordered)
                    }
                }
            }
            if roundPhase != "revealed" {
                Button("Reveal All") { send("whoami.reveal", roundId: roundId) }
                    .buttonStyle(.bordered)
            }
            if !assignments.isEmpty {
                Text("Assignments").font(.subheadline)
                ForEach(assignments.keys.sorted(), id: \.self) { playerId in
                    if let a = assignments[playerId] {
                        HStack {
                            Text(names[playerId] ?? playerId)
                            Spacer()
                            Text((a["label"] as? String) ?? (a["ar"] as? String) ?? "?")
                        }.font(.footnote)
                    }
                }
            }
            Button("Next Round") { send("round.next", roundId: roundId) }
                .buttonStyle(.bordered)
        }
    }

    private var feudPanel: some View {
        // Same top-level-vs-`game` bug as whoAmIPanel above: answers,
        // buzzer, strikes, maxStrikes, control and question all live under
        // `game` in Feud's project(), not at the top level (L02A blocker 2).
        let roundId = state?["roundId"] as? String
        let roundPhase = game?["roundPhase"] as? String
        let answers = (game?["answers"] as? [[String: Any]]) ?? []
        let buzzer = (game?["buzzer"] as? [String: Any]) ?? [:]
        let buzzerWinner = buzzer["winner"] as? String
        let strikes = (game?["strikes"] as? Int) ?? 0
        let maxStrikes = (game?["maxStrikes"] as? Int) ?? 3
        let control = game?["control"] as? String
        // party/core/majlis-core.js's Feud project() exposes this as
        // `roundPoints` (awarded total once a target is chosen, else the
        // live sum of revealed answers) — the operator panel never showed
        // it (L02A blocker 2).
        let roundPoints = (game?["roundPoints"] as? Int) ?? 0
        let names = playerNames()
        return VStack(alignment: .leading, spacing: 8) {
            Text("Feud — round \(roundId ?? "?")").font(.headline)
            if let question = game?["question"] as? String { Text(question) }
            Text("Strikes: \(strikes)/\(maxStrikes)").font(.subheadline)
            Text("Round points: \(roundPoints)").font(.subheadline)

            ForEach(answers.indices, id: \.self) { i in
                let a = answers[i]
                let answerId = a["id"] as? String
                let isRevealed = (a["revealed"] as? Bool) ?? false
                HStack {
                    Text((a["text"] as? String) ?? "?")
                    Spacer()
                    Text("\(a["points"] as? Int ?? 0)")
                    if !isRevealed, let answerId {
                        Button("Reveal") { send("feud.reveal", payload: ["answerId": answerId], roundId: roundId) }
                            .buttonStyle(.bordered)
                    }
                }
            }

            HStack {
                if roundPhase == "buzzer_open" {
                    Text("Buzzer open").foregroundColor(.green)
                } else if let buzzerWinner {
                    Text("Buzzed: \(names[buzzerWinner] ?? buzzerWinner)")
                }
                Spacer()
                // feud.open_buzzer is only accepted from roundPhase
                // "closed"; feud.reset_buzzer is rejected ("round_awarded")
                // only once roundPhase is "awarded" — deriving the button
                // from buzzer.open alone (old code) left it stuck on "Open
                // Buzzer" after a winner/control was already set, and let
                // "Reset Buzzer" be sent (and rejected) once a board was
                // already awarded (L02A blocker 2).
                if roundPhase == "closed" {
                    Button("Open Buzzer") { send("feud.open_buzzer", roundId: roundId) }
                        .buttonStyle(.bordered)
                } else if roundPhase != "awarded" {
                    Button("Reset Buzzer") { send("feud.reset_buzzer", roundId: roundId) }
                        .buttonStyle(.bordered)
                }
            }

            HStack {
                Button("Strike") { send("feud.strike", roundId: roundId) }
                    .buttonStyle(.bordered)
                    .disabled(strikes >= maxStrikes || !(roundPhase == "answering" || roundPhase == "steal"))
                // feud.steal requires a target different from the current
                // control-holder (reducer: target !== state.control, else
                // "invalid_payload") — the old code re-sent `control` itself
                // as the steal target, which the reducer could only ever
                // reject (L02A blocker 2).
                if strikes >= maxStrikes, roundPhase == "answering" {
                    ForEach((state?["players"] as? [[String: Any]])?.compactMap { $0["id"] as? String } ?? [], id: \.self) { playerId in
                        if playerId != control {
                            Button("Steal: \(names[playerId] ?? playerId)") {
                                send("feud.steal", payload: ["playerId": playerId], roundId: roundId)
                            }
                            .buttonStyle(.bordered)
                        }
                    }
                }
            }

            if roundPhase != "awarded", let players = state?["players"] as? [[String: Any]], !players.isEmpty {
                Text("Award points").font(.subheadline)
                ForEach(players.indices, id: \.self) { i in
                    if let playerId = players[i]["id"] as? String {
                        Button((players[i]["name"] as? String) ?? playerId) {
                            send("feud.award", payload: ["playerId": playerId], roundId: roundId)
                        }
                        .buttonStyle(.bordered)
                    }
                }
            }

            Button("Next Round") { send("round.next", roundId: roundId) }
                .buttonStyle(.bordered)
        }
    }

    private func roomControls(phase: String?) -> some View {
        HStack(spacing: 10) {
            switch phase {
            case "lobby", nil:
                // Start is gated on an explicit mode and the mode's own
                // minimum player count (lobby()'s own gate in
                // party/web/js/host.js) — never enabled on an unselected
                // mode (blocker 1).
                let min = (state?["playerLimits"] as? [String: Any])?["min"] as? Int ?? 3
                let count = (state?["players"] as? [[String: Any]])?.count ?? 0
                let mode = state?["mode"] as? String
                Button("Start") { send("room.start") }
                    .disabled(mode == nil || count < min)
            case "playing":
                Button("Pause") { send("room.pause") }
            case "paused":
                Button("Resume") { send("room.resume") }
            case "ended":
                // reduceRoom's ROOM_ACTIONS gating accepts room.reset and
                // room.close unconditionally from any phase, including
                // "ended" — but the old switch had no "ended" case at all,
                // so after room.end the operator was stuck with no way to
                // reset or close the room (L02A blocker 2).
                Button("Play Again") { send("room.reset") }
                    .buttonStyle(.borderedProminent)
                Button("Close Room", role: .destructive) { send("room.close") }
            case "closed":
                // room.close leaves the engine's own room/token state
                // terminal (same as "ended" for ROOM_ACTIONS purposes), but
                // the coordinator's own status stays `.hosting` until
                // endSession is actually called — the old switch had no
                // "closed" case, so the operator was left looking at a dead
                // room with zero controls after closing it (L02A blocker 2:
                // "closed phase must offer new-room/exit, not leave the
                // coordinator stuck hosting").
                Button("Host a New Room") { coordinator.endSession(reason: "The room was closed.") }
                    .buttonStyle(.borderedProminent)
            default:
                EmptyView()
            }
            if phase != "ended" && phase != "closed" {
                Button("End", role: .destructive) { send("room.end") }
            }
        }
    }

    /// Lobby-only mode picker (blocker 1): room.set_mode must be submitted
    /// and its accepted revision observed before Start can ever be enabled
    /// — createRoom never selects a mode itself.
    private func modePicker() -> some View {
        VStack(spacing: 8) {
            // JSONSerialization decodes a JSON null as NSNull, not Swift
            // nil, so `state?["mode"] == nil` was always false once the
            // server had answered even one state request (mode starts out
            // JSON null, not an absent key) — the mode picker/Set Mode
            // button could never appear (L02A blocker 1).
            if (state?["mode"] as? String) == nil {
                Picker("Mode", selection: $selectedMode) {
                    ForEach(Self.modes, id: \.0) { mode in Text(mode.1).tag(mode.0) }
                }
                .pickerStyle(.segmented)
                Button("Set Mode") { send("room.set_mode", payload: ["mode": selectedMode]) }
                    .buttonStyle(.borderedProminent)
            } else if let mode = state?["mode"] as? String {
                Text("Mode: \(Self.modes.first(where: { $0.0 == mode })?.1 ?? mode)")
            }
        }
    }

    private func pausedScreen(code: String) -> some View {
        VStack(spacing: 12) {
            Text("Paused — room \(code)").font(.headline)
            Text("Resume to rebind and tell the room to continue.")
            Button("Resume Hosting") { coordinator.resumeHosting() }
                .buttonStyle(.borderedProminent)
        }
    }

    /// Resume failed (L02A blocker 5): the checkpoint for `code` is still
    /// intact, so this is a retry point, not a dead end — distinct from the
    /// `.failed`/startScreen path, which can only start a brand-new room.
    private func resumeFailedScreen(code: String, message: String) -> some View {
        VStack(spacing: 12) {
            Text("Could not resume room \(code)").font(.headline).foregroundColor(.red)
            Text(message).font(.footnote).foregroundColor(.secondary)
            Button("Retry") { coordinator.resumeHosting() }
                .buttonStyle(.borderedProminent)
            Button("End", role: .destructive) {
                coordinator.endSession(reason: "Room \(code) ended.")
            }
            .buttonStyle(.bordered)
        }
    }

    private func endedScreen(reason: String) -> some View {
        VStack(spacing: 12) {
            Text(reason).multilineTextAlignment(.center)
            // startHosting(body:) already builds a fresh PartyHost when
            // status is .ended (Server.shutdown is terminal — see
            // PartyLifecycleCoordinator.init's doc comment) — this is the
            // same action as the start screen's button, not a separate path.
            Button("Host a New Room") {
                lastError = nil
                coordinator.startHosting(body: [:])
            }
            .buttonStyle(.bordered)
        }
    }

    /// `roundId` binds the action to the round the operator was actually
    /// looking at; party/core's own stale-action protection then rejects it
    /// if the room has moved on since (mirrors party/web/js/host.js's own
    /// `act(type, payload, btn, { round })`).
    private func send(_ type: String, payload: [String: Any] = [:], roundId: String? = nil) {
        coordinator.submitAction(type: type, payload: payload, roundId: roundId) { result in
            DispatchQueue.main.async {
                switch result {
                case .success(let r) where r.accepted:
                    lastError = nil
                case .success(let r):
                    lastError = r.error ?? "rejected"
                case .failure(let e):
                    lastError = String(describing: e)
                }
            }
        }
    }

    private func startPolling() {
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            Task { @MainActor in
                coordinator.fetchState { result in
                    DispatchQueue.main.async {
                        if case .success(let s) = result { state = s }
                    }
                }
            }
        }
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private static func qrImage(from string: String) -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(string.utf8)
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
        guard let cgImage = CIContext().createCGImage(scaled, from: scaled.extent) else { return nil }
        return UIImage(cgImage: cgImage)
    }
}
