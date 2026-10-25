import Foundation

// The device's own current Wi-Fi IPv4 address, for binding the native host
// and building the join address/QR code. No Bonjour/mDNS and no guessing:
// just "what IPv4 address does en0 have right now", read fresh every time a
// caller asks (interface changes are not cached).
enum InterfaceAddress {
    /// iOS's Wi-Fi interface name. Cellular is pdp_ip0/pdp_ip1/...; a
    /// Personal Hotspot/USB/wired accessory interface is never en0 either,
    /// so filtering on this name is how "Wi-Fi only" is enforced, not a
    /// heuristic over the address range.
    static let wifiInterfaceName = "en0"

    /// nil when there is no Wi-Fi interface, or it has no IPv4 address
    /// (Wi-Fi off, not associated, IPv6-only network).
    static func currentWiFiIPv4() -> String? {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return nil }
        defer { freeifaddrs(first) }
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let ifa = cursor {
            defer { cursor = ifa.pointee.ifa_next }
            let flags = Int32(ifa.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0 else { continue }
            guard let addr = ifa.pointee.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET) else { continue }
            guard String(cString: ifa.pointee.ifa_name) == wifiInterfaceName else { continue }
            var buf = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN))
            let ok = addr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                getnameinfo(sa, socklen_t(sa.pointee.sa_len), &buf, socklen_t(buf.count), nil, 0, NI_NUMERICHOST) == 0
            }
            if ok { return String(cString: buf) }
        }
        return nil
    }
}
