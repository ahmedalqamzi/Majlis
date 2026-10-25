// Exercises the real getifaddrs lookup in Majlis/Party/InterfaceAddress.swift
// on whatever machine runs this check (en0 is Wi-Fi on both iOS and this
// Mac). It proves the parsing/filtering logic against a real interface
// list; it cannot prove iOS's actual Wi-Fi-off or no-IP behavior, which
// needs a physical device (see L02A report, physical-device items).
//   swiftc majlis-ios/Majlis/Party/InterfaceAddress.swift majlis-ios/checks/interface_address/main.swift -o /tmp/interface_address && /tmp/interface_address
import Foundation

var failures = 0
func check(_ ok: Bool, _ what: String) {
    print(ok ? "ok   " : "FAIL ", what)
    if !ok { failures += 1 }
}

let addr = InterfaceAddress.currentWiFiIPv4()
print("en0 IPv4 address on this machine: \(addr ?? "none")")
check(InterfaceAddress.wifiInterfaceName == "en0", "Wi-Fi interface name is en0, not a heuristic over address ranges")
if let a = addr {
    let parts = a.split(separator: ".")
    check(parts.count == 4 && parts.allSatisfy { $0.allSatisfy(\.isNumber) && (UInt16($0) ?? 999) <= 255 },
          "returned address is a well-formed dotted-quad IPv4 address")
    check(a != "127.0.0.1", "never returns the loopback address")
} else {
    check(true, "no en0 IPv4 address on this machine right now (acceptable: Wi-Fi may be off here)")
}

print(failures == 0 ? "all interface address checks passed" : "\(failures) interface address check(s) failed")
exit(failures == 0 ? 0 : 1)
