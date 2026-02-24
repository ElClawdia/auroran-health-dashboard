import SwiftUI

struct ContentView: View {
    private let baseURL = URL(string: "https://lobstergains.ai/")!
    @StateObject private var store: WebViewStore

    init() {
        _store = StateObject(wrappedValue: WebViewStore(url: baseURL))
    }

    var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                if store.isLoading {
                    ProgressView(value: store.estimatedProgress)
                        .progressViewStyle(.linear)
                }
                WebView(webView: store.webView)
            }
            .toolbar {
                ToolbarItemGroup(placement: .bottomBar) {
                    Button(action: { store.webView.goBack() }) {
                        Image(systemName: "chevron.left")
                    }
                    .disabled(!store.webView.canGoBack)

                    Button(action: { store.webView.goForward() }) {
                        Image(systemName: "chevron.right")
                    }
                    .disabled(!store.webView.canGoForward)

                    Spacer()

                    Button(action: { store.webView.reload() }) {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
