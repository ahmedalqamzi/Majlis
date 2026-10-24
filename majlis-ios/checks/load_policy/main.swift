// Checks the launch/retry decisions in Majlis/LoadPolicy.swift without Xcode or a simulator:
//   swiftc majlis-ios/Majlis/LoadPolicy.swift majlis-ios/checks/load_policy/main.swift -o /tmp/load_policy && /tmp/load_policy
import Foundation

var failures = 0
func check(_ ok: Bool, _ what: String) {
    print(ok ? "ok   " : "FAIL ", what)
    if !ok { failures += 1 }
}
func urlError(_ code: Int) -> Error { NSError(domain: NSURLErrorDomain, code: code) }

let game = URL(string: "https://ahmedalqamzi.github.io/Majlis/")!

// Launch and Try again always ask the server (conditional request), never "cache first".
let fresh = LoadPolicy.request(game, .network)
check(fresh.cachePolicy == .reloadRevalidatingCacheData, "network step revalidates with the server")
check(fresh.cachePolicy != .returnCacheDataElseLoad, "network step no longer prefers stale cache")
check(fresh.url == game && fresh.timeoutInterval == LoadPolicy.timeout, "network step targets the game URL with the idle timeout")
check(LoadPolicy.request(game, .savedCopy).cachePolicy == .returnCacheDataDontLoad, "saved-copy step never touches the network")

// No network on launch: try the saved copy once, then the offline screen.
for code in [NSURLErrorNotConnectedToInternet, NSURLErrorTimedOut, NSURLErrorCannotFindHost,
             NSURLErrorNetworkConnectionLost, NSURLErrorDataNotAllowed] {
    check(LoadPolicy.next(after: urlError(code), during: .network) == .trySavedCopy, "network error \(code) falls back to the saved copy")
    check(LoadPolicy.next(after: urlError(code), during: .savedCopy) == .showOffline, "error \(code) on the saved copy shows Try again")
}
check(LoadPolicy.next(after: urlError(NSURLErrorResourceUnavailable), during: .savedCopy) == .showOffline,
      "no saved copy (first launch offline) shows Try again")
check(LoadPolicy.next(after: urlError(NSURLErrorBadServerResponse), during: .network) == .showOffline,
      "non-connectivity network error shows Try again instead of a stale copy")
check(LoadPolicy.next(after: NSError(domain: "WebKitErrorDomain", code: 101), during: .network) == .showOffline,
      "WebKit error shows Try again")

// A cancelled load (new navigation replaced it) changes nothing.
check(LoadPolicy.next(after: urlError(NSURLErrorCancelled), during: .network) == .ignore, "cancelled network load is ignored")
check(LoadPolicy.next(after: urlError(NSURLErrorCancelled), during: .savedCopy) == .ignore, "cancelled saved-copy load is ignored")

// Try again goes through the app (fresh network load), not through the web page's own cache rules.
check(LoadPolicy.retryURL.scheme == "majlis-retry" && LoadPolicy.retryURL.host == nil, "retry link uses the app-only scheme")

print(failures == 0 ? "all load policy checks passed" : "\(failures) load policy check(s) failed")
exit(failures == 0 ? 0 : 1)
