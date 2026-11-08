import SwiftUI
import WebKit
import AppKit
import UniformTypeIdentifiers
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

    // 🎯 支援 JavaScript alert() 原生彈窗
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "系統提示"
        alert.informativeText = message
        alert.alertStyle = .informational
        alert.addButton(withTitle: "確定")
        alert.runModal()
        completionHandler()
    }

    // 🎯 支援 JavaScript confirm() 原生確認彈窗
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "操作確認"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "確定")
        alert.addButton(withTitle: "取消")
        let res = alert.runModal()
        completionHandler(res == .alertFirstButtonReturn)
    }

    // 🎯 支援 JavaScript prompt() 原生文字輸入彈窗 (解決 WKWebView 預設忽略 prompt 導致無法輸入路標/路線名稱的問題)
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.alertStyle = .informational
        alert.addButton(withTitle: "確定")
        alert.addButton(withTitle: "取消")

        let inputField = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        inputField.stringValue = defaultText ?? ""
        alert.accessoryView = inputField
        alert.window.initialFirstResponder = inputField

        let res = alert.runModal()
        if res == .alertFirstButtonReturn {
            completionHandler(inputField.stringValue)
        } else {
            completionHandler(nil)
        }
    }

    // 🎯 支援 HTML <input type="file"> 原生檔案選擇器 (解決 WKWebView 點擊匯入無反應或讀不到檔案的問題)
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let openPanel = NSOpenPanel()
        openPanel.canChooseFiles = true
        openPanel.canChooseDirectories = parameters.allowsDirectories
        openPanel.allowsMultipleSelection = parameters.allowsMultipleSelection
        openPanel.prompt = "選擇匯入檔案"
        openPanel.message = "請選擇要匯入的 GPX / JSON / GeoJSON / CSV 檔案"
        if openPanel.runModal() == .OK {
            completionHandler(openPanel.urls)
        } else {
            completionHandler(nil)
        }
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

    // 🎯 接收來自前端發出的原生通知與原生檔案儲存/開啟指令
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "nativeNotify", let body = message.body as? [String: String] {
            let title = body["title"] ?? "SIM-GPS"
            let subtitle = body["subtitle"] ?? ""
            let msg = body["message"] ?? ""
            AppDelegate.sendAppNotification(title: title, subtitle: subtitle, body: msg)
        } else if message.name == "nativeFileBridge", let body = message.body as? [String: Any] {
            let action = body["action"] as? String ?? ""
            if action == "saveFile" {
                let filename = body["filename"] as? String ?? "export.json"
                let content = body["content"] as? String ?? ""
                DispatchQueue.main.async {
                    NSApp.activate(ignoringOtherApps: true)
                    let savePanel = NSSavePanel()
                    savePanel.title = "選擇匯出檔案儲存位置"
                    savePanel.message = "請選擇要匯出儲存的資料夾位置與檔案名稱"
                    savePanel.nameFieldStringValue = filename
                    savePanel.canCreateDirectories = true
                    savePanel.isExtensionHidden = false
                    savePanel.prompt = "匯出儲存"
                    let response = savePanel.runModal()
                    if response == .OK, let targetURL = savePanel.url {
                        do {
                            try content.write(to: targetURL, atomically: true, encoding: .utf8)
                            let safePath = targetURL.path.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "'", with: "\\'")
                            self.webView?.evaluateJavaScript("if(window.onNativeFileSaved) window.onNativeFileSaved(true, '\(safePath)');", completionHandler: nil)
                        } catch {
                            let errStr = error.localizedDescription.replacingOccurrences(of: "'", with: "\\'")
                            self.webView?.evaluateJavaScript("if(window.onNativeFileSaved) window.onNativeFileSaved(false, '\(errStr)');", completionHandler: nil)
                        }
                    } else {
                        self.webView?.evaluateJavaScript("if(window.onNativeFileSaved) window.onNativeFileSaved(false, '__CANCELLED__');", completionHandler: nil)
                    }
                }
            } else if action == "restartBackend" {
                DispatchQueue.main.async {
                    AppDelegate.shared?.startBackendServer()
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                        if let newURL = URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/?token=\(AppDelegate.sessionToken)") {
                            self.webView?.load(URLRequest(url: newURL))
                        }
                    }
                }
            } else if action == "openExternalUrl", let urlStr = body["url"] as? String, let extURL = URL(string: urlStr) {
                DispatchQueue.main.async {
                    NSWorkspace.shared.open(extURL)
                }
            }
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
        userContentController.add(model, name: "nativeNotify")
        userContentController.add(model, name: "nativeFileBridge")

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
    @State private var startupError: String? = nil
    @State private var pollAttempts: Int = 0
    let token = AppDelegate.sessionToken

    var body: some View {
        ZStack {
            if isServerReady {
                WebView(
                    url: URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/?token=\(token)")!,
                    coords: locationManager.userLocation,
                    model: webViewModel
                )
            } else if let err = startupError {
                VStack(spacing: 14) {
                    Text("⚠️ 後端定位服務啟動失敗")
                        .font(.title3)
                        .fontWeight(.bold)
                        .foregroundColor(.red)
                    Text(err)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(.secondary)
                        .multilineTextAlignment(.leading)
                        .padding(12)
                        .frame(maxWidth: 560)
                        .background(Color.gray.opacity(0.12))
                        .cornerRadius(8)
                    Button("🔄 重新啟動後端服務") {
                        startupError = nil
                        pollAttempts = 0
                        AppDelegate.shared?.startBackendServer()
                        checkServerReady()
                    }
                    .buttonStyle(.borderedProminent)
                }
                .padding(24)
                .frame(minWidth: 500, minHeight: 340)
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
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                checkServerReady()
            }
        }
    }

    func checkServerReady() {
        Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { timer in
            pollAttempts += 1
            let port = AppDelegate.serverPort

            if let proc = AppDelegate.shared?.pythonProcess, !proc.isRunning, pollAttempts > 3 {
                DispatchQueue.main.async {
                    let logTail = AppDelegate.lastBackendOutput.isEmpty ? "Python 進程異常退出 (Exit code: \(proc.terminationStatus))" : AppDelegate.lastBackendOutput
                    self.startupError = logTail
                    timer.invalidate()
                }
                return
            }

            if pollAttempts > 40 {
                DispatchQueue.main.async {
                    let logTail = AppDelegate.lastBackendOutput.isEmpty ? "等待本地後端伺服器回應超時 (超過 16 秒)" : AppDelegate.lastBackendOutput
                    self.startupError = logTail
                    timer.invalidate()
                }
                return
            }

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
