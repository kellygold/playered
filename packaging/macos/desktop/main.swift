import AppKit
import WebKit
import Security

final class StudioDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private var engine: Process?
    private var readyTimer: Timer?
    private var baseURL: URL?
    private var session = ""
    private var workspace: URL!
    private var home: URL!
    private var stopping = false
    private var quitChecked = false
    private var outputBuffer = Data()
    private var downloads: [ObjectIdentifier: (temporary: URL, final: URL)] = [:]
    private let name = "PLAyered"
    // Retain the existing workspace preferences and engine log location.
    private let storageName = "Image23MF Studio"
    private let files = FileManager.default

    func applicationDidFinishLaunching(_ notification: Notification) {
        let env = ProcessInfo.processInfo.environment
        home = env["IMAGE23MF_DESKTOP_TEST_HOME"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? files.homeDirectoryForCurrentUser
        setupMenus()
        setupWindow()
        do {
            workspace = try selectWorkspace()
            try startEngine()
        } catch {
            showFailure(error.localizedDescription)
        }
    }

    private func setupWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        if #available(macOS 13.3, *) {
            web.isInspectable = ProcessInfo.processInfo.environment["IMAGE23MF_DESKTOP_INSPECT"] == "1"
        }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 850),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = name
        window.minSize = NSSize(width: 800, height: 600)
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.loadHTMLString("<html><body style='font:18px -apple-system;background:#f5f7fa;color:#273746;display:grid;place-items:center;height:90vh'><p>Starting PLAyered…</p></body></html>", baseURL: nil)
    }

    private func setupMenus() {
        let bar = NSMenu()
        let application = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About \(name)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(name)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        application.submenu = appMenu
        bar.addItem(application)
        let edit = NSMenuItem()
        edit.title = "Edit"
        let editMenu = NSMenu(title: "Edit")
        for (title, selector, key) in [("Undo", "undo:", "z"), ("Cut", "cut:", "x"),
            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(selector), keyEquivalent: key)
        }
        let redo = NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.insertItem(redo, at: 1)
        edit.submenu = editMenu
        bar.addItem(edit)
        let project = NSMenuItem()
        project.title = "Project"
        let projectMenu = NSMenu(title: "Project")
        let reveal = projectMenu.addItem(withTitle: "Show Workspace in Finder", action: #selector(revealWorkspace), keyEquivalent: "")
        reveal.target = self
        let logs = projectMenu.addItem(withTitle: "Show Logs in Finder", action: #selector(revealLogs), keyEquivalent: "")
        logs.target = self
        project.submenu = projectMenu
        bar.addItem(project)
        let help = NSMenuItem()
        help.title = "Help"
        let helpMenu = NSMenu(title: "Help")
        let notices = helpMenu.addItem(withTitle: "Licenses and Notices", action: #selector(openNotices), keyEquivalent: "")
        notices.target = self
        help.submenu = helpMenu
        bar.addItem(help)
        NSApp.mainMenu = bar
    }

    private func selectWorkspace() throws -> URL {
        if let path = ProcessInfo.processInfo.environment["IMAGE23MF_DESKTOP_WORKSPACE"] {
            let url = URL(fileURLWithPath: path, isDirectory: true)
            try files.createDirectory(at: url, withIntermediateDirectories: true)
            return url
        }
        let config = home.appendingPathComponent("Library/Application Support/\(storageName)/config.json")
        if let data = try? Data(contentsOf: config),
           let saved = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           saved["schema_version"] as? Int == 1,
           let path = saved["workspace"] as? String, files.fileExists(atPath: path) {
            return URL(fileURLWithPath: path, isDirectory: true)
        }
        let panel = NSOpenPanel()
        panel.title = "Choose where to save your PLAyered projects"
        panel.message = "Your images, saved projects and exports stay in this folder."
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = home.appendingPathComponent("Documents", isDirectory: true)
        panel.prompt = "Use This Folder"
        guard panel.runModal() == .OK, let url = panel.url else {
            throw NSError(domain: name, code: 1, userInfo: [NSLocalizedDescriptionKey: "No workspace selected. Quit and reopen to choose a folder."])
        }
        try files.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONSerialization.data(withJSONObject: ["schema_version": 1, "workspace": url.path])
        try data.write(to: config, options: .atomic)
        return url
    }

    private func startEngine() throws {
        guard let resources = Bundle.main.resourceURL else { throw CocoaError(.fileNoSuchFile) }
        let binary = resources.appendingPathComponent("engine/image23mf-engine")
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw NSError(domain: name, code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not create a private session. Please reopen the app."])
        }
        session = bytes.map { String(format: "%02x", $0) }.joined()
        let process = Process()
        process.executableURL = binary
        process.currentDirectoryURL = workspace
        // Do not inherit developer .env, PYTHONPATH, DYLD, or optional provider keys.
        process.environment = ["HOME": home.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8", "TMPDIR": NSTemporaryDirectory(), "PYTHONUNBUFFERED": "1"]
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        let logDir = home.appendingPathComponent("Library/Logs/\(storageName)", isDirectory: true)
        try files.createDirectory(at: logDir, withIntermediateDirectories: true)
        let logURL = logDir.appendingPathComponent("desktop-engine.log")
        if let size = (try? files.attributesOfItem(atPath: logURL.path)[.size]) as? NSNumber,
           size.intValue > 5 * 1024 * 1024 {
            let previous = logDir.appendingPathComponent("desktop-engine.previous.log")
            try? files.removeItem(at: previous)
            try files.moveItem(at: logURL, to: previous)
        }
        if !files.fileExists(atPath: logURL.path) { files.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        let log = try FileHandle(forWritingTo: logURL)
        try log.seekToEnd()
        process.standardError = log
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.consume(data) }
        }
        process.terminationHandler = { [weak self] _ in
            try? log.close()
            DispatchQueue.main.async {
                guard let self else { return }
                self.readyTimer?.invalidate()
                if self.stopping { NSApp.reply(toApplicationShouldTerminate: true) }
                else { self.showFailure("The processing engine stopped. Your saved projects are still in the workspace. Quit and reopen the app; use Project → Show Logs in Finder if it happens again.") }
            }
        }
        engine = process
        try process.run()
        let config: [String: Any] = ["workspace": workspace.path, "home": home.path,
            "web_root": resources.appendingPathComponent("web").path, "token": session]
        var data = try JSONSerialization.data(withJSONObject: config)
        data.append(10)
        try input.fileHandleForWriting.write(contentsOf: data)
        try input.fileHandleForWriting.close()
        readyTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: false) { [weak self] _ in
            self?.showFailure("Startup took too long. Quit and reopen the app. Logs are available in the Project menu.")
            self?.engine?.terminate()
        }
    }

    private func consume(_ data: Data) {
        outputBuffer.append(data)
        if outputBuffer.count > 65536 { outputBuffer.removeAll(); return }
        while let newline = outputBuffer.firstIndex(of: 10) {
            let line = outputBuffer.prefix(upTo: newline)
            outputBuffer.removeSubrange(...newline)
            guard let reply = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            if let error = reply["error"] as? String { showFailure(error); continue }
            guard reply["ready"] as? Bool == true, let address = reply["url"] as? String,
                let url = URL(string: address), url.scheme == "http", url.host == "127.0.0.1",
                let port = url.port, port > 0 && port < 65536 else { continue }
            readyTimer?.invalidate()
            baseURL = url
            let properties: [HTTPCookiePropertyKey: Any] = [.name: "image23mf_desktop", .value: session,
                .domain: "127.0.0.1", .path: "/", .discard: "TRUE",
                HTTPCookiePropertyKey("HttpOnly"): "TRUE", HTTPCookiePropertyKey("SameSite"): "Strict"]
            guard let cookie = HTTPCookie(properties: properties) else { showFailure("Could not start the private application session."); return }
            web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { [weak self] in
                self?.web.load(URLRequest(url: url))
            }
        }
    }

    private func isLocal(_ url: URL) -> Bool {
        guard let base = baseURL else { return false }
        if url.scheme == "blob" { return url.absoluteString.hasPrefix("blob:\(base.absoluteString)/") }
        return url.scheme == base.scheme && url.host == base.host && url.port == base.port
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.absoluteString == "about:blank" && baseURL == nil { decisionHandler(.allow); return }
        if isLocal(url) {
            if action.shouldPerformDownload { decisionHandler(.download) }
            else { decisionHandler(.allow) }
            return
        }
        if action.navigationType == .linkActivated && ["https", "http", "mailto"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if response.response.url?.absoluteString == "about:blank" && baseURL == nil { decisionHandler(.allow); return }
        guard let url = response.response.url, isLocal(url) else { decisionHandler(.cancel); return }
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
        completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = URL(fileURLWithPath: suggestedFilename).lastPathComponent
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self, result == .OK, let destination = panel.url else { completionHandler(nil); return }
            let temporary = self.files.temporaryDirectory.appendingPathComponent("image23mf-download-\(UUID().uuidString)")
            self.downloads[ObjectIdentifier(download)] = (temporary, destination)
            completionHandler(temporary)
        }
    }
    func downloadDidFinish(_ download: WKDownload) {
        guard let target = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do {
            if files.fileExists(atPath: target.final.path) {
                _ = try files.replaceItemAt(target.final, withItemAt: target.temporary)
            } else { try files.moveItem(at: target.temporary, to: target.final) }
            NSWorkspace.shared.activateFileViewerSelecting([target.final])
        } catch {
            try? files.removeItem(at: target.temporary)
            showAlert("Could not save the download", error.localizedDescription)
        }
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if let target = downloads.removeValue(forKey: ObjectIdentifier(download)) { try? files.removeItem(at: target.temporary) }
        if (error as NSError).code != NSURLErrorCancelled { showAlert("Download failed", error.localizedDescription) }
    }
    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
        decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        decisionHandler(request.url.map(isLocal) == true ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void) { showAlert(name, message); completionHandler() }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "OK"); alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { showFailure(error.localizedDescription) }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        showFailure("The interface stopped responding. Quit and reopen the app. Your saved projects remain in the workspace.")
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { NSApp.terminate(nil); return false }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let process = engine, process.isRunning else { return .terminateNow }
        if stopping { return .terminateLater }
        if !quitChecked, let url = baseURL?.appendingPathComponent("__desktop/status") {
            var request = URLRequest(url: url, timeoutInterval: 3)
            request.setValue("image23mf_desktop=\(session)", forHTTPHeaderField: "Cookie")
            URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
                DispatchQueue.main.async {
                    guard let self else { return }
                    let state = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                    let active = state?["active_jobs"] as? Int
                    if active == nil || active! > 0 || !self.downloads.isEmpty {
                        let alert = NSAlert()
                        alert.messageText = "Quit PLAyered?"
                        alert.informativeText = "Any builds or downloads in progress will stop. Saved projects stay in your workspace."
                        alert.addButton(withTitle: "Keep Working"); alert.addButton(withTitle: "Quit")
                        if alert.runModal() != .alertSecondButtonReturn { NSApp.reply(toApplicationShouldTerminate: false); return }
                    }
                    self.quitChecked = true
                    self.stopEngine()
                }
            }.resume()
            return .terminateLater
        }
        stopEngine()
        return .terminateLater
    }
    private func stopEngine() {
        stopping = true
        readyTimer?.invalidate()
        guard let process = engine, process.isRunning else { NSApp.reply(toApplicationShouldTerminate: true); return }
        process.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 12) { [weak self, weak process] in
            guard self?.stopping == true, let process, process.isRunning else { return }
            let pid = process.processIdentifier
            if getpgid(pid) == pid { kill(-pid, SIGKILL) } else { kill(pid, SIGKILL) }
        }
    }
    @objc private func revealWorkspace() { if let workspace { NSWorkspace.shared.open(workspace) } }
    @objc private func revealLogs() { NSWorkspace.shared.open(home.appendingPathComponent("Library/Logs/\(storageName)")) }
    @objc private func openNotices() {
        if let url = Bundle.main.resourceURL?.appendingPathComponent("LICENSES/NOTICE.txt") { NSWorkspace.shared.open(url) }
    }
    private func showFailure(_ message: String) {
        readyTimer?.invalidate()
        let escaped = message.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;").replacingOccurrences(of: ">", with: "&gt;")
        baseURL = nil
        web.loadHTMLString("<html><body style='font:17px -apple-system;background:#f5f7fa;color:#273746;padding:60px;max-width:680px'><h1>Couldn’t open PLAyered</h1><p>\(escaped)</p></body></html>", baseURL: nil)
    }
    private func showAlert(_ title: String, _ message: String) {
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = message; alert.runModal()
    }
}
let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = StudioDelegate()
application.delegate = delegate
application.run()
