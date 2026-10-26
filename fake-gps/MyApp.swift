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
    static weak var shared: AppDelegate?
    static let sessionToken = UUID().uuidString
    static var serverPort: Int = 8000
    static var notifyPort: Int = 8001
    static var lastBackendOutput: String = ""
    private var notifyListener: NWListener?
    private var isConfirmedTerminating = false

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
        AppDelegate.shared = self
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            if granted {
                print("✅ 已取得 macOS 原生通知授權")
            }
        }

        startNativeNotificationListener()
        startBackendServer()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    // 🎯 統一處理視窗關閉與 Cmd+Q 退出：支援「依設定速度平滑返航」或「立即清除模擬定位並退出」
    func confirmAndHandleExit() -> Bool {
        if isConfirmedTerminating {
            return true
        }
        guard let url = URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/api/status?token=\(AppDelegate.sessionToken)") else {
            return true
        }

        var isSimulating = false
        let semaphore = DispatchSemaphore(value: 0)
        var req = URLRequest(url: url)
        req.timeoutInterval = 0.6

        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sim = json["is_simulating"] as? Bool {
                isSimulating = sim
            }
            semaphore.signal()
        }.resume()

        _ = semaphore.wait(timeout: .now() + 0.6)

        if !isSimulating {
            isConfirmedTerminating = true
            return true
        }

        let alert = NSAlert()
        alert.messageText = "確定要結束並關閉程式嗎？"
        alert.informativeText = "目前 iPhone 仍處於虛擬定位狀態。您可以選擇立即還原真實 GPS 定位並退出，或在畫面中啟動「依設定速度平滑返航」。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "立即還原真實定位並退出")
        alert.addButton(withTitle: "留在本頁並依設定速度返航")
        alert.addButton(withTitle: "取消")

        let response = alert.runModal()
        if response == .alertFirstButtonReturn {
            isConfirmedTerminating = true
            if let stopUrl = URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/api/stop-force?token=\(AppDelegate.sessionToken)") {
                var stopReq = URLRequest(url: stopUrl)
                stopReq.httpMethod = "POST"
                stopReq.timeoutInterval = 3.0
                let stopSem = DispatchSemaphore(value: 0)
                URLSession.shared.dataTask(with: stopReq) { _, _, _ in
                    stopSem.signal()
                }.resume()
                _ = stopSem.wait(timeout: .now() + 3.0)
            }
            return true
        } else if response == .alertSecondButtonReturn {
            if let smoothUrl = URL(string: "http://127.0.0.1:\(AppDelegate.serverPort)/api/stop-smooth?token=\(AppDelegate.sessionToken)") {
                var smoothReq = URLRequest(url: smoothUrl)
                smoothReq.httpMethod = "POST"
                URLSession.shared.dataTask(with: smoothReq).resume()
            }
            return false
        } else {
            return false
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        return confirmAndHandleExit()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        return confirmAndHandleExit() ? .terminateNow : .terminateCancel
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

    func startBackendServer() {
        if let existing = pythonProcess, existing.isRunning {
            existing.terminate()
        }
        AppDelegate.lastBackendOutput = ""
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

        guard let pythonPath = validPythonPath else {
            AppDelegate.lastBackendOutput = "找不到內嵌的 EmbeddedPython/python/bin/python3 執行檔"
            return
        }

        let scriptPath = "\(resourcePath)/app.py"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [
            scriptPath,
            String(AppDelegate.serverPort),
            AppDelegate.sessionToken,
            String(AppDelegate.notifyPort)
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: resourcePath)

        // 🎯 隔離使用者外部環境變數污染 (避免外部 Homebrew/Anaconda 的 PYTHONPATH 干擾內嵌環境)
        var env = ProcessInfo.processInfo.environment
        env.removeValue(forKey: "PYTHONPATH")
        env.removeValue(forKey: "PYTHONHOME")
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYMOBILEDEVICE3_USERSPACE_TUNNEL"] = "1"
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { fileHandle in
            let data = fileHandle.availableData
            if let output = String(data: data, encoding: .utf8), !output.isEmpty {
                let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
                print("[Python] \(trimmed)")
                DispatchQueue.main.async {
                    AppDelegate.lastBackendOutput = String((AppDelegate.lastBackendOutput + "\n" + trimmed).suffix(1200))
                }
            }
        }

        do {
            try process.run()
            self.pythonProcess = process
        } catch {
            AppDelegate.lastBackendOutput = "啟動 Python 子進程失敗: \(error.localizedDescription)"
        }
    }
}
