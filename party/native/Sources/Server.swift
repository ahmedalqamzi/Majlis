// Network.framework HTTP + SSE transport around the JSC engine.
//
// Everything (listener, connections, timers, engine calls, the operator
// bridge) runs on one serial queue, so the engine sees a total order of
// calls and needs no locks. One request per connection; every response
// closes it. Route order, checks and error codes follow party/server/httpd.py
// (`_handle`); deviations are listed in party/native/README.md.

import Foundation
import JavaScriptCore
import Network

enum Surface: String {
    /// LAN-capable. Players only from the network; no POST /api/rooms; host
    /// and display capabilities exist only behind the in-process operator bridge.
    case production
    /// Full reference wire API for the reference suites. Loopback binds only,
    /// and only compiled with -D MAJLIS_TEST_LOOPBACK.
    case testLoopback = "test-loopback"
}

struct Limits {
    var requestTimeout = 15.0
    var sseWriteTimeout = 10.0
    var sweepInterval = 30.0
    var maxBodyBytes = 16384
    var maxConnections = 320
    var requestRate = 30.0, requestBurst = 120.0
    var createRate = 10.0 / 60.0, createBurst = 10.0
    var joinRate = 60.0 / 60.0, joinBurst = 30.0
    var actionRate = 10.0, actionBurst = 30.0
    var accessLog = false
}

final class Conn {
    enum Phase { case head, body, responding, linger, sse, done }

    let id: Int
    let nw: NWConnection
    let ip: String
    var phase = Phase.head
    var buf: [UInt8] = []
    var req: Request?
    var bodyNeed = 0
    var bodyThen: (([UInt8]) -> Void)?
    var dispatched = false
    var method: String?
    var route = "unmatched"
    var status: Int?
    var logged = false
    var peerEOF = false
    var extraBytes = 0
    var deadline: DispatchSourceTimer?
    // SSE
    var streamId: Int?
    var sending = false
    var waitTimer: DispatchSourceTimer?
    var writeTimer: DispatchSourceTimer?

    init(id: Int, nw: NWConnection, ip: String) {
        self.id = id
        self.nw = nw
        self.ip = ip
    }
}

private struct Fail: Error {
    let code: String
    let retry: Double?
    init(_ code: String, _ retry: Double? = nil) {
        self.code = code
        self.retry = retry
    }
}

final class Server {
    static let maxDrainBytes = 1024 * 1024
    static let lingerSeconds = 2.0

    let queue: DispatchQueue
    let engine: Engine
    let surface: Surface
    let limits: Limits
    let web: StaticRoot?
    var clock = MonotonicClock()
    /// Real (unfrozen) reading clock.now was constructed with, captured in
    /// init before clock.now is overridden below — pauseClock/resumeClock
    /// read real time through this, never through clock.now itself (which
    /// is the gated value every engine call sees).
    private let rawClockNow: () -> Double
    private var clockGate = ClockGate()
    var allowedOrigins = Set<String>()
    var allowedHosts = Set<String>()
    /// stderr line writer (access log and class-name-only internal errors).
    var log: (String) -> Void = { _ in }

    private var listener: NWListener?
    private var conns: [Int: Conn] = [:]
    private var streams: [Int: Conn] = [:]
    private var nextId = 1
    private var pumpScheduled = false
    private var sweepTimer: DispatchSourceTimer?
    private(set) var stopping = false
    /// Bound on simultaneous over-capacity 503 replies (accept(), below): these
    /// connections sit outside `conns` for up to a second, so without their own
    /// cap they are an unbounded resource outside the documented connection limit.
    private var busyReplies = 0
    private let maxBusyReplies = 64

    private let requestLimiter: RateLimiter
    private let createLimiter: RateLimiter
    private let joinLimiter: RateLimiter
    private let actionLimiter: RateLimiter

    init(queue: DispatchQueue, engine: Engine, surface: Surface, limits: Limits, web: StaticRoot?) {
        self.queue = queue
        self.engine = engine
        self.surface = surface
        self.limits = limits
        self.web = web
        requestLimiter = RateLimiter(rate: limits.requestRate, burst: limits.requestBurst)
        createLimiter = RateLimiter(rate: limits.createRate, burst: limits.createBurst)
        joinLimiter = RateLimiter(rate: limits.joinRate, burst: limits.joinBurst)
        actionLimiter = RateLimiter(rate: limits.actionRate, burst: limits.actionBurst)
        let raw = clock.now
        rawClockNow = raw
        clock.now = { [weak self] in self.map { $0.clockGate.apply(raw()) } ?? raw() }
    }

    var connectionCount: Int { conns.count }
    var streamCount: Int { streams.count }

    // --------------------------------------------------------------- clock gate

    /// Freezes clock.now at its current (gated) value for the duration of a
    /// host-declared pause (L02A blocker 6). Like pause()/shutdown() above,
    /// assumes the caller is already running on `queue` (PartyHost's
    /// suspendNetworking, strictly before server.pause() stops the queue
    /// pump), so the sweep/actionCommit/join paths above never observe the
    /// real backgrounded interval as elapsed room/token time.
    func pauseClock() {
        clockGate.pause(real: rawClockNow())
    }

    /// Un-freezes clock.now, permanently discounting exactly the real
    /// duration spent paused. Assumes the caller is on `queue` (PartyHost's
    /// rebind, strictly before server.pause{ start(...) } resumes serving)
    /// — so no engine call in between ever sees a partially-resumed clock.
    @discardableResult
    func resumeClock() -> TimeInterval {
        clockGate.resume(real: rawClockNow())
    }

    /// Discards any pending pause/offset. Called when a paused session ends
    /// instead of resuming (host.stop/forget), so a Server instance reused
    /// for a later room never inherits a stale freeze.
    func resetClock() {
        clockGate.reset()
    }

    // ------------------------------------------------------------ listener

    /// Binds host:port (IPv4 only) and reports the bound port or a POSIX error text.
    func start(host: IPv4Address, port: UInt16, done: @escaping (Result<UInt16, NWError>) -> Void) {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowLocalEndpointReuse = true
        params.includePeerToPeer = false
        if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options { ip.version = .v4 }
        params.requiredLocalEndpoint = .hostPort(host: .ipv4(host), port: NWEndpoint.Port(rawValue: port) ?? .any)
        let l: NWListener
        do {
            l = try NWListener(using: params)
        } catch let e as NWError {
            done(.failure(e))
            return
        } catch {
            done(.failure(.posix(.EINVAL)))
            return
        }
        listener = l
        var reported = false
        l.stateUpdateHandler = { [weak self, weak l] state in
            switch state {
            case .ready:
                if !reported, let p = l?.port {
                    reported = true
                    self?.startSweeper()
                    done(.success(p.rawValue))
                }
            case .failed(let e), .waiting(let e):
                if !reported {
                    reported = true
                    l?.cancel()
                    done(.failure(e))
                }
            default:
                break
            }
        }
        l.newConnectionHandler = { [weak self] c in self?.accept(c) }
        l.start(queue: queue)
    }

    private func startSweeper() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + limits.sweepInterval, repeating: limits.sweepInterval)
        t.setEventHandler { [weak self] in
            guard let self = self, !self.stopping else { return }
            _ = self.engine.call("sweep", [self.engine.number(self.clock.now())])
            self.flushDiagnostics()
            self.schedulePumpAll()
        }
        t.resume()
        sweepTimer = t
    }

    static func ipString(_ e: NWEndpoint) -> String {
        if case let .hostPort(host, _) = e {
            switch host {
            case .ipv4(let a): return a.rawValue.map { String($0) }.joined(separator: ".")
            case .ipv6(let a): return "\(a)"
            case .name(let n, _): return n
            @unknown default: return "-"
            }
        }
        return "-"
    }

    private func timer(_ seconds: Double, _ fire: @escaping () -> Void) -> DispatchSourceTimer {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + max(0.0, seconds))
        t.setEventHandler(handler: fire)
        t.resume()
        return t
    }

    // --------------------------------------------------------- connections

    private func accept(_ nw: NWConnection) {
        if stopping {
            nw.cancel()
            return
        }
        if conns.count >= limits.maxConnections {
            // httpd.process_request: raw 503 server_busy, never counted in `conns`.
            // That rejection work (socket + 1s linger timer + drain) is itself
            // unbounded unless capped here: past maxBusyReplies, cancel at once
            // with no reply rather than holding more transient state.
            if busyReplies >= maxBusyReplies {
                nw.cancel()
                return
            }
            busyReplies += 1
            var finished = false
            func finish() {
                if finished { return }
                finished = true
                busyReplies -= 1
            }
            let body = Reply(engine.call("errorFor", [engine.string("server_busy")]))?.body ?? []
            nw.start(queue: queue)
            nw.send(content: Data(HTTP.serverBusy(body)), contentContext: .finalMessage, isComplete: true,
                    completion: .contentProcessed { _ in })
            let t = timer(1.0) { nw.cancel() }
            nw.stateUpdateHandler = { s in
                if case .cancelled = s { t.cancel(); finish() }
                if case .failed = s { nw.cancel() }
            }
            drainOnly(nw)
            return
        }
        let c = Conn(id: nextId, nw: nw, ip: Server.ipString(nw.endpoint))
        nextId += 1
        conns[c.id] = c
        nw.stateUpdateHandler = { [weak self, weak c] s in
            guard let self = self, let c = c else { return }
            switch s {
            case .failed, .cancelled: self.finish(c, force: false)
            default: break
            }
        }
        nw.start(queue: queue)
        c.deadline = timer(limits.requestTimeout) { [weak self, weak c] in
            guard let self = self, let c = c else { return }
            self.onDeadline(c)
        }
        receive(c)
    }

    private func drainOnly(_ nw: NWConnection) {
        nw.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] _, _, done, err in
            if done || err != nil { nw.cancel() } else { self?.drainOnly(nw) }
        }
    }

    private func receive(_ c: Conn) {
        c.nw.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self = self, c.phase != .done else { return }
            if let d = data, !d.isEmpty { self.onData(c, d) }
            if c.phase == .done { return }
            if isComplete || error != nil {
                self.onEOF(c)
                return
            }
            self.receive(c)
        }
    }

    private func onData(_ c: Conn, _ d: Data) {
        switch c.phase {
        case .head:
            c.buf.append(contentsOf: d)
            switch HTTP.parse(c.buf) {
            case .incomplete:
                break
            case .reject(let status, let code):
                // BaseHTTPRequestHandler.send_error path: no access line.
                c.phase = .responding
                let body = Reply(engine.call("errorFor", [engine.string(code)]))?.body ?? []
                send(c, HTTP.json(status, body), status: status)
            case .request(let r):
                c.req = r
                c.method = r.method
                c.phase = .responding
                c.dispatched = true
                dispatch(c, r)
            }
        case .body:
            guard let r = c.req else { return }
            let room = r.bodyStart + c.bodyNeed - c.buf.count
            if room > 0 { c.buf.append(contentsOf: d.prefix(room)) }
            if c.buf.count >= r.bodyStart + c.bodyNeed {
                let then = c.bodyThen
                c.bodyThen = nil
                c.phase = .responding
                let body = Array(c.buf[r.bodyStart..<(r.bodyStart + c.bodyNeed)])
                then?(body)
            }
        case .responding, .linger, .sse:
            c.extraBytes += d.count
            if c.extraBytes > Server.maxDrainBytes { close(c, force: true) }
        case .done:
            break
        }
    }

    private func onEOF(_ c: Conn) {
        c.peerEOF = true
        switch c.phase {
        case .head, .linger:
            close(c, force: false)
        case .body:
            // httpd._read_json: a short body is bad_request.
            c.bodyThen = nil
            c.phase = .responding
            fail(c, Fail("bad_request"))
        case .responding:
            break  // the response completes, then the connection closes
        case .sse:
            close(c, force: false)  // immediate teardown (declared deviation)
        case .done:
            break
        }
    }

    private func onDeadline(_ c: Conn) {
        switch c.phase {
        case .head, .body, .responding, .linger:
            close(c, force: true)
        case .sse, .done:
            break
        }
    }

    private func restartDeadline(_ c: Conn, _ seconds: Double) {
        c.deadline?.cancel()
        c.deadline = timer(seconds) { [weak self, weak c] in
            guard let self = self, let c = c else { return }
            self.onDeadline(c)
        }
    }

    private func logAccess(_ c: Conn) {
        guard c.dispatched, !c.logged else { return }
        c.logged = true
        if limits.accessLog {
            log("access \(c.method ?? "-") \(c.route) \(c.status.map { String($0) } ?? "-")")
        }
    }

    func flushDiagnostics() {
        for name in engine.diagnostics() { log("internal_error \(name)") }
    }

    /// Writes a complete response, half-closes, then drains until the peer
    /// closes (bounded), so the peer is not reset before it reads the reply.
    private func send(_ c: Conn, _ bytes: [UInt8], status: Int) {
        c.status = status
        c.phase = .responding
        restartDeadline(c, limits.requestTimeout)
        c.nw.send(content: Data(bytes), contentContext: .finalMessage, isComplete: true,
                  completion: .contentProcessed { [weak self] err in
                      guard let self = self, c.phase != .done else { return }
                      self.logAccess(c)
                      if err != nil || c.peerEOF {
                          self.close(c, force: err != nil)
                          return
                      }
                      c.phase = .linger
                      self.restartDeadline(c, Server.lingerSeconds)
                  })
    }

    private func close(_ c: Conn, force: Bool) {
        if force { c.nw.forceCancel() } else { c.nw.cancel() }
        finish(c, force: force)
    }

    /// Idempotent teardown: stream bookkeeping, access line, timers, slot.
    private func finish(_ c: Conn, force: Bool) {
        guard c.phase != .done else { return }
        c.phase = .done
        if let id = c.streamId {
            c.streamId = nil
            _ = engine.call("streamClose", [engine.number(Double(id))])
            schedulePumpAll()
        }
        streams.removeValue(forKey: c.id)
        logAccess(c)
        c.deadline?.cancel()
        c.waitTimer?.cancel()
        c.writeTimer?.cancel()
        c.bodyThen = nil
        c.buf = []
        conns.removeValue(forKey: c.id)
        c.nw.cancel()
    }

    // ------------------------------------------------------------- routing

    private func fail(_ c: Conn, _ f: Fail) {
        guard let reply = Reply(engine.call("errorFor", [engine.string(f.code)])) else {
            close(c, force: true)
            return
        }
        sendReply(c, reply, extra: f.retry.map { [("Retry-After", retryAfterHeader($0))] } ?? [])
    }

    private func sendReply(_ c: Conn, _ r: Reply?, extra: [(String, String)] = []) {
        flushDiagnostics()
        guard let r = r, r.status >= 100 else {
            log("internal_error JSException")
            fail(c, Fail("internal_error"))
            return
        }
        send(c, HTTP.json(r.status, r.body, extra: extra, includeBody: c.method != "HEAD"), status: r.status)
    }

    private static func strip(_ s: String) -> String { s.trimmingCharacters(in: .whitespaces) }

    private func dispatch(_ c: Conn, _ r: Request) {
        do {
            try handle(c, r)
        } catch let f as Fail {
            fail(c, f)
        } catch {
            log("internal_error \(type(of: error))")
            fail(c, Fail("internal_error"))
        }
    }

    private func bearer(_ r: Request) -> (Int, String?) {
        let values = r.all("authorization")
        if values.isEmpty { return (0, nil) }
        if values.count > 1 { return (2, nil) }
        let v = Server.strip(values[0])
        let scheme: Substring, token: String
        if let sp = v.firstIndex(of: " ") {
            scheme = v[..<sp]
            token = Server.strip(String(v[v.index(after: sp)...]))
        } else {
            scheme = Substring(v)
            token = ""
        }
        if scheme.lowercased() != "bearer" || token.isEmpty { return (2, nil) }
        return (1, token)
    }

    private func handle(_ c: Conn, _ r: Request) throws {
        if r.method != "GET" && r.method != "POST" {
            c.route = "method-rejected"
            throw Fail("method_not_allowed")
        }
        let raw = r.target
        guard raw.hasPrefix("/"), !raw.hasPrefix("//") else { throw Fail("not_found") }
        let path: String, query: String, hasQuery: Bool
        if let q = raw.firstIndex(of: "?") {
            path = String(raw[..<q])
            query = String(raw[raw.index(after: q)...])
            hasQuery = true
        } else {
            path = raw
            query = ""
            hasQuery = false
        }
        // Host / Origin / Sec-Fetch-Site (DNS-rebinding and cross-site guard).
        let hosts = r.all("host")
        guard hosts.count == 1, allowedHosts.contains(Server.strip(hosts[0]).lowercased()) else {
            throw Fail("host_forbidden")
        }
        let origins = r.all("origin")
        if origins.count > 1 || (origins.count == 1 && !allowedOrigins.contains(origins[0])) {
            throw Fail("origin_forbidden")
        }
        if let site = r.first("sec-fetch-site"), !["same-origin", "none"].contains(Server.strip(site).lowercased()) {
            throw Fail("origin_forbidden")
        }
        let staticFile = web?.lookup(path)
        if hasQuery && !(staticFile != nil && StaticRoot.queryAllowed(path, query)) {
            c.route = "query-rejected"
            throw Fail("query_not_allowed")
        }
        let (ok, retry) = requestLimiter.allow(c.ip, now: clock.now())
        if !ok { throw Fail("rate_limited", retry) }
        let method = r.method

        if let (parts, type) = staticFile, let web = web {
            c.route = "static"
            if method != "GET" { throw Fail("method_not_allowed") }
            guard let data = web.read(parts) else { throw Fail("not_found") }
            send(c, HTTP.staticFile(data, type), status: 200)
            return
        }
        if path == "/api/health" {
            c.route = "/api/health"
            if method != "GET" { throw Fail("method_not_allowed") }
            sendReply(c, Reply(engine.call("health", [])))
            return
        }
        if path == "/api/modes" {
            c.route = "/api/modes"
            if method != "GET" { throw Fail("method_not_allowed") }
            sendReply(c, Reply(engine.call("modes", [])))
            return
        }
        if path == "/api/rooms" && surface == .testLoopback {
            c.route = "/api/rooms"
            if method != "POST" { throw Fail("method_not_allowed") }
            let (ok, retry) = createLimiter.allow(c.ip, now: clock.now())
            if !ok { throw Fail("rate_limited", retry) }
            try readJSON(c, r, allowEmpty: true) { [unowned self] body in
                guard var pool = SecureRandom.draw(SecureRandom.create) else { return self.fail(c, Fail("internal_error")) }
                let reply = Reply(self.engine.call("create", [self.bodyValue(body), self.engine.number(self.clock.now()),
                                                              self.engine.bytes(pool)]))
                SecureRandom.wipe(&pool)
                self.sendReply(c, reply)
            }
            return
        }
        guard let (code, leaf) = Server.roomRoute(path) else { throw Fail("not_found") }
        c.route = "/api/rooms/:code/" + leaf

        switch leaf {
        case "join":
            if method != "POST" { throw Fail("method_not_allowed") }
            let (ok, retry) = joinLimiter.allow(c.ip, now: clock.now())
            if !ok { throw Fail("rate_limited", retry) }
            try readJSON(c, r, allowEmpty: true) { [unowned self] body in
                let (b, token) = self.bearer(r)
                guard var pool = SecureRandom.draw(SecureRandom.join) else { return self.fail(c, Fail("internal_error")) }
                let reply = Reply(self.engine.call("join", [
                    self.engine.string(code), self.bodyValue(body), self.engine.number(Double(b)), self.engine.string(token),
                    self.engine.number(self.clock.now()), self.engine.bytes(pool),
                ]))
                SecureRandom.wipe(&pool)
                self.sendReply(c, reply)
                self.schedulePumpAll()
            }
        case "state":
            if method != "GET" { throw Fail("method_not_allowed") }
            let (b, token) = bearer(r)
            sendReply(c, Reply(engine.call("state", [engine.string(code), engine.number(Double(b)), engine.string(token),
                                                     engine.number(clock.now())])))
        case "events":
            if method != "GET" { throw Fail("method_not_allowed") }
            let (b, token) = bearer(r)
            guard let reply = Reply(engine.call("streamOpen", [engine.string(code), engine.number(Double(b)),
                                                               engine.string(token), engine.number(clock.now())])) else {
                throw Fail("internal_error")
            }
            if reply.status != 0 { return sendReply(c, reply) }
            startStream(c, Int(reply.field("id")?.toInt32() ?? 0))
        default:  // actions
            if method != "POST" { throw Fail("method_not_allowed") }
            try readJSON(c, r, allowEmpty: false) { [unowned self] body in
                let (b, token) = self.bearer(r)
                guard let prep = Reply(self.engine.call("actionPrepare", [
                    self.engine.string(code), self.bodyValue(body), self.engine.number(Double(b)), self.engine.string(token),
                    self.engine.number(self.clock.now()),
                ])) else { return self.fail(c, Fail("internal_error")) }
                if prep.status != 0 { return self.sendReply(c, prep) }
                let key = prep.field("limiterKey")?.toString() ?? ""
                let (ok, retry) = self.actionLimiter.allow(key, now: self.clock.now())
                if !ok { return self.fail(c, Fail("rate_limited", retry)) }
                guard var pool = SecureRandom.draw(SecureRandom.action) else { return self.fail(c, Fail("internal_error")) }
                let reply = Reply(self.engine.call("actionCommit", [prep.field("ctx") as Any, self.engine.number(self.clock.now()),
                                                                    self.engine.bytes(pool)]))
                SecureRandom.wipe(&pool)
                var extra: [(String, String)] = []
                if let reply = reply, reply.replayed >= 0 { extra = [("X-Action-Replayed", reply.replayed == 1 ? "1" : "0")] }
                self.sendReply(c, reply, extra: extra)
                self.schedulePumpAll()
            }
        }
    }

    private func bodyValue(_ body: [UInt8]?) -> JSValue {
        guard let body = body else { return engine.null }
        return engine.bytes(body)
    }

    /// ROOM_ROUTE.fullmatch: /api/rooms/<[A-Za-z0-9]{1,16}>/(join|state|events|actions)
    static func roomRoute(_ path: String) -> (String, String)? {
        let prefix = "/api/rooms/"
        guard path.hasPrefix(prefix) else { return nil }
        let rest = path.dropFirst(prefix.count).split(separator: "/", omittingEmptySubsequences: false)
        guard rest.count == 2 else { return nil }
        let code = rest[0], leaf = String(rest[1])
        guard (1...16).contains(code.utf8.count),
              code.utf8.allSatisfy({ ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x41 && $0 <= 0x5A) || ($0 >= 0x61 && $0 <= 0x7A) }),
              ["join", "state", "events", "actions"].contains(leaf) else { return nil }
        return (String(code), leaf)
    }

    /// httpd._read_json framing and media-type checks. Calls `then` with the
    /// exact body bytes, or nil for an allowed empty body.
    private func readJSON(_ c: Conn, _ r: Request, allowEmpty: Bool, then: @escaping ([UInt8]?) -> Void) throws {
        guard let length = r.contentLength else {
            if allowEmpty { return then(nil) }
            throw Fail("length_required")
        }
        if length > limits.maxBodyBytes { throw Fail("body_too_large") }
        if length == 0 {
            if allowEmpty { return then(nil) }
            throw Fail("invalid_json")
        }
        let ct = (r.first("content-type") ?? "").split(separator: ";", maxSplits: 1, omittingEmptySubsequences: false)
            .first.map { Server.strip(String($0)).lowercased() } ?? ""
        if ct != "application/json" { throw Fail("unsupported_media_type") }
        if c.buf.count >= r.bodyStart + length {
            return then(Array(c.buf[r.bodyStart..<(r.bodyStart + length)]))
        }
        c.bodyNeed = length
        c.bodyThen = { then($0) }
        c.phase = .body
        if c.peerEOF {
            c.bodyThen = nil
            c.phase = .responding
            throw Fail("bad_request")
        }
    }

    // ----------------------------------------------------------------- SSE

    private func startStream(_ c: Conn, _ id: Int) {
        c.streamId = id
        c.status = 200
        c.phase = .sse
        c.deadline?.cancel()
        c.deadline = nil
        streams[c.id] = c
        sendFrame(c, HTTP.sseHead(), thenClose: false)
        schedulePumpAll()  // presence changed the revision for every viewer
    }

    private func sendFrame(_ c: Conn, _ bytes: [UInt8], thenClose: Bool) {
        c.sending = true
        c.writeTimer?.cancel()
        c.writeTimer = timer(limits.sseWriteTimeout) { [weak self, weak c] in
            guard let self = self, let c = c, c.phase == .sse else { return }
            self.close(c, force: true)  // slow or stalled reader
        }
        c.nw.send(content: Data(bytes), completion: .contentProcessed { [weak self] err in
            guard let self = self else { return }
            c.sending = false
            c.writeTimer?.cancel()
            c.writeTimer = nil
            guard c.phase == .sse else { return }
            if err != nil { return self.close(c, force: true) }
            if thenClose { return self.closeStreamGracefully(c) }
            self.pump(c)
        })
    }

    private func closeStreamGracefully(_ c: Conn) {
        c.nw.send(content: nil, contentContext: .finalMessage, isComplete: true, completion: .contentProcessed { [weak self] _ in
            self?.close(c, force: false)
        })
        // Bookkeeping now; the FIN is already queued.
        if let id = c.streamId {
            c.streamId = nil
            _ = engine.call("streamClose", [engine.number(Double(id))])
            schedulePumpAll()
        }
    }

    private func pump(_ c: Conn) {
        guard c.phase == .sse, !c.sending, let id = c.streamId else { return }
        c.waitTimer?.cancel()
        c.waitTimer = nil
        guard let ev = engine.call("streamPoll", [engine.number(Double(id)), engine.number(clock.now())]), ev.isObject else {
            flushDiagnostics()
            return closeStreamGracefully(c)
        }
        let kind = ev.objectForKeyedSubscript("kind")?.toString() ?? "close"
        let frame = ev.objectForKeyedSubscript("frame").flatMap { $0.isString ? Array($0.toString().utf8) : nil }
        switch kind {
        case "wait":
            let secs = ev.objectForKeyedSubscript("wait")?.toDouble() ?? 1.0
            c.waitTimer = timer(secs) { [weak self, weak c] in
                guard let self = self, let c = c else { return }
                self.pump(c)
            }
        case "heartbeat":
            if c.peerEOF { return close(c, force: false) }
            sendFrame(c, frame ?? [], thenClose: false)
        case "state":
            sendFrame(c, frame ?? [], thenClose: false)
        case "error":
            sendFrame(c, frame ?? [], thenClose: true)
        default:
            flushDiagnostics()
            closeStreamGracefully(c)
        }
    }

    /// Re-poll every idle stream soon (coalesced). Called after anything that
    /// can change a revision: joins, actions, stream open/close, sweeps and
    /// operator calls.
    func schedulePumpAll() {
        if pumpScheduled { return }
        pumpScheduled = true
        // Strong self: same lifetime hazard as pause()/shutdown() above — a
        // weak capture can silently drop this poll if the caller's last
        // reference to the owning PartyHost completes before this runs.
        queue.async { [self] in
            self.pumpScheduled = false
            for c in Array(self.streams.values) { self.pump(c) }
        }
    }

    // --------------------------------------------------- pause/resume (non-terminal)

    /// Non-terminal network suspend (e.g. app backgrounded): cancels the
    /// listener and sweep timer and tears down current connections/streams
    /// so clients see a clean disconnect and know to reconnect, but — unlike
    /// shutdown() — never sets `stopping`, never calls engine "shutdown"
    /// (that flips party/core's RoomManager.stopped permanently with no way
    /// back, per majlis-core.js), and never closes the static-file root.
    /// Room/engine state is untouched. Call start() again to resume; the
    /// listener is gone (`listener = nil`) so a fresh NWListener is created,
    /// possibly on a new address/port (the caller's rebind path).
    func pause(_ done: @escaping () -> Void) {
        guard !stopping else { done(); return }
        sweepTimer?.cancel()
        sweepTimer = nil
        listener?.cancel()
        listener = nil
        for c in Array(streams.values) { closeStreamGracefully(c) }
        // Strong self: a caller (e.g. PartyHost.rebind) can hold no other
        // reference to this Server across the 0.2s drain once its own last
        // syntactic use of the owning PartyHost completes — Swift's ARC can
        // deallocate at last use, not just at scope exit. A weak capture here
        // let `self` go nil mid-drain (observed directly via debug trace
        // tracking item3/4's rework), silently skipping the conns-close loop.
        queue.asyncAfter(deadline: .now() + 0.2) { [self] in
            for c in Array(self.conns.values) { self.close(c, force: false) }
            done()
        }
    }

    // ------------------------------------------------------------ shutdown

    /// Terminal stop: tears down the listener/connections, flips party/core's
    /// RoomManager.stopped permanently (engine "shutdown"), and closes the
    /// static-file root. One-way — never call this for a temporary pause
    /// (use pause() above). Idempotent including completion: a repeat call
    /// while already stopping still invokes `done()` rather than dropping it,
    /// since callers (e.g. PartyLifecycleCoordinator.endSession) rely on the
    /// completion to update UI/state even on a redundant stop.
    func shutdown(_ done: @escaping () -> Void) {
        if stopping {
            queue.async { done() }
            return
        }
        stopping = true
        _ = engine.call("shutdown", [])
        sweepTimer?.cancel()
        listener?.cancel()
        for c in Array(streams.values) { pump(c) }  // every stream sees "close"
        // Strong self (see pause()'s comment above): a weak capture here let
        // `self` go nil during the drain whenever the caller's last reference
        // to the owning PartyHost completed before this fired (e.g.
        // PartyHost.stop(completion:) itself, the normal call site) — the
        // `guard` then returned without ever calling `done()`, hanging the
        // caller forever. Confirmed via debug trace: "asyncAfter fired" then
        // "self was nil!", never "calling done()".
        queue.asyncAfter(deadline: .now() + 0.2) { [self] in
            for c in Array(self.conns.values) { self.close(c, force: false) }
            self.web?.close()
            done()
        }
    }
}
