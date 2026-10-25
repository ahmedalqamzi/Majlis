// majlis-party-native: CLI mirroring `python3 -m party.server`.
//
// Production build (party/native/build.sh, no flags): LAN-capable, production
// surface only. Rooms are created and hosted through the private operator
// bridge on stdin (--operator-stdin), never over the network.
//
// Test build (-D MAJLIS_TEST_LOOPBACK): additionally accepts
// --surface test-loopback|production (default test-loopback), refuses every
// non-loopback bind, and accepts test control lines on the bridge. It exists
// to run the reference wire suites and fault injection; it is not shipped.

import Foundation
import JavaScriptCore
import Network

setvbuf(stdout, nil, _IOLBF, 0)

func writeLine(_ s: String, _ stream: UnsafeMutablePointer<FILE>) {
    fputs(s + "\n", stream)
    fflush(stream)
}

let usage = "usage: majlis-party-native [--host HOST] [--port PORT] [--allow-lan] [--allowed-origin URL] " +
    "[--content-dir DIR] [--check-content] [--web-dir DIR] [--operator-stdin] [--exit-on-stdin-eof] [limits...]"

func argError(_ message: String) -> Never {
    writeLine(usage, stderr)
    writeLine("majlis-party-native: error: " + message, stderr)
    exit(2)
}

// ------------------------------------------------------------------ arguments

enum ArgKind { case int, float, flag, string, append }

struct Options {
    var host = "127.0.0.1"
    var port = 8765
    var allowLan = false
    var origins: [String] = []
    var contentDir: String?
    var checkContent = false
    var webDir: String?
    var operatorStdin = false
    var exitOnStdinEOF = false
    var surface = Surface.production
    /// ServerConfig overrides, snake_case, numbers and access_log.
    var config: [String: Any] = [:]
}

// flag -> (config key or "", kind)
let configFlags: [String: (String, ArgKind)] = [
    "--min-players": ("min_players", .int), "--max-players": ("max_players", .int),
    "--room-idle-ttl": ("room_idle_ttl", .float), "--room-max-age": ("room_max_age", .float),
    "--token-ttl": ("token_ttl", .float), "--closed-room-grace": ("closed_room_grace", .float),
    "--max-rooms": ("max_rooms", .int), "--action-retention": ("action_retention", .int),
    "--heartbeat": ("heartbeat_interval", .float), "--sweep-interval": ("sweep_interval", .float),
    "--sse-write-timeout": ("sse_write_timeout", .float), "--request-timeout": ("request_timeout", .float),
    "--max-body-bytes": ("max_body_bytes", .int),
    "--max-streams-per-principal": ("max_streams_per_principal", .int),
    "--max-streams-per-room": ("max_streams_per_room", .int), "--max-streams-total": ("max_streams_total", .int),
    "--max-connections": ("max_connections", .int), "--request-rate": ("request_rate", .float),
    "--request-burst": ("request_burst", .int), "--create-per-minute": ("create_per_minute", .float),
    "--create-burst": ("create_burst", .int), "--join-per-minute": ("join_per_minute", .float),
    "--join-burst": ("join_burst", .int), "--action-rate": ("action_rate", .float),
    "--action-burst": ("action_burst", .int),
]

func parseInt(_ s: String, _ flag: String) -> Int {
    let t = s.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "_", with: "")
    guard let v = Int(t) else { argError("argument \(flag): invalid int value: '\(s)'") }
    return v
}

func parseFloat(_ s: String, _ flag: String) -> Double {
    let t = s.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: "_", with: "")
    guard let v = Double(t) else { argError("argument \(flag): invalid float value: '\(s)'") }
    return v
}

func parseOptions(_ argv: [String]) -> Options {
    var o = Options()
    #if MAJLIS_TEST_LOOPBACK
    o.surface = .testLoopback
    #endif
    var i = 0
    while i < argv.count {
        var a = argv[i]
        var inline: String?
        if a.hasPrefix("--"), let eq = a.firstIndex(of: "=") {
            inline = String(a[a.index(after: eq)...])
            a = String(a[..<eq])
        }
        func value() -> String {
            if let v = inline { return v }
            i += 1
            guard i < argv.count else { argError("argument \(a): expected one argument") }
            return argv[i]
        }
        func noValue() {
            if inline != nil { argError("argument \(a): ignored explicit argument '\(inline!)'") }
        }
        switch a {
        case "--host": o.host = value()
        case "--port": o.port = parseInt(value(), a)
        case "--allow-lan": noValue(); o.allowLan = true
        case "--allowed-origin": o.origins.append(value())
        case "--content-dir": o.contentDir = value()
        case "--check-content": noValue(); o.checkContent = true
        case "--web-dir": o.webDir = value()
        case "--access-log": noValue(); o.config["access_log"] = true
        case "--exit-on-stdin-eof": noValue(); o.exitOnStdinEOF = true
        case "--operator-stdin": noValue(); o.operatorStdin = true
        #if MAJLIS_TEST_LOOPBACK
        case "--surface":
            guard let s = Surface(rawValue: value()) else { argError("argument --surface: invalid choice") }
            o.surface = s
        #endif
        default:
            guard let (key, kind) = configFlags[a] else { argError("unrecognized arguments: \(argv[i])") }
            let v = value()
            o.config[key] = kind == .int ? parseInt(v, a) as Any : parseFloat(v, a) as Any
        }
        i += 1
    }
    return o
}

let options = parseOptions(Array(CommandLine.arguments.dropFirst()))

// ------------------------------------------------------------------ engine

let engine: Engine
do {
    engine = try Engine(sources: EmbeddedSources.all)
} catch {
    writeLine("{\"detail\": \"engine failed to load\", \"error\": \"internal_error\", \"event\": \"error\"}", stderr)
    exit(2)
}

func emitError(_ code: String, _ detail: String) -> Never {
    let line = engine.call("line", [engine.object([("event", engine.string("error")), ("error", engine.string(code)),
                                                   ("detail", engine.string(detail))])])?.toString() ?? ""
    writeLine(line, stderr)
    exit(2)
}

// content.load_mode reads {dir}/{mode}.json (at most MAX_FILE_BYTES + 1 bytes).
// Absent, non-regular or unreadable files load as missing.
let implementedModes = ["who_am_i", "feud", "impostor"]
func readPack(_ dir: String, _ mode: String) -> [UInt8]? {
    var isDir: ObjCBool = false
    guard FileManager.default.fileExists(atPath: dir, isDirectory: &isDir), isDir.boolValue else { return nil }
    let path = (dir as NSString).appendingPathComponent(mode + ".json")
    let fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK)
    guard fd >= 0 else { return nil }
    defer { close(fd) }
    var st = stat()
    guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else { return nil }
    let cap = 2 * 1024 * 1024 + 1
    var out = [UInt8]()
    var chunk = [UInt8](repeating: 0, count: 65536)
    while out.count < cap {
        let n = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress!, min(65536, cap - out.count)) }
        if n < 0 { return nil }
        if n == 0 { break }
        out.append(contentsOf: chunk[0..<n])
    }
    return out
}

let contentDir = options.contentDir ?? (FileManager.default.currentDirectoryPath as NSString).appendingPathComponent("party/content")
let packs = engine.object(implementedModes.map { mode in
    (mode, readPack(contentDir, mode).map { engine.bytes($0) } ?? engine.null)
})

if options.checkContent {
    guard let r = engine.call("checkContent", [packs]), let line = r.objectForKeyedSubscript("line")?.toString() else {
        exit(2)
    }
    writeLine(line, stdout)
    exit(r.objectForKeyedSubscript("ok")?.toBool() == true ? 0 : 1)
}

// ------------------------------------------------------------------ config

let configValue = engine.object([])
for (k, v) in options.config {
    if let b = v as? Bool { configValue.setObject(engine.bool(b), forKeyedSubscript: k as NSString) }
    else if let n = v as? Int { configValue.setObject(engine.number(Double(n)), forKeyedSubscript: k as NSString) }
    else if let d = v as? Double { configValue.setObject(engine.number(d), forKeyedSubscript: k as NSString) }
}
guard let initResult = engine.call("init", [engine.string(options.surface.rawValue), configValue, packs]) else {
    emitError("internal_error", "engine init failed")
}
if initResult.objectForKeyedSubscript("ok")?.toBool() != true {
    emitError("invalid_config", initResult.objectForKeyedSubscript("detail")?.toString() ?? "invalid config")
}

func cfg(_ key: String) -> Double { configValue.hasProperty(key) ? configValue.forProperty(key).toDouble() : -1 }
var limits = Limits()
func setIf(_ key: String, _ apply: (Double) -> Void) { if configValue.hasProperty(key) { apply(cfg(key)) } }
setIf("request_timeout") { limits.requestTimeout = $0 }
setIf("sse_write_timeout") { limits.sseWriteTimeout = $0 }
setIf("sweep_interval") { limits.sweepInterval = $0 }
setIf("max_body_bytes") { limits.maxBodyBytes = Int($0) }
setIf("max_connections") { limits.maxConnections = Int($0) }
setIf("request_rate") { limits.requestRate = $0 }
setIf("request_burst") { limits.requestBurst = $0 }
setIf("create_per_minute") { limits.createRate = $0 / 60.0 }
setIf("create_burst") { limits.createBurst = $0 }
setIf("join_per_minute") { limits.joinRate = $0 / 60.0 }
setIf("join_burst") { limits.joinBurst = $0 }
setIf("action_rate") { limits.actionRate = $0 }
setIf("action_burst") { limits.actionBurst = $0 }
limits.accessLog = options.config["access_log"] as? Bool ?? false

// ------------------------------------------------------------- bind address

/// ipaddress.ip_address for IPv4 dotted quads (no leading zeros).
func parseIPv4(_ s: String) -> [UInt8]? {
    let parts = s.split(separator: ".", omittingEmptySubsequences: false)
    guard parts.count == 4 else { return nil }
    var out: [UInt8] = []
    for p in parts {
        guard (1...3).contains(p.count), p.allSatisfy({ $0 >= "0" && $0 <= "9" }), !(p.count > 1 && p.first == "0"),
              let v = Int(p), v <= 255 else { return nil }
        out.append(UInt8(v))
    }
    return out
}

/// httpd.normalize_origin: scheme://netloc, lowercased.
func normalizeOrigin(_ raw: String) throws -> String {
    let v = raw.trimmingCharacters(in: .whitespacesAndNewlines).filter { $0 != "\t" && $0 != "\r" && $0 != "\n" }
    guard let sep = v.range(of: "://") else { throw ConfigError("origin must be http(s)://host[:port]") }
    let scheme = v[..<sep.lowerBound].lowercased()
    let rest = v[sep.upperBound...]
    let netEnd = rest.firstIndex(where: { "/?#".contains($0) }) ?? rest.endIndex
    let netloc = String(rest[..<netEnd])
    guard scheme == "http" || scheme == "https", !netloc.isEmpty else {
        throw ConfigError("origin must be http(s)://host[:port]")
    }
    let tail = rest[netEnd...]
    var path = Substring(""), query = Substring(""), fragment = Substring("")
    if let h = tail.firstIndex(of: "#") { fragment = tail[tail.index(after: h)...] }
    let beforeFrag = tail.firstIndex(of: "#").map { tail[..<$0] } ?? tail
    if let q = beforeFrag.firstIndex(of: "?") {
        query = beforeFrag[beforeFrag.index(after: q)...]
        path = beforeFrag[..<q]
    } else {
        path = beforeFrag
    }
    if !(path.isEmpty || path == "/") || !query.isEmpty || !fragment.isEmpty || netloc.contains("@") {
        throw ConfigError("origin must not contain a path, query, fragment or userinfo")
    }
    return scheme + "://" + netloc.lowercased()
}

let bindHost = options.host == "localhost" ? "127.0.0.1" : options.host
guard let addr = parseIPv4(bindHost) else { emitError("invalid_config", "--host must be an IPv4 address or 'localhost'") }
let isLoopback = addr[0] == 127
let isUnspecified = addr == [0, 0, 0, 0]
#if MAJLIS_TEST_LOOPBACK
if !isLoopback { emitError("invalid_config", "test build binds loopback only") }
#endif
if !options.allowLan && !isLoopback { emitError("invalid_config", "refusing non-loopback bind without --allow-lan") }
var origins = Set<String>()
do {
    for o in options.origins { origins.insert(try normalizeOrigin(o)) }
} catch let e as ConfigError {
    emitError("invalid_config", e.detail)
}
if isUnspecified && origins.isEmpty { emitError("invalid_config", "binding 0.0.0.0 requires at least one --allowed-origin") }
guard (0...65535).contains(options.port) else { emitError("bind_failed", "Invalid argument") }

var web: StaticRoot?
if let dir = options.webDir {
    do {
        web = try StaticRoot(directory: dir, files: options.surface == .production ? StaticRoot.productionFiles
                                                                                 : StaticRoot.referenceFiles)
    } catch let e as ConfigError {
        emitError("invalid_config", e.detail)
    }
}

// ------------------------------------------------------------------- serve

let queue = DispatchQueue(label: "majlis.engine")
let server = Server(queue: queue, engine: engine, surface: options.surface, limits: limits, web: web)
server.log = { writeLine($0, stderr) }
engine.onFault = { _ in }  // counted by Engine; class names surface via takeDiagnostics

func stopAndExit() {
    server.shutdown {
        server.flushDiagnostics()
        writeLine(engine.call("line", [engine.object([("event", engine.string("stopped"))])])?.toString() ?? "", stdout)
        exit(0)
    }
}

var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: queue)
    s.setEventHandler { stopAndExit() }
    s.resume()
    signalSources.append(s)
}
signal(SIGPIPE, SIG_IGN)

/// The private operator bridge: one JSON request per stdin line, one JSON
/// reply per stdout line. Runs on the engine queue like any request.
let maxOperatorLine = 64 * 1024
func operatorLine(_ bytes: [UInt8]) {
    #if MAJLIS_TEST_LOOPBACK
    let text = String(decoding: bytes, as: UTF8.self)
    if text == "#csprng-fail on" || text == "#csprng-fail off" {
        SecureRandom.failForTesting = text.hasSuffix("on")
        writeLine("{\"ok\":true,\"op\":\"#csprng-fail\"}", stdout)
        return
    }
    #endif
    if bytes.count > maxOperatorLine {
        return writeLine(Reply(engine.call("errorFor", [engine.string("body_too_large")]))
            .map { String(decoding: $0.body, as: UTF8.self) } ?? "", stdout)
    }
    guard var pool = SecureRandom.draw(SecureRandom.operatorCall) else {
        return writeLine(Reply(engine.call("errorFor", [engine.string("internal_error")]))
            .map { String(decoding: $0.body, as: UTF8.self) } ?? "", stdout)
    }
    let reply = engine.call("operatorCall", [engine.bytes(bytes), engine.number(server.clock.now()), engine.bytes(pool)])
    SecureRandom.wipe(&pool)
    server.flushDiagnostics()
    writeLine(reply?.toString() ?? "{\"ok\":false,\"op\":null,\"error\":{\"code\":\"internal_error\"}}", stdout)
    server.schedulePumpAll()
}

func startStdin() {
    guard options.exitOnStdinEOF || options.operatorStdin else { return }
    let t = Thread {
        var pending = [UInt8]()
        var overflow = false
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = chunk.withUnsafeMutableBytes { read(0, $0.baseAddress!, 65536) }
            if n < 0 && errno == EINTR { continue }
            if n <= 0 { break }
            guard options.operatorStdin else { continue }
            for b in chunk[0..<n] {
                if b == 0x0A {
                    var line = overflow ? [UInt8](repeating: 0x20, count: maxOperatorLine + 1) : pending
                    if line.last == 0x0D { line.removeLast() }
                    pending.removeAll(keepingCapacity: true)
                    overflow = false
                    if !line.isEmpty { queue.async { operatorLine(line) } }
                } else if !overflow {
                    pending.append(b)
                    if pending.count > maxOperatorLine {
                        overflow = true
                        pending.removeAll()
                    }
                }
            }
        }
        if options.exitOnStdinEOF { queue.async { stopAndExit() } }
    }
    t.stackSize = 1 << 20
    t.start()
}

queue.async {
    server.start(host: IPv4Address(Data(addr))!, port: UInt16(options.port)) { result in
        switch result {
        case .failure(let err):
            web?.close()
            var detail = "OSError"
            if case let .posix(code) = err { detail = String(cString: strerror(code.rawValue)) }
            emitError("bind_failed", detail)
        case .success(let port):
            let host = bindHost
            var all = origins
            if !isUnspecified { all.insert("http://\(host):\(port)") }
            if isLoopback {
                all.insert("http://127.0.0.1:\(port)")
                all.insert("http://localhost:\(port)")
            }
            server.allowedOrigins = all
            server.allowedHosts = Set(all.map { o in
                let rest = o[o.range(of: "://")!.upperBound...]
                return String(rest)
            })
            let banner = engine.call("banner", [engine.string(host), engine.number(Double(port)),
                                                engine.bool(web != nil), engine.string(options.surface.rawValue)])
            writeLine(banner?.toString() ?? "", stdout)
            startStdin()
        }
    }
}

dispatchMain()
