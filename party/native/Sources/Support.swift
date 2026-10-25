// Entropy, clock, rate limiting and the static file root.

import Foundation
import Security

// ------------------------------------------------------------------ entropy

/// OS CSPRNG. Every operation gets a fresh pool sized for its worst case; a
/// failed draw yields nil and the caller answers internal_error without
/// calling the engine, so no partial credential can exist.
enum SecureRandom {
    // Worst-case draws (see party/native/README.md, "Entropy").
    static let create = 6 * 16 + 32 + 32
    static let join = 32 + 5 * 16
    static let action = 8
    static let operatorCall = create

    #if MAJLIS_TEST_LOOPBACK
    /// Test-only failure injection (stdin control line). Absent from production builds.
    static var failForTesting = false
    #endif

    static func draw(_ n: Int) -> [UInt8]? {
        #if MAJLIS_TEST_LOOPBACK
        if failForTesting { return nil }
        #endif
        var out = [UInt8](repeating: 0, count: n)
        let status = out.withUnsafeMutableBytes { SecRandomCopyBytes(kSecRandomDefault, n, $0.baseAddress!) }
        if status != errSecSuccess {
            wipe(&out)
            return nil
        }
        return out
    }

    static func wipe(_ b: inout [UInt8]) {
        b.withUnsafeMutableBytes { p in
            if let base = p.baseAddress { memset_s(base, p.count, 0, p.count) }
        }
    }
}

// -------------------------------------------------------------------- clock

/// Monotonic seconds, including time asleep (CLOCK_MONOTONIC). Injected so
/// tests can substitute; read once per request on the engine queue.
struct MonotonicClock {
    var now: () -> Double = { Double(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1_000_000_000 }
}

// ----------------------------------------------------------------- ClockGate

/// Bridges a host's own background/foreground boundary into Server.clock:
/// freezes exactly the real wall-clock duration spent paused out of every
/// `clock.now()` reading party/core sees (sweep, actionCommit, join, ...),
/// so a room's/token's TTL deadline (`now + ttl`, compared against a later
/// `now()`) never counts a suspended gap as idle time the room/token sat
/// through unattended (L02A blocker 6). Time while actually active — before
/// `pause()` and after `resume()` — passes through unchanged; only the gap
/// itself is ever removed, and `resume()` is a one-shot correction, not a
/// standing freeze, so elapsed time after it resumes advancing immediately.
/// Pure arithmetic, no clock of its own: Server (the only caller) supplies
/// every real reading and decides when pause/resume happen; this has no
/// opinion on whether a given gap is within the host's own pause policy at
/// all — LifecyclePolicy.onForeground's `.rebind`-vs-`.endExpired` bound
/// already owns that, before Server.resumeClock() is ever called.
struct ClockGate: Equatable {
    private(set) var offset: TimeInterval = 0
    private var pausedAtReal: TimeInterval?

    var isPaused: Bool { pausedAtReal != nil }

    /// Idempotent: a second pause() before any resume() is a no-op, so a
    /// caller can't accidentally lose the original pause instant by
    /// signaling background twice.
    mutating func pause(real now: TimeInterval) {
        guard pausedAtReal == nil else { return }
        pausedAtReal = now
    }

    /// Returns the real elapsed duration removed, for the caller's own
    /// evidence/logging. A resume() with no matching pause() is a no-op
    /// (returns 0) rather than an error — Server always calls pause/resume
    /// in matched pairs, but this stays safe if a caller ever doesn't.
    @discardableResult
    mutating func resume(real now: TimeInterval) -> TimeInterval {
        guard let pausedAtReal else { return 0 }
        let elapsed = max(0, now - pausedAtReal)
        offset -= elapsed
        self.pausedAtReal = nil
        return elapsed
    }

    /// Discards any accumulated offset and pending pause — used when a
    /// paused session ends instead of resuming, so a later unrelated room
    /// sharing the same Server never inherits a stale offset.
    mutating func reset() {
        offset = 0
        pausedAtReal = nil
    }

    /// While paused, every reading must come back identical regardless of
    /// how much real time actually passes during the gap — `offset` alone
    /// cannot do that (it only accounts for elapsed real time once
    /// `resume()` computes it), so this returns the frozen instant captured
    /// at `pause()` directly instead of `real + offset` whenever a pause is
    /// still open. `resume()` is defined so that `apply` at the resume
    /// instant equals this same frozen value — continuing forward, never
    /// backward — the moment the pause actually ends.
    func apply(_ real: TimeInterval) -> TimeInterval {
        if let pausedAtReal {
            return pausedAtReal + offset
        }
        return real + offset
    }
}

// --------------------------------------------------------------- RateLimiter

/// manager.RateLimiter: token bucket per key, at most 4096 keys, evicting the
/// least recently used. Only touched on the engine queue.
final class RateLimiter {
    private let rate: Double
    private let burst: Double
    private let maxKeys: Int
    private var buckets: [String: (tokens: Double, last: Double, stamp: UInt64)] = [:]
    private var stamp: UInt64 = 0

    init(rate: Double, burst: Double, maxKeys: Int = 4096) {
        self.rate = rate
        self.burst = burst
        self.maxKeys = maxKeys
    }

    func allow(_ key: String, now: Double) -> (Bool, Double) {
        let prev = buckets.removeValue(forKey: key) ?? (burst, now, 0)
        var tokens = min(burst, prev.tokens + max(0.0, now - prev.last) * rate)
        let allowed: Bool
        var retry = 0.0
        if tokens >= 1.0 {
            tokens -= 1.0
            allowed = true
        } else {
            allowed = false
            retry = (1.0 - tokens) / rate
        }
        stamp += 1
        buckets[key] = (tokens, now, stamp)
        while buckets.count > maxKeys {
            if let oldest = buckets.min(by: { $0.value.stamp < $1.value.stamp }) { buckets.removeValue(forKey: oldest.key) }
        }
        return (allowed, retry)
    }

    var count: Int { buckets.count }
}

/// Retry-After as the reference writes it: str(max(1, int(retry + 0.999))).
func retryAfterHeader(_ retry: Double) -> String {
    let v = retry + 0.999
    if v < 1 { return "1" }
    if v < 1e15 { return String(Int(v)) }
    return String(format: "%.0f", v.rounded(.towardZero))
}

// -------------------------------------------------------------- static root

enum StaticType {
    static let html = "text/html; charset=utf-8"
    static let js = "text/javascript; charset=utf-8"
    static let css = "text/css; charset=utf-8"
}

/// static.py: exact allowlist, descriptor-relative opens with O_NOFOLLOW on
/// every component, regular files up to 2 MiB, read per request.
final class StaticRoot {
    static let maxFileBytes = 2 * 1024 * 1024
    static let htmlCSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; " +
        "form-action 'self'; frame-ancestors 'none'"

    /// The reference allowlist (test-loopback surface).
    static let referenceFiles: [String: ([String], String)] = {
        var f: [String: ([String], String)] = [
            "/": (["index.html"], StaticType.html),
            "/index.html": (["index.html"], StaticType.html),
            "/host.html": (["host.html"], StaticType.html),
            "/play.html": (["play.html"], StaticType.html),
            "/tv.html": (["tv.html"], StaticType.html),
            "/css/party.css": (["css", "party.css"], StaticType.css),
        ]
        for n in ["index", "host", "play", "tv", "ui", "transport", "session", "modes", "views", "qr"] {
            f["/js/\(n).js"] = (["js", "\(n).js"], StaticType.js)
        }
        return f
    }()

    /// Production LAN surface: the player pages and the modules they import.
    /// Operator and display pages are not served to the network. The index
    /// page's "create room" control calls the network POST /api/rooms route,
    /// which production never has (L02A brief: "remove the broken network
    /// create-room entry"); play.html is the player entry point instead, so
    /// "/" and the index page/script are dropped from this surface too.
    static let productionExcluded: Set<String> = [
        "/host.html", "/tv.html", "/js/host.js", "/js/tv.js", "/", "/index.html", "/js/index.js",
    ]
    static let productionFiles = referenceFiles.filter { !productionExcluded.contains($0.key) }

    private var fd: Int32
    let files: [String: ([String], String)]

    init(directory: String, files: [String: ([String], String)]) throws {
        self.files = files
        let d = directory.isEmpty ? -1 : open(directory, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard d >= 0 else { throw ConfigError("--web-dir must be an existing directory") }
        var st = stat()
        if fstat(d, &st) != 0 || (st.st_mode & S_IFMT) != S_IFDIR {
            Darwin.close(d)
            throw ConfigError("--web-dir must be an existing directory")
        }
        fd = d
    }

    func lookup(_ path: String) -> ([String], String)? { files[path] }

    static func queryAllowed(_ path: String, _ query: String) -> Bool {
        guard path == "/play.html" else { return false }
        let q = Array(query.utf8)
        guard q.count == 11, Array("code=".utf8) == Array(q[0..<5]) else { return false }
        let alphabet = Set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789".utf8)
        return q[5...].allSatisfy { alphabet.contains($0) }
    }

    func read(_ parts: [String]) -> [UInt8]? {
        guard fd >= 0 else { return nil }
        var opened: [Int32] = []
        defer { for f in opened.reversed() { Darwin.close(f) } }
        var dir = fd
        for name in parts.dropLast() {
            let d = openat(dir, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard d >= 0 else { return nil }
            opened.append(d)
            var st = stat()
            guard fstat(d, &st) == 0, (st.st_mode & S_IFMT) == S_IFDIR else { return nil }
            dir = d
        }
        let f = openat(dir, parts.last!, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard f >= 0 else { return nil }
        opened.append(f)
        var st = stat()
        guard fstat(f, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_size <= StaticRoot.maxFileBytes else { return nil }
        var out = [UInt8]()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = chunk.withUnsafeMutableBytes { Darwin.read(f, $0.baseAddress!, 65536) }
            if n < 0 { return nil }
            if n == 0 { break }
            out.append(contentsOf: chunk[0..<n])
            if out.count > StaticRoot.maxFileBytes { return nil }
        }
        return out
    }

    func close() {
        if fd >= 0 { Darwin.close(fd) }
        fd = -1
    }
}

struct ConfigError: Error {
    let detail: String
    init(_ d: String) { detail = d }
}
