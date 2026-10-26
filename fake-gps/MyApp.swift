import SwiftUI
import AppKit
import UserNotifications
import Network

@main
struct iOSLocationControllerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .frame(minWidth: 1050, minHeight: 720)
                .onAppear {
                    appDelegate.attachWindowCloseHandler()
                }
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, UNUserNotificationCenterDelegate {
    var pythonProcess: Process?
    static let sessionToken = UUID().uuidString
    static var serverPort: Int = 8000
    static var notifyPort: Int = 8001
    private var notifyListener: NWListener?

    // 🎯 取得作業系統當前可用的空閒 Port
    static func findFreePort() -> Int {
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.stride)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")

        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return 8000 }
        defer { Darwin.close(sock) }

        var bindAddr = sockaddr()
        memcpy(&bindAddr, &addr, Int(addr.sin_len))

        if Darwin.bind(sock, &bindAddr, socklen_t(addr.sin_len)) == 0 {
            var len = socklen_t(addr.sin_len)
            if Darwin.getsockname(sock, &bindAddr, &len) == 0 {
                var actualAddr = sockaddr_in()
                memcpy(&actualAddr, &bindAddr, Int(len))
                let port = Int(UInt16(bigEndian: actualAddr.sin_port))
                return port > 0 ? port : 8000
            }
        }
        return 8000
    }

    // 🎯 以 App 自身名義發送原生系統通知
    static func sendAppNotification(title: String, subtitle: String = "", body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        if !subtitle.isEmpty { content.subtitle = subtitle }
        content.body = body
        content.sound = .default

        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error = error {
                print("❌ 原生通知發送失敗: \(error)")
            }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 設定通知委派，使 App 在前景時也能正常顯示橫幅與播放聲音
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            if granted {
                print("✅ 已取得 macOS 原生通知授權")
            }
        }

        // 啟動 Swift 內部的原生通知監聽服務
        startNativeNotificationListener()
        startBackendServer()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        notifyListener?.cancel()
        if let process = pythonProcess, process.isRunning {
            process.terminate()
            print("🛑 原生 Python 後端已安全退出")
        }
    }

    func attachWindowCloseHandler() {
        DispatchQueue.main.async {
            if let window = NSApplication.shared.windows.first(where: { $0.canBecomeMain }) {
                window.delegate = self
            }
        }
    }

    // 🎯 接收來自 Python 後端直接發送的原生通知請求
    func startNativeNotificationListener() {
        AppDelegate.notifyPort = AppDelegate.findFreePort()
        guard let port = NWEndpoint.Port(rawValue: UInt16(AppDelegate.notifyPort)) else { return }

        do {
            notifyListener = try NWListener(using: .tcp, on: port)
            notifyListener?.newConnectionHandler = { connection in
                connection.start(queue: .main)
                connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, _, _ in
                    if let data = data, let text = String(data: data, encoding: .utf8) {
                        // 解析 HTTP 請求體中的 JSON
                        if let jsonStart = text.range(of: "\r\n\r\n") {
                            let jsonString = String(text[jsonStart.upperBound...])
                            if let jsonData = jsonString.data(using: .utf8),
                               let dict = try? JSONSerialization.jsonObject(with: jsonData) as? [String: String] {
                                let title = dict["title"] ?? "SIM-GPS"
                                let subtitle = dict["subtitle"] ?? ""
                                let message = dict["message"] ?? ""
                                AppDelegate.sendAppNotification(title: title, subtitle: subtitle, body: message)
                            }
                        }
                    }
                    let response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
                    connection.send(content: response.data(using: .utf8), completion: .contentProcessed({ _ in
                        connection.cancel()
                    }))
                }
            }
            notifyListener?.start(queue: .main)
            print("🔔 原生通知監聽端點已啟動於 Port: \(AppDelegate.notifyPort)")
        } catch {
            print("❌ 通知監聽端點啟動失敗: \(error)")
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/api/status?token=\(AppDelegate.sessionToken)") else {
            return true
        }

        var isSimulating = false
        let semaphore = DispatchSemaphore(value: 0)
        var req = URLRequest(url: url)
        req.timeoutInterval = 0.5

        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sim = json["is_simulating"] as? Bool {
                isSimulating = sim
            }
            semaphore.signal()
        }.resume()

        _ = semaphore.wait(timeout: .now() + 0.5)

        if !isSimulating {
            return true
        }

        let alert = NSAlert()
        alert.messageText = "確定要結束並關閉程式嗎？"
        alert.informativeText = "目前 iPhone 仍處於虛擬定位狀態。關閉程式將會自動為您平滑恢復真實 GPS 定位。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "恢復定位並退出")
        alert.addButton(withTitle: "取消")

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            if let stopUrl = URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/api/stop-smooth?token=\(AppDelegate.sessionToken)") {
                var stopReq = URLRequest(url: stopUrl)
                stopReq.httpMethod = "POST"
                stopReq.timeoutInterval = 5.0
                let stopSem = DispatchSemaphore(value: 0)
                URLSession.shared.dataTask(with: stopReq) { _, _, _ in
                    stopSem.signal()
                }.resume()
                _ = stopSem.wait(timeout: .now() + 5.0)
            }
            return true
        } else {
            return false
        }
    }

    func startBackendServer() {
        AppDelegate.serverPort = AppDelegate.findFreePort()
        print("🎯 分配空閒 Port: \(AppDelegate.serverPort)")

        guard let resourcePath = Bundle.main.resourcePath else { return }

        let fm = FileManager.default
        let candidatePaths = [
            "\(resourcePath)/EmbeddedPython/python/bin/python3",
            "\(resourcePath)/EmbeddedPython/bin/python3",
            "\(resourcePath)/python/bin/python3"
        ]

        var validPythonPath: String?
        for path in candidatePaths {
            if fm.fileExists(atPath: path) {
                validPythonPath = path
                break
            }
        }

        guard let pythonPath = validPythonPath else { return }

        let scriptPath = "\(resourcePath)/app.py"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        // 傳遞參數：app.py <SERVER_PORT> <SESSION_TOKEN> <NOTIFY_PORT>
        process.arguments = [
            scriptPath,
            String(AppDelegate.serverPort),
            AppDelegate.sessionToken,
            String(AppDelegate.notifyPort)
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: resourcePath)

        var env = ProcessInfo.processInfo.environment
        env["PYMOBILEDEVICE3_USERSPACE_TUNNEL"] = "1"
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { fileHandle in
            let data = fileHandle.availableData
            if let output = String(data: data, encoding: .utf8), !output.isEmpty {
                print("[Python] \(output.trimmingCharacters(in: .whitespacesAndNewlines))")
            }
        }

        try? process.run()
        self.pythonProcess = process
    }
}
