// JavaScriptCore host for MajlisCore + MajlisNative.
//
// One bare JSContext (no injected natives, no console, no timers). The host
// evaluates exactly three reviewed sources at startup and afterwards only
// calls fixed functions on the global MajlisNative object with
// JSValue.call(withArguments:). Request bytes cross as Uint8Array data and
// are parsed by the core's JSON parser; nothing received from the network is
// ever passed to evaluateScript.
//
// Not thread-safe by design: every method must run on the owning serial
// queue (Server.queue), which gives the core the total order it expects.

import Foundation
import JavaScriptCore

struct EngineSource {
    let name: String
    let text: String
}

enum EngineError: Error {
    case load(String)
}

final class Engine {
    private let context: JSContext
    private let api: JSValue
    private var fns: [String: JSValue] = [:]
    /// Set by the exception handler; holds a class label only, never a message.
    private(set) var faults = 0
    var onFault: ((String) -> Void)?

    static let entryPoints = [
        "init", "checkContent", "banner", "line", "sweep", "shutdown", "takeDiagnostics", "health", "modes",
        "errorFor", "create", "join", "state", "actionPrepare", "actionCommit", "streamOpen", "streamPoll",
        "streamClose", "streamCount", "operatorCall",
    ]

    init(sources: [EngineSource]) throws {
        guard let ctx = JSContext() else { throw EngineError.load("JSContext unavailable") }
        context = ctx
        var loadFailure: String?
        ctx.exceptionHandler = { _, exc in
            // Keep only the constructor name: exception messages may quote data.
            let name = exc?.objectForKeyedSubscript("name")?.toString() ?? "Error"
            loadFailure = Engine.label(name)
        }
        for src in sources {
            _ = ctx.evaluateScript(src.text, withSourceURL: URL(string: "majlis://" + src.name))
            if let f = loadFailure { throw EngineError.load("\(src.name): \(f)") }
        }
        guard let native = ctx.objectForKeyedSubscript("MajlisNative"), native.isObject else {
            throw EngineError.load("MajlisNative missing")
        }
        api = native
        for name in Engine.entryPoints {
            guard let f = native.objectForKeyedSubscript(name), f.isObject else {
                throw EngineError.load("MajlisNative.\(name) missing")
            }
            fns[name] = f
        }
        ctx.exceptionHandler = { [weak self] _, exc in
            let name = exc?.objectForKeyedSubscript("name")?.toString() ?? "Error"
            self?.faults += 1
            self?.onFault?(Engine.label(name))
        }
    }

    static func label(_ s: String) -> String {
        let letters = s.unicodeScalars.filter { ($0.value >= 65 && $0.value <= 90) || ($0.value >= 97 && $0.value <= 122) }
        let out = String(String.UnicodeScalarView(letters.prefix(40)))
        return out.isEmpty ? "Error" : out
    }

    // ------------------------------------------------------------ values

    var null: JSValue { JSValue(nullIn: context) }

    func string(_ s: String?) -> JSValue {
        guard let s = s else { return null }
        return JSValue(object: s, in: context)
    }

    func number(_ d: Double) -> JSValue { JSValue(double: d, in: context) }

    func bool(_ b: Bool) -> JSValue { JSValue(bool: b, in: context) }

    /// A fresh Uint8Array holding a copy of `bytes`.
    func bytes(_ bytes: [UInt8]) -> JSValue {
        let ref = context.jsGlobalContextRef
        var exc: JSValueRef?
        guard let obj = JSObjectMakeTypedArray(ref, kJSTypedArrayTypeUint8Array, bytes.count, &exc), exc == nil else {
            return null
        }
        if !bytes.isEmpty, let ptr = JSObjectGetTypedArrayBytesPtr(ref, obj, &exc) {
            bytes.withUnsafeBytes { src in _ = memcpy(ptr, src.baseAddress!, bytes.count) }
        }
        return JSValue(jsValueRef: obj, in: context)
    }

    func object(_ pairs: [(String, JSValue)]) -> JSValue {
        let o = JSValue(newObjectIn: context)!
        for (k, v) in pairs { o.setObject(v, forKeyedSubscript: k as NSString) }
        return o
    }

    // ------------------------------------------------------------- calls

    /// Calls MajlisNative[name](...args). Returns nil if the call threw.
    func call(_ name: String, _ args: [Any]) -> JSValue? {
        let before = faults
        guard let f = fns[name] else { return nil }
        let r = f.call(withArguments: args)
        if faults != before { return nil }
        return r
    }

    func diagnostics() -> [String] {
        guard let d = call("takeDiagnostics", []), let arr = d.toArray() else { return [] }
        return arr.compactMap { ($0 as? String).map(Engine.label) }
    }
}

/// An HTTP-shaped result from the adapter: status 0 means "continue".
struct Reply {
    let status: Int
    let body: [UInt8]
    let replayed: Int
    let code: String?
    let raw: JSValue?

    init?(_ v: JSValue?) {
        guard let v = v, v.isObject else { return nil }
        status = Int(v.objectForKeyedSubscript("status").toInt32())
        if let b = v.objectForKeyedSubscript("body"), b.isString {
            body = Array(b.toString().utf8)
        } else {
            body = []
        }
        let rp = v.objectForKeyedSubscript("replayed")
        replayed = (rp?.isNumber ?? false) ? Int(rp!.toInt32()) : -1
        let c = v.objectForKeyedSubscript("code")
        code = (c?.isString ?? false) ? c!.toString() : nil
        raw = v
    }

    func field(_ name: String) -> JSValue? { raw?.objectForKeyedSubscript(name) }
}
