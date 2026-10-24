import Foundation

// How the app loads the game page. Every launch and every Try again asks the server first (a conditional request,
// so an unchanged page costs a 304), which means a new web deploy shows up on the next launch. Only when the
// network can't be reached does it fall back to the copy saved from an earlier launch, which may be older.
// With no saved copy it shows the offline screen with Try again. The game still needs the network for
// YouTube clips, photo packs and fonts, so the saved copy is a fallback, not offline play.
enum LoadStep: Equatable {
    case network
    case savedCopy
}

enum LoadPolicy {
    static let timeout: TimeInterval = 20

    static func request(_ url: URL, _ step: LoadStep) -> URLRequest {
        URLRequest(url: url,
                   cachePolicy: step == .network ? .reloadRevalidatingCacheData : .returnCacheDataDontLoad,
                   timeoutInterval: timeout)
    }

    enum Next: Equatable {
        case ignore
        case trySavedCopy
        case showOffline
    }

    // Errors that mean "no usable network", where a saved copy is worth trying.
    static let offlineCodes: Set<Int> = [
        NSURLErrorNotConnectedToInternet, NSURLErrorNetworkConnectionLost, NSURLErrorTimedOut,
        NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost, NSURLErrorDNSLookupFailed,
        NSURLErrorDataNotAllowed, NSURLErrorInternationalRoamingOff, NSURLErrorCallIsActive,
        NSURLErrorSecureConnectionFailed,
    ]

    static func next(after error: Error, during step: LoadStep) -> Next {
        let e = error as NSError
        if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return .ignore }
        if step == .network && e.domain == NSURLErrorDomain && offlineCodes.contains(e.code) { return .trySavedCopy }
        return .showOffline
    }

    // The offline screen's Try again link; the app catches it and starts a fresh network load.
    static let retryURL = URL(string: "majlis-retry:again")!
}
