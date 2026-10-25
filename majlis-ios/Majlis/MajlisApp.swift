import SwiftUI
import WebKit

// Majlis for iPhone and iPad: a full-screen web view on the published web game.
// YouTube clips play inline without a tap, which a home-screen web app on iOS can't do.
let gameURL = URL(string: "https://ahmedalqamzi.github.io/Majlis/")!
let navy = UIColor(red: 13 / 255, green: 39 / 255, blue: 66 / 255, alpha: 1)

@main
struct MajlisApp: App {
    var body: some Scene {
        WindowGroup {
            TabView {
                GameView()
                    .ignoresSafeArea(.container, edges: .bottom)
                    .background(Color(navy).ignoresSafeArea())
                    .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
                    .tabItem { Label("Majlis", systemImage: "gamecontroller") }

                PartyTab()
                    .tabItem { Label("Party", systemImage: "person.3") }
            }
            .preferredColorScheme(.dark)
        }
    }
}

// Hosts OperatorView with real bundle paths for PartyHost's contentDir/webDir
// (the PartyContent/PartyWeb folder references project.yml bundles via
// gen_ios_content_packs.sh/gen_ios_web_resources.sh). Resolved lazily inside
// makeHost — PartyLifecycleCoordinator's init (and startHosting after an
// .ended session) calls this each time a fresh PartyHost is actually
// needed, not once at app launch.
private struct PartyTab: View {
    var body: some View {
        OperatorView(makeHost: {
            guard let contentDir = Bundle.main.url(forResource: "PartyContent", withExtension: nil)?.path,
                  let webDir = Bundle.main.url(forResource: "PartyWeb", withExtension: nil)?.path else {
                throw PartyHost.HostError.invalidConfig("bundled PartyContent/PartyWeb resources not found")
            }
            return try PartyHost(contentDir: contentDir, webDir: webDir)
        })
    }
}

struct GameView: UIViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.websiteDataStore = .default()
        if #available(iOS 16.4, *) { config.preferences.isElementFullscreenEnabled = true }
        let web = WKWebView(frame: .zero, configuration: config)
        web.isOpaque = false
        web.backgroundColor = navy
        web.scrollView.backgroundColor = navy
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.allowsBackForwardNavigationGestures = false
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.customUserAgent = nil
        context.coordinator.load(web, .network)
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        private var step = LoadStep.network

        // Ask the server for the latest game; see LoadPolicy for the saved-copy fallback.
        func load(_ web: WKWebView, _ step: LoadStep) {
            self.step = step
            web.load(LoadPolicy.request(gameURL, step))
        }

        // Links that open a new window (Open on YouTube, sources) go to Safari or the YouTube app.
        func webView(_ web: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { UIApplication.shared.open(url) }
            return nil
        }

        // Keep the game itself in the app; anything else on another site opens outside it.
        func webView(_ web: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.request.url?.scheme == LoadPolicy.retryURL.scheme {
                decisionHandler(.cancel)
                load(web, .network)
                return
            }
            if let url = action.request.url, action.targetFrame?.isMainFrame == true,
               let host = url.host, host != gameURL.host, url.scheme?.hasPrefix("http") == true {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        func webView(_ web: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
            failed(web, error)
        }

        func webView(_ web: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
            failed(web, error)
        }

        private func failed(_ web: WKWebView, _ error: Error) {
            switch LoadPolicy.next(after: error, during: step) {
            case .ignore: break
            case .trySavedCopy: load(web, .savedCopy)
            case .showOffline: showOffline(web)
            }
        }

        private func showOffline(_ web: WKWebView) {
            let html = """
            <meta name=viewport content="width=device-width,initial-scale=1">
            <body style="margin:0;height:100vh;display:grid;place-items:center;background:#0D2742;color:#fff;font:600 18px -apple-system;text-align:center">
            <div><p style="font-size:34px;font-weight:900;margin:0 0 8px">Majlis</p><p>Majlis needs an internet connection.</p>
            <button onclick="location.href='\(LoadPolicy.retryURL.absoluteString)'" style="margin-top:12px;font:800 18px -apple-system;padding:14px 28px;border:0;background:#FFC83D;color:#0D2742">Try again</button></div>
            """
            web.loadHTMLString(html, baseURL: nil)
        }
    }
}
