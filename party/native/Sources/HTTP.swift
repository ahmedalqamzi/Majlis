// Strict HTTP/1.x request-head parser and response writer.
//
// One request per connection; every response closes. The parser accepts
// less than the reference's BaseHTTPRequestHandler on purpose (see
// party/native/README.md, "Declared transport deviations"): no HTTP/0.9, no
// obs-fold, no non-ASCII or control bytes in the head, a single well-formed
// Content-Length, no Transfer-Encoding, and smaller head limits.

import Foundation

struct Request {
    let method: String
    let target: String
    let headers: [(name: String, value: String)]  // name lowercased; value with leading SP/HTAB removed
    let bodyStart: Int
    let contentLength: Int?  // nil when absent; capped at Int.max / 2

    func all(_ name: String) -> [String] { headers.filter { $0.name == name }.map { $0.value } }
    func first(_ name: String) -> String? { headers.first { $0.name == name }?.value }
}

enum ParseResult {
    case incomplete
    case request(Request)
    case reject(status: Int, code: String)
}

enum HTTP {
    static let maxRequestLine = 8 * 1024
    static let maxHead = 16 * 1024
    static let maxHeaders = 100
    static let knownMethods: Set<String> = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE", "CONNECT"]

    // RFC 9110 tchar
    static func isTchar(_ b: UInt8) -> Bool {
        switch b {
        case 0x30...0x39, 0x41...0x5A, 0x61...0x7A: return true
        case UInt8(ascii: "!"), UInt8(ascii: "#"), UInt8(ascii: "$"), UInt8(ascii: "%"), UInt8(ascii: "&"),
             UInt8(ascii: "'"), UInt8(ascii: "*"), UInt8(ascii: "+"), UInt8(ascii: "-"), UInt8(ascii: "."),
             UInt8(ascii: "^"), UInt8(ascii: "_"), UInt8(ascii: "`"), UInt8(ascii: "|"), UInt8(ascii: "~"):
            return true
        default: return false
        }
    }

    static func ascii(_ b: ArraySlice<UInt8>) -> String { String(decoding: b, as: UTF8.self) }

    /// Next line [start, end) excluding CR LF / LF, and the index after the LF.
    private static func line(_ buf: [UInt8], from: Int) -> (Int, Int, Int)? {
        var i = from
        while i < buf.count {
            if buf[i] == 0x0A {
                let end = (i > from && buf[i - 1] == 0x0D) ? i - 1 : i
                return (from, end, i + 1)
            }
            i += 1
        }
        return nil
    }

    static func parse(_ buf: [UInt8]) -> ParseResult {
        guard let (ls, le, next0) = line(buf, from: 0) else {
            if buf.count > maxRequestLine { return .reject(status: 414, code: "bad_request") }
            return .incomplete
        }
        if le - ls > maxRequestLine { return .reject(status: 414, code: "bad_request") }
        let rl = buf[ls..<le]
        // METHOD SP request-target SP HTTP/x.y, visible ASCII only.
        var parts: [ArraySlice<UInt8>] = []
        var start = ls
        for i in ls..<le {
            let b = buf[i]
            if b == 0x20 {
                parts.append(buf[start..<i])
                start = i + 1
            } else if b < 0x21 || b > 0x7E {
                return .reject(status: 400, code: "bad_request")
            }
        }
        parts.append(buf[start..<le])
        guard rl.count > 0, parts.count == 3, parts.allSatisfy({ !$0.isEmpty }) else {
            return .reject(status: 400, code: "bad_request")
        }
        guard parts[0].allSatisfy(isTchar) else { return .reject(status: 400, code: "bad_request") }
        let version = ascii(parts[2])
        guard version.hasPrefix("HTTP/") else { return .reject(status: 400, code: "bad_request") }
        let nums = version.dropFirst(5).split(separator: ".", omittingEmptySubsequences: false)
        guard nums.count == 2, nums.allSatisfy({ !$0.isEmpty && $0.count <= 10 && $0.allSatisfy { $0 >= "0" && $0 <= "9" } }),
              let major = Int(nums[0]) else {
            return .reject(status: 400, code: "bad_request")
        }
        if major >= 2 { return .reject(status: 505, code: "bad_request") }

        var headers: [(name: String, value: String)] = []
        var pos = next0
        while true {
            guard let (hs, he, nx) = line(buf, from: pos) else {
                if buf.count > maxHead { return .reject(status: 431, code: "bad_request") }
                return .incomplete
            }
            if nx > maxHead { return .reject(status: 431, code: "bad_request") }
            if hs == he { pos = nx; break }
            if headers.count >= maxHeaders { return .reject(status: 431, code: "bad_request") }
            var colon = -1
            for i in hs..<he where buf[i] == UInt8(ascii: ":") {
                colon = i
                break
            }
            guard colon > hs, buf[hs..<colon].allSatisfy(isTchar) else {
                return .reject(status: 400, code: "bad_request")  // includes obs-fold (leading SP/HTAB)
            }
            var vs = colon + 1
            for i in vs..<he {
                let b = buf[i]
                if !(b == 0x09 || (b >= 0x20 && b <= 0x7E)) { return .reject(status: 400, code: "bad_request") }
            }
            while vs < he && (buf[vs] == 0x20 || buf[vs] == 0x09) { vs += 1 }
            headers.append((ascii(buf[hs..<colon]).lowercased(), ascii(buf[vs..<he])))
            pos = nx
        }

        let method = ascii(parts[0])
        if !knownMethods.contains(method) { return .reject(status: 501, code: "method_not_allowed") }
        if headers.contains(where: { $0.name == "transfer-encoding" }) {
            return .reject(status: 411, code: "length_required")
        }
        let cls = headers.filter { $0.name == "content-length" }
        var length: Int?
        if cls.count > 1 { return .reject(status: 400, code: "bad_request") }
        if let v = cls.first?.value {
            let t = v.trimmingCharacters(in: CharacterSet(charactersIn: " \t"))
            guard !t.isEmpty, t.utf8.allSatisfy({ $0 >= 0x30 && $0 <= 0x39 }) else {
                return .reject(status: 400, code: "bad_request")
            }
            let digits = t.drop(while: { $0 == "0" })
            length = digits.count > 15 ? Int.max / 2 : (Int(digits) ?? 0)
        }
        return .request(Request(method: method, target: ascii(parts[1]), headers: headers, bodyStart: pos,
                                contentLength: length))
    }

    // ------------------------------------------------------------ responses

    static let apiCSP = "default-src 'none'; frame-ancestors 'none'"

    static let phrases: [Int: String] = [
        200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
        405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict", 410: "Gone", 411: "Length Required",
        413: "Request Entity Too Large", 414: "Request-URI Too Long", 415: "Unsupported Media Type",
        429: "Too Many Requests", 431: "Request Header Fields Too Large", 500: "Internal Server Error",
        501: "Not Implemented", 503: "Service Unavailable", 505: "HTTP Version Not Supported",
    ]

    static func date() -> String {
        var t = time(nil)
        var tmv = tm()
        gmtime_r(&t, &tmv)
        let days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return String(format: "%@, %02d %@ %04d %02d:%02d:%02d GMT", days[Int(tmv.tm_wday)], tmv.tm_mday,
                      months[Int(tmv.tm_mon)], tmv.tm_year + 1900, tmv.tm_hour, tmv.tm_min, tmv.tm_sec)
    }

    static func common(csp: String = apiCSP) -> [(String, String)] {
        [("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
         ("X-Frame-Options", "DENY"), ("Content-Security-Policy", csp),
         ("Cross-Origin-Resource-Policy", "same-origin"), ("Connection", "close")]
    }

    static func head(_ status: Int, _ headers: [(String, String)]) -> [UInt8] {
        var s = "HTTP/1.0 \(status) \(phrases[status] ?? "Unknown")\r\nServer: MajlisParty/1\r\nDate: \(date())\r\n"
        for (k, v) in headers { s += "\(k): \(v)\r\n" }
        s += "\r\n"
        return Array(s.utf8)
    }

    static func json(_ status: Int, _ body: [UInt8], extra: [(String, String)] = [], includeBody: Bool = true) -> [UInt8] {
        let h = common() + [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", String(body.count))] + extra
        return head(status, h) + (includeBody ? body : [])
    }

    static func staticFile(_ data: [UInt8], _ type: String) -> [UInt8] {
        let csp = type == StaticType.html ? StaticRoot.htmlCSP : apiCSP
        return head(200, common(csp: csp) + [("Content-Type", type), ("Content-Length", String(data.count))]) + data
    }

    static func sseHead() -> [UInt8] {
        head(200, common() + [("Content-Type", "text/event-stream; charset=utf-8"), ("X-Accel-Buffering", "no")]) +
            Array("retry: 3000\n\n".utf8)
    }

    /// httpd.process_request's raw over-capacity reply (no Server/Date headers).
    static func serverBusy(_ body: [UInt8]) -> [UInt8] {
        Array(("HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n" +
               "Content-Length: \(body.count)\r\nConnection: close\r\n\r\n").utf8) + body
    }
}
