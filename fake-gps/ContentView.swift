import SwiftUI
import WebKit
import CoreLocation
import Combine
import UserNotifications

class LocationManager: NSObject, ObservableObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()
    @Published var userLocation: CLLocationCoordinate2D?

    override init() {
        super.init()
        manager.delegate = self
        manager.desiredAccuracy = kCLLocationAccuracyBest
        
        // 1. 請求本機真實 GPS 定位授權
        manager.requestWhenInUseAuthorization()
        manager.startUpdatingLocation()
        
        // 2. 同步向系統請求 App 原生通知授權 (橫幅、提示音、標記)
        requestNotificationPermission()
    }

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            DispatchQueue.main.async {
                if granted {
                    print("✅ macOS 原生通知授權已取得")
                } else if let error = error {
                    print("❌ 請求通知授權失敗: \(error.localizedDescription)")
                } else {
                    print("⚠️ 使用者拒絕了通知授權")
                }
            }
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let loc = locations.last else { return }
        DispatchQueue.main.async {
            self.userLocation = loc.coordinate
            print("📍 原生 CoreLocation 成功取得位置: \(loc.coordinate.latitude), \(loc.coordinate.longitude)")
        }
        manager.stopUpdatingLocation()
    }

    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        print("❌ CoreLocation 取得位置失敗: \(error.localizedDescription)")
    }
}

class WebViewModel: NSObject, ObservableObject, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var webView: WKWebView?
    var initialCoords: CLLocationCoordinate2D?

    @available(macOS 12.0, *)
    func webView(_ webView: WKWebView, requestGeolocationPermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(.grant)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        self.webView = webView
        injectLocation()
    }

    func updateCoords(_ coords: CLLocationCoordinate2D?) {
        self.initialCoords = coords
        injectLocation()
    }

    private func injectLocation() {
        guard let webView = self.webView, let coords = self.initialCoords else { return }
        let js = """
            if (typeof updateInitialLocation === 'function') {
                updateInitialLocation(\(coords.latitude), \(coords.longitude));
            }
        """
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    // 🎯 接收來自前端發出的原生通知指令
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "nativeNotify", let body = message.body as? [String: String] {
            let title = body["title"] ?? "SIM-GPS"
            let subtitle = body["subtitle"] ?? ""
            let msg = body["message"] ?? ""
            AppDelegate.sendAppNotification(title: title, subtitle: subtitle, body: msg)
        }
    }
}

struct WebView: NSViewRepresentable {
    let url: URL
    let coords: CLLocationCoordinate2D?
    @ObservedObject var model: WebViewModel

    func makeNSView(context: Context) -> WKWebView {
        let prefs = WKWebpagePreferences()
        prefs.allowsContentJavaScript = true

        let userContentController = WKUserContentController()
        // 註冊 nativeNotify 通道
        userContentController.add(model, name: "nativeNotify")

        let config = WKWebViewConfiguration()
        config.userContentController = userContentController
        config.defaultWebpagePreferences = prefs

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = model
        webView.navigationDelegate = model
        model.webView = webView
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {
        if nsView.url == nil {
            let request = URLRequest(url: url)
            nsView.load(request)
        }
        model.updateCoords(coords)
    }
}

struct ContentView: View {
    @StateObject private var locationManager = LocationManager()
    @StateObject private var webViewModel = WebViewModel()
    @State private var isServerReady = false
    let token = AppDelegate.sessionToken

    var body: some View {
        ZStack {
            if isServerReady {
                WebView(
                    url: URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/?token=\(token)")!,
                    coords: locationManager.userLocation,
                    model: webViewModel
                )
            } else {
                VStack(spacing: 14) {
                    ProgressView()
                    Text("正在啟動 iOS 定位後端服務...")
                        .font(.headline)
                        .foregroundColor(.secondary)
                    Text("正在同步本機真實位置中...")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .frame(minWidth: 400, minHeight: 300)
            }
        }
        .onAppear {
            checkServerReady()
        }
    }

    func checkServerReady() {
        Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { timer in
            let port = AppDelegate.serverPort
            guard let url = URL(string: "http://127.0.0.1:\(port)/api/init?token=\(token)") else { return }

            var req = URLRequest(url: url)
            req.timeoutInterval = 0.5

            let task = URLSession.shared.dataTask(with: req) { _, response, _ in
                if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                    DispatchQueue.main.async {
                        self.isServerReady = true
                        timer.invalidate()
                    }
                }
            }
            task.resume()
        }
    }
}
