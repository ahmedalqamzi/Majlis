// L01A local JavaScriptCore parity runner (macOS CLI, test tool only).
//
// Evaluates exactly three classic scripts in one JSContext (generated
// Unicode tables, majlis-core.js, trace-runner.js), injects no native
// functions, then feeds every line of IN.jsonl to MajlisTraceRunner.runLine
// and writes the returned line to OUT.jsonl. No network, files or clocks are
// reachable from JS.
//
// Usage: jsc_runner TABLES.js CORE.js RUNNER.js IN.jsonl OUT.jsonl

import Foundation
import JavaScriptCore

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(2)
}

let args = CommandLine.arguments
guard args.count == 6 else { fail("usage: jsc_runner TABLES.js CORE.js RUNNER.js IN.jsonl OUT.jsonl") }

guard let context = JSContext() else { fail("no JSContext") }
var scriptError: String?
context.exceptionHandler = { _, exception in
    scriptError = exception?.toString() ?? "unknown exception"
}

for path in args[1...3] {
    guard let source = try? String(contentsOfFile: path, encoding: .utf8) else { fail("cannot read \(path)") }
    context.evaluateScript(source, withSourceURL: URL(fileURLWithPath: path))
    if let err = scriptError { fail("script \(path): \(err)") }
}

// Evidence that the engine exposes no host APIs to the core.
let probe = context.evaluateScript("""
  JSON.stringify(["window","document","setTimeout","setInterval","fetch","XMLHttpRequest","WebSocket",
    "require","process","crypto","localStorage","navigator","Date"].map(function (n) {
      return [n, typeof globalThis[n]]; }))
""")?.toString() ?? "?"
FileHandle.standardError.write("globals: \(probe)\n".data(using: .utf8)!)

guard let runner = context.objectForKeyedSubscript("MajlisTraceRunner"),
      let runLine = runner.objectForKeyedSubscript("runLine"), !runLine.isUndefined else {
    fail("MajlisTraceRunner.runLine missing")
}

guard let input = FileManager.default.contents(atPath: args[4]) else { fail("cannot read \(args[4])") }
FileManager.default.createFile(atPath: args[5], contents: nil)
guard let out = FileHandle(forWritingAtPath: args[5]) else { fail("cannot write \(args[5])") }

var count = 0
let started = Date()
var buffer = Data()
input.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
    var start = 0
    let n = raw.count
    while start < n {
        var end = start
        while end < n && raw[end] != 0x0A { end += 1 }
        if end > start {
            autoreleasepool {
                let line = String(decoding: UnsafeRawBufferPointer(rebasing: raw[start..<end]), as: UTF8.self)
                let result = runLine.call(withArguments: [line])
                if let err = scriptError { fail("line \(count + 1): \(err)") }
                buffer.append(result!.toString().data(using: .utf8)!)
                buffer.append(0x0A)
                if buffer.count > 1 << 20 { out.write(buffer); buffer.removeAll(keepingCapacity: true) }
            }
            count += 1
        }
        start = end + 1
    }
}
out.write(buffer)
out.closeFile()
FileHandle.standardError.write(String(format: "lines: %d seconds: %.2f\n", count,
                                      Date().timeIntervalSince(started)).data(using: .utf8)!)
