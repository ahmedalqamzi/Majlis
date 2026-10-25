import Foundation
import Network

// Constructs and owns the in-process party engine/server for iOS, replacing
// the macOS CLI's stdin operator bridge (party/native/Sources/main.swift,
// `operatorLine`/`startStdin`) with direct Swift calls into
// `engine.call("operatorCall", ...)`. The reviewed adapter in
// party/native/majlis-native.js is reused unchanged: `operatorCall` already
// defines the exact JSON request ({"op":...,"code":...,"body":...}) and
// plain-JSON-string reply shape the stdin bridge used; this class builds and
// parses that same shape in Swift instead of framing it as stdin lines.
//
// Not checkable with standalone swiftc (it drives NWListener/Engine; see
// majlis-ios/Majlis/Party/LifecyclePolicy.swift and InterfaceAddress.swift
// for the parts of this feature that are). Requires Xcode/iOS build to
// compile and a physical device to run (see L02A report).
final class PartyHost {
    enum HostError: Error {
        case engineFailed
        case noWiFi
        case bindFailed(String)
        case invalidConfig(String)
        case internalError
    }

    private let queue = DispatchQueue(label: "majlis.engine.ios")
    private let engine: Engine
    private let server: Server
    private(set) var boundHost: String?
    private(set) var boundPort: UInt16?

    // Phone-sized limits: the Mac CLI defaults (Limits(), e.g.
    // maxConnections 320) assume a desktop serving many LAN clients; a
    // party room is a handful of phones on the same Wi-Fi, so the caps are
    // tightened to match a phone's actual resource budget rather than
    // reusing desktop defaults verbatim.
    static func phoneLimits() -> Limits {
        var l = Limits()
        l.maxConnections = 32
        l.requestRate = 15.0
        l.requestBurst = 60.0
        return l
    }

    init(contentDir: String, webDir: String, limits: Limits = PartyHost.phoneLimits()) throws {
        // Built as a local binding, not `self.engine`, and used only by
        // plain local closures below (capturing `localEngine`/`packBytes`,
        // never `self`): referencing `self.engine` here — even implicitly,
        // through a closure — would be a definite-initialization error,
        // since `self.server` isn't set yet. Both stored properties are
        // assigned together at the very end of this initializer instead.
        let localEngine: Engine
        do {
            localEngine = try Engine(sources: EmbeddedSources.all)
        } catch {
            throw HostError.engineFailed
        }

        // Fail loudly, not silently: a missing/unreadable content pack used
        // to fall back to a JS `null` entry, which the brief (review item 5)
        // flagged as a way to silently ship a mode with no content.
        let implementedModes = ["who_am_i", "feud", "impostor"]
        var packBytes: [String: [UInt8]] = [:]
        for mode in implementedModes {
            let path = (contentDir as NSString).appendingPathComponent(mode + ".json")
            guard let data = FileManager.default.contents(atPath: path) else {
                throw HostError.invalidConfig("missing or unreadable content pack: \(mode).json")
            }
            packBytes[mode] = [UInt8](data)
        }
        let packs = localEngine.object(implementedModes.map { mode in (mode, localEngine.bytes(packBytes[mode]!)) })

        let initResult = localEngine.call("init", [localEngine.string(Surface.production.rawValue), localEngine.object([]), packs])
        guard initResult?.objectForKeyedSubscript("ok")?.toBool() == true else {
            throw HostError.invalidConfig(initResult?.objectForKeyedSubscript("detail")?.toString() ?? "invalid config")
        }

        // Fail loudly here too: `try?` used to leave `web = nil` (silently
        // serving no static files at all) on any StaticRoot error, e.g. a
        // missing bundled resource directory.
        let web: StaticRoot
        do {
            web = try StaticRoot(directory: webDir, files: StaticRoot.productionFiles)
        } catch let e as ConfigError {
            throw HostError.invalidConfig("web resources: \(e.detail)")
        }

        engine = localEngine
        server = Server(queue: queue, engine: localEngine, surface: .production, limits: limits, web: web)
    }

    /// Binds to the phone's current Wi-Fi address on an ephemeral port (0),
    /// mirroring main.swift's bind sequence: set allowedOrigins/allowedHosts
    /// from the bound host/port exactly once bound (the actual port is only
    /// known after the OS assigns it), no 0.0.0.0/loopback origins since this
    /// is always a concrete Wi-Fi address on iOS, never --allow-lan's
    /// desktop-only "bind anything" path.
    func start(completion: @escaping (Result<(host: String, port: UInt16), HostError>) -> Void) {
        guard let ip = InterfaceAddress.currentWiFiIPv4() else {
            completion(.failure(.noWiFi))
            return
        }
        guard let addr = IPv4Address(ip) else {
            completion(.failure(.invalidConfig("current Wi-Fi address is not a valid IPv4 address")))
            return
        }
        queue.async { [self] in
            server.log = { line in NSLog("majlis-party: %@", line) }
            server.start(host: addr, port: 0) { result in
                switch result {
                case .failure(let err):
                    var detail = "bind failed"
                    if case let .posix(code) = err { detail = String(cString: strerror(code.rawValue)) }
                    completion(.failure(.bindFailed(detail)))
                case .success(let port):
                    let origin = "http://\(ip):\(port)"
                    self.server.allowedOrigins = [origin]
                    self.server.allowedHosts = ["\(ip):\(port)"]
                    self.boundHost = ip
                    self.boundPort = port
                    completion(.success((ip, port)))
                }
            }
        }
    }

    /// Rebinds after PartyHost survives a background pause (LifecyclePolicy
    /// `.rebind`): the Wi-Fi address may have changed while backgrounded
    /// (brief: "regenerate address/QR after interface changes"), so this is
    /// `start` again, not a no-op resume. Uses `Server.pause`, not
    /// `Server.shutdown`: shutdown is terminal (it flips party/core's
    /// RoomManager.stopped permanently, with no way back — confirmed by
    /// Skull's rebind-probe, build.log/result-init-only.log), so reusing the
    /// same Server after a real shutdown leaves the bind callback and
    /// operator/state snapshot looking fine while actual HTTP serving is
    /// broken (curl 000/exit 56). pause() only tears down the listener and
    /// current connections; room/identity/revision are untouched because the
    /// engine itself is never told to shut down.
    func rebind(completion: @escaping (Result<(host: String, port: UInt16), HostError>) -> Void) {
        queue.async { [self] in
            server.pause {
                // Only un-freeze the clock once this rebind attempt has
                // actually succeeded. Calling resumeClock() before start()
                // could fail (used to run before server.pause() here) let a
                // failing retry's own wait count against the room/token TTL
                // it was trying to survive (L02A blocker 5) — a retry that
                // never serves a single request must cost zero elapsed time.
                // pause()/resume() are idempotent/no-ops when already in the
                // matching state, so retrying this same rebind() again after
                // a failure (still paused) is always safe.
                self.start { result in
                    if case .success = result {
                        self.server.resumeClock()
                    }
                    completion(result)
                }
            }
        }
    }

    func stop(completion: @escaping () -> Void) {
        queue.async {
            self.server.resetClock()
            self.server.shutdown(completion)
        }
    }

    /// Suspends network serving only — no game-logic change, no listener
    /// recreated afterward (unlike rebind, which pauses then immediately
    /// starts again). Used when the app backgrounds: PartyLifecycleCoordinator
    /// calls this after (if the room was playing) pausing the room itself via
    /// submitAction(type: "room.pause"), so both serving and game time are
    /// actually stopped while backgrounded, not just a persisted checkpoint
    /// with the server still answering requests underneath it.
    func suspendNetworking(completion: @escaping () -> Void) {
        queue.async { [self] in
            server.pauseClock()
            server.pause(completion)
        }
    }

    func createRoom(body: [String: Any], completion: @escaping (Result<(code: String, revision: Int), HostError>) -> Void) {
        call(op: "create", code: nil, body: body) { result in
            switch result {
            case .failure(let e):
                completion(.failure(e))
            case .success(let json):
                guard let obj = json as? [String: Any], obj["ok"] as? Bool == true,
                      let code = obj["code"] as? String, let revision = obj["revision"] as? Int else {
                    completion(.failure(.internalError))
                    return
                }
                completion(.success((code, revision)))
            }
        }
    }

    func action(code: String, body: [String: Any], completion: @escaping (Result<[String: Any], HostError>) -> Void) {
        call(op: "action", code: code, body: body) { result in
            completion(result.flatMap { json in
                guard let obj = json as? [String: Any], obj["ok"] as? Bool == true else {
                    return .failure(.internalError)
                }
                return .success(obj)
            })
        }
    }

    /// Builds and sends one operator action with the exact wire envelope
    /// party/core/majlis-core.js's reduceRoom/submitAction and the browser
    /// transport (party/web/js/transport.js `act()`) both use:
    /// {"actionId","type","payload","roundId"?}. `action(code:body:)` above
    /// is the raw primitive; this is the one callers (lifecycle, operator UI)
    /// should actually use so every call site generates a fresh actionId and
    /// parses the {"accepted","revision","error"} result the same way,
    /// instead of each caller re-deriving that shape by hand.
    /// `roundId` is party/core's own round identity (party/core/majlis-core.js
    /// `state.roundId`, e.g. "g1r1"/"g1b1") and must stay a String end to
    /// end: `parseAction`/`ROUND_ID_RE` require a string and reject anything
    /// else outright, so an Int here was never "a round id that happens to
    /// be numeric" — it was a shape the engine cannot parse at all (L02A
    /// blocker 3).
    func submitAction(code: String, type: String, payload: [String: Any] = [:], roundId: String? = nil,
                       completion: @escaping (Result<(accepted: Bool, revision: Int, error: String?), HostError>) -> Void) {
        var body: [String: Any] = ["actionId": UUID().uuidString, "type": type, "payload": payload]
        if let roundId = roundId { body["roundId"] = roundId }
        action(code: code, body: body) { result in
            completion(result.flatMap { obj in
                guard let status = obj["status"] as? Int, let res = obj["result"] as? [String: Any],
                      let accepted = res["accepted"] as? Bool, let revision = res["revision"] as? Int else {
                    return .failure(.internalError)
                }
                _ = status
                let errCode = (res["error"] as? [String: Any])?["code"] as? String
                return .success((accepted, revision, errCode))
            })
        }
    }

    func state(code: String, completion: @escaping (Result<[String: Any], HostError>) -> Void) {
        call(op: "state", code: code, body: [:]) { result in
            completion(result.flatMap { json in
                guard let obj = json as? [String: Any], obj["ok"] as? Bool == true,
                      let state = obj["state"] as? [String: Any] else {
                    return .failure(.internalError)
                }
                return .success(state)
            })
        }
    }

    /// Drops the host token for a room (session end / revoke on kill-cleanup).
    /// `code: nil` matches the stdin bridge's forget-all behavior.
    func forget(code: String?, completion: @escaping (Result<Void, HostError>) -> Void) {
        call(op: "forget", code: code, body: [:]) { result in
            completion(result.flatMap { _ in .success(()) })
        }
    }

    private func call(op: String, code: String?, body: [String: Any],
                       completion: @escaping (Result<Any, HostError>) -> Void) {
        var req: [String: Any] = ["op": op, "body": body]
        if let code = code { req["code"] = code }
        guard let reqBytes = try? JSONSerialization.data(withJSONObject: req) else {
            completion(.failure(.internalError))
            return
        }
        queue.async { [self] in
            guard var pool = SecureRandom.draw(SecureRandom.operatorCall) else {
                completion(.failure(.internalError))
                return
            }
            let now = server.clock.now()
            let reply = engine.call("operatorCall", [engine.bytes([UInt8](reqBytes)), engine.number(now), engine.bytes(pool)])
            SecureRandom.wipe(&pool)
            server.flushDiagnostics()
            server.schedulePumpAll()
            guard let text = reply?.toString(), let data = text.data(using: .utf8),
                  let json = try? JSONSerialization.jsonObject(with: data) else {
                completion(.failure(.internalError))
                return
            }
            completion(.success(json))
        }
    }
}
