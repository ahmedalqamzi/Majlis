import SwiftUI
import WebKit

// Majlis for iPhone and iPad: a full-screen web view on the published web game.
// YouTube clips play inline without a tap, which a home-screen web app on iOS can't do.
let gameURL = URL(string: "https://ahmedalqamzi.github.io/majlis/")!
let navy = UIColor(red: 13 / 255, green: 39 / 255, blue: 66 / 255, alpha: 1)

@main
struct MajlisApp: App {
    var body: some Scene {
        WindowGroup {
            GameView()
                .ignoresSafeArea(.container, edges: .bottom)
                .background(Color(navy).ignoresSafeArea())
                .preferredColorScheme(.dark)
                .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
        }
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
        web.load(URLRequest(url: gameURL, cachePolicy: .returnCacheDataElseLoad))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        // Links that open a new window (Open on YouTube, sources) go to Safari or the YouTube app.
        func webView(_ web: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { UIApplication.shared.open(url) }
            return nil
        }

        // Keep the game itself in the app; anything else on another site opens outside it.
        func webView(_ web: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if let url = action.request.url, action.targetFrame?.isMainFrame == true,
               let host = url.host, host != gameURL.host, url.scheme?.hasPrefix("http") == true {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        func webView(_ web: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
            showOffline(web)
        }

        func webView(_ web: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
            if (error as NSError).code != NSURLErrorCancelled { showOffline(web) }
        }

        private func showOffline(_ web: WKWebView) {
            let html = """
            <meta name=viewport content="width=device-width,initial-scale=1">
            <body style="margin:0;height:100vh;display:grid;place-items:center;background:#0D2742;color:#fff;font:600 18px -apple-system;text-align:center">
            <div><p style="font-size:34px;font-weight:900;margin:0 0 8px">Majlis</p><p>Majlis needs an internet connection.</p>
            <button onclick="location.href='\(gameURL.absoluteString)'" style="margin-top:12px;font:800 18px -apple-system;padding:14px 28px;border:0;background:#FFC83D;color:#0D2742">Try again</button></div>
            """
            web.loadHTMLString(html, baseURL: nil)
        }
    }
}
