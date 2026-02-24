# AuroranHealth iOS WebView

This is a minimal SwiftUI wrapper around the web dashboard using `WKWebView`.

## Setup
- Open this folder in Xcode and create a new iOS App target.
- Add `AuroranHealthApp.swift`, `ContentView.swift`, and `WebView.swift` to the target.
- Update the URL in `ContentView.swift` if needed.

## Notes
- Uses the default `WKWebsiteDataStore` so session cookies persist.
- Back/forward/reload controls are provided in the bottom toolbar.
