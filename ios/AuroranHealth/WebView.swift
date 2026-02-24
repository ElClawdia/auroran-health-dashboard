import SwiftUI
import WebKit

final class WebViewStore: NSObject, ObservableObject, WKNavigationDelegate {
    @Published var isLoading = false
    @Published var estimatedProgress: Double = 0
    let webView: WKWebView

    private var progressObservation: NSKeyValueObservation?
    private var loadingObservation: NSKeyValueObservation?

    init(url: URL) {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        self.webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()

        webView.navigationDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.load(URLRequest(url: url))

        progressObservation = webView.observe(\.estimatedProgress, options: [.new]) { [weak self] _, change in
            self?.estimatedProgress = change.newValue ?? 0
        }
        loadingObservation = webView.observe(\.isLoading, options: [.new]) { [weak self] _, change in
            self?.isLoading = change.newValue ?? false
        }
    }
}

struct WebView: UIViewRepresentable {
    let webView: WKWebView

    func makeUIView(context: Context) -> WKWebView {
        webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
