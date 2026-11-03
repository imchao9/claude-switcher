import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - 模型

enum RowState { case pending, ok, warn, fail, skip }

struct CheckRow: Identifiable {
    let id = UUID()
    let name: String
    let icon: String
    var state: RowState = .pending
    var detail: String = ""
}

enum Phase { case checking, ready, failed, switching, done, fixing }

// 入口只改变展示，不推断服务端账号状态，也不触发任何核心命令。
enum UsageJourney: String, CaseIterable, Identifiable {
    case firstUse, daily, recovery
    var id: String { rawValue }
    var title: String {
        switch self {
        case .firstUse: return "首次使用"
        case .daily: return "日常使用"
        case .recovery: return "受限与清理"
        }
    }
}

struct CoreResult {
    let output: String
    let succeeded: Bool
}

struct CleanupItem: Decodable, Identifiable {
    let id: String
    let scope: String
    let path: String
    let state: String
    let bytes: Int64?

    var sizeLabel: String {
        guard let bytes else { return "无法估算" }
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

struct CleanupPlan: Decodable {
    let version: Int
    let token: String
    let items: [CleanupItem]
    let notes: [String]
    let manual: [String]
}

struct CleanupReport: Decodable {
    struct Item: Decodable {
        let path: String
        let state: String
        let detail: String
    }
    let results: [Item]
    let recovery: String
    let complete: Bool
}

struct AccountStamp: Equatable {
    let size: String
    let date: String
}

// 保留系统原始顺序；Claude 的单独语言设置与系统首选语言分别展示。
struct LanguagePreferences {
    let system: [String]?
    let claude: [String]?

    static func read() -> LanguagePreferences {
        func languages(_ domain: CFString) -> [String]? {
            CFPreferencesSynchronize(domain, kCFPreferencesCurrentUser, kCFPreferencesAnyHost)
            guard let values = CFPreferencesCopyValue("AppleLanguages" as CFString, domain,
                                                     kCFPreferencesCurrentUser, kCFPreferencesAnyHost) as? [String],
                  !values.isEmpty, values.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
                return nil
            }
            return values
        }
        return LanguagePreferences(system: languages(kCFPreferencesAnyApplication),
                                   claude: languages("com.anthropic.claudefordesktop" as CFString))
    }

    static func displayName(_ identifier: String) -> String {
        Locale(identifier: "zh_CN").localizedString(forIdentifier: identifier) ?? identifier
    }

    var systemSummary: String { system?.first ?? "无法读取" }
    var claudeSummary: String {
        if let language = claude?.first { return "单独设置：\(language)" }
        if let language = system?.first { return "跟随系统：\(language)" }
        return "未单独设置，系统首选语言无法读取"
    }

    var auditDetail: String { "系统首选：\(systemSummary)；Claude \(claudeSummary)" }
}

// 超时回调与 waitUntilExit 在不同队列上执行。
private final class ProcessDeadline {
    private let lock = NSLock()
    private var expired = false

    func expire(_ process: Process) {
        lock.lock()
        defer { lock.unlock() }
        guard process.isRunning else { return }
        expired = true
        process.terminate()
    }

    var timedOut: Bool {
        lock.lock()
        defer { lock.unlock() }
        return expired
    }
}

final class VM: ObservableObject {
    @Published var rows: [CheckRow] = [
        CheckRow(name: "代理内核", icon: "cpu"),
        CheckRow(name: "TUN 网卡", icon: "cable.connector"),
        CheckRow(name: "出口 IP", icon: "globe.americas"),
        CheckRow(name: "IP 与上次一致", icon: "checkmark.seal"),
        CheckRow(name: "时区", icon: "clock"),
        CheckRow(name: "地区", icon: "flag"),
        CheckRow(name: "IPv6 关闭", icon: "6.circle"),
        CheckRow(name: "DNS 走隧道", icon: "magnifyingglass"),
        CheckRow(name: "WebRTC", icon: "eye.slash"),
        CheckRow(name: "保镖在线", icon: "hand.raised"),
        CheckRow(name: "规则置顶", icon: "list.number"),
        CheckRow(name: "客户端语言", icon: "abc"),
        CheckRow(name: "ASN 属性", icon: "building.columns"),
        CheckRow(name: "生态可达性", icon: "antenna.radiowaves.left.and.right"),
    ]
    @Published var phase: Phase = .checking
    @Published var current = "?"
    @Published var archived: Set<String> = []
    @Published var stamps: [String: AccountStamp] = [:]
    @Published var banner = ""
    @Published var note = ""
    @Published var fingerprint = ""
    @Published private(set) var isBusy = false
    @Published private(set) var lastCheckSummary = "尚未检查"
    @Published var updateText = ""
    @Published var showUpdateSheet = false
    @Published private(set) var coreMissing = false
    @Published private(set) var currentClient = ""
    @Published private(set) var isSwitchingClient = false
    @Published var disabledChecks: Set<String> = []
    @Published var allowOptionalFailures = false
    @Published var loggingEnabled = true
    @Published var settingsMessage = ""
    @Published private(set) var settingsBusy = false
    @Published var showSettingsSheet = false
    @Published private(set) var languagePreferences = LanguagePreferences(system: nil, claude: nil)
    @Published private(set) var languageSettingsMessage = ""

    @Published private(set) var selectedJourney: UsageJourney?
    var journey: UsageJourney {
        selectedJourney ?? (coreMissing || !Self.accountLetters.contains(current) ? .firstUse : .daily)
    }
    var environmentReady: Bool { phase == .ready || phase == .done }
    var failedChecks: [CheckRow] { rows.filter { $0.state == .fail } }
    func selectJourney(_ value: UsageJourney) {
        hideAfterSwitch?.cancel()
        selectedJourney = value
    }
    func openCleanup() {
        guard !isBusy, coreAvailable() else { return }
        selectJourney(.recovery)
        showCleanupSheet = true
    }

    @Published var showCleanupSheet = false
    @Published private(set) var cleanupPlan: CleanupPlan?
    @Published var cleanupSelection: Set<String> = []
    @Published var cleanupConfirmation = ""
    @Published var cleanupReview = false
    @Published private(set) var cleanupBusy = false
    @Published private(set) var cleanupMessage = ""
    @Published private(set) var cleanupReport: CleanupReport?

    static let cleanupScopes: [(key: String, label: String, detail: String)] = [
        ("desktop-data", "Claude 桌面端数据", "登录资料、设置、MCP 配置、缓存和日志；重置当前账号指针"),
        ("code-data", "Claude Code 数据", "全部 A–D 的配置、会话历史、插件和登录资料"),
        ("desktop-app", "Claude 桌面端程序", "/Applications 与个人 Applications 中的 Claude.app"),
        ("code-app", "Claude Code 原生程序", "~/.local/bin/claude 与 ~/.local/share/claude；包管理器安装见下方"),
        ("archives", "A–D 桌面账号快照", "包含旧登录资料；默认保留，仅在不再需要时勾选"),
    ]

    var cleanupChosen: [CleanupItem] {
        cleanupPlan?.items.filter { cleanupSelection.contains($0.scope) } ?? []
    }
    var canReviewCleanup: Bool {
        !isBusy && !cleanupBusy && cleanupPlan != nil && !cleanupSelection.isEmpty &&
            cleanupChosen.contains { $0.state == "present" } &&
            !cleanupChosen.contains { $0.state == "blocked" }
    }
    var canExecuteCleanup: Bool {
        canReviewCleanup && cleanupReview && cleanupConfirmation == "移入废纸篓"
    }

    func loadCleanupPlan() {
        guard !isBusy, !cleanupBusy, coreAvailable() else { return }
        hideAfterSwitch?.cancel()
        cleanupBusy = true
        isBusy = true
        cleanupPlan = nil
        cleanupReport = nil
        cleanupReview = false
        cleanupConfirmation = ""
        cleanupMessage = "正在扫描本地路径…"
        run(["cleanup-info"], timeout: operationTimeout) { result in
            defer { self.cleanupBusy = false; self.isBusy = false }
            guard result.succeeded, let data = result.output.data(using: .utf8),
                  let plan = try? JSONDecoder().decode(CleanupPlan.self, from: data),
                  plan.version == 1, plan.token.count == 64 else {
                self.cleanupMessage = "无法读取清理预览。请确认核心脚本与界面均已更新。"
                return
            }
            self.cleanupPlan = plan
            self.cleanupMessage = "选择需要清理的项目，再查看确认清单。"
        }
    }

    func executeCleanup() {
        guard canExecuteCleanup, let plan = cleanupPlan else { return }
        let scopes = Self.cleanupScopes.map { $0.key }.filter { cleanupSelection.contains($0) }
        hideAfterSwitch?.cancel()
        cleanupBusy = true
        isBusy = true
        cleanupMessage = "正在移入废纸篓，请保持 Claude 与 Claude Code 退出…"
        run(["cleanup-data", scopes.joined(separator: ","), plan.token, cleanupConfirmation], timeout: operationTimeout) { result in
            defer { self.cleanupBusy = false; self.isBusy = false }
            self.cleanupConfirmation = ""
            // run() may prefix nonzero results with an exit-code explanation.
            let json = result.output.range(of: "{").map { String(result.output[$0.lowerBound...]) } ?? ""
            if let data = json.data(using: .utf8),
               let report = try? JSONDecoder().decode(CleanupReport.self, from: data) {
                self.cleanupReport = report
                self.cleanupMessage = result.succeeded && report.complete
                    ? "所选项目已处理。已移动的数据保留在废纸篓。"
                    : "部分项目未完成，后续项目已停止。请查看逐项结果。"
            } else if let data = json.data(using: .utf8),
                      let value = try? JSONSerialization.jsonObject(with: data) as? [String: String],
                      let error = value["error"] {
                self.cleanupMessage = error
            } else {
                self.cleanupMessage = "未收到完整清理结果，请检查废纸篓后刷新预览。"
            }
            self.cleanupPlan = nil // A plan is single-use, including a failed attempt.
            self.cleanupReview = false
            self.phase = .failed
            self.lastCheckSummary = "清理后需重新检查"
            self.banner = "本地数据发生变化或清理未完成，请关闭清理窗口后重新检查。"
            self.current = "未知"
            self.archived = []
            self.stamps = [:]
        }
    }

    static let optionalChecks: [(key: String, label: String)] = [
        ("IP一致", "IP 与上次一致"),
        ("DNS", "DNS 走隧道"),
        ("WebRTC", "WebRTC"),
        ("客户端语言", "客户端语言"),
        ("ASN属性", "ASN 属性"),
        ("生态可达性", "生态可达性"),
    ]

    static let clientChoices: [(key: String, label: String)] = [
        ("clash-verge", "Clash Verge"),
        ("flclash", "FlClash"),
        ("shadowrocket", "Shadowrocket"),
        ("sing-box", "sing-box"),
        ("clashx", "ClashX"),
        ("surge", "Surge"),
    ]

    let core: String
    private let checkTimeout: TimeInterval
    private let operationTimeout: TimeInterval
    private let languagePreferencesReader: () -> LanguagePreferences
    private let languageSettingsOpener: () -> Bool
    private var started = false
    private var switchRequested = false
    private var checkingUpdate = false
    private var hideAfterSwitch: DispatchWorkItem?
    var onSwitchCompleted: (() -> Void)?

    init(core: String = NSHomeDirectory() + "/.local/bin/claude-switch.sh",
         checkTimeout: TimeInterval = 45, operationTimeout: TimeInterval = 180,
         languagePreferencesReader: @escaping () -> LanguagePreferences = LanguagePreferences.read,
         languageSettingsOpener: @escaping () -> Bool = {
             NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Localization-Settings.extension")!)
         }) {
        self.core = core
        self.checkTimeout = checkTimeout
        self.operationTimeout = operationTimeout
        self.languagePreferencesReader = languagePreferencesReader
        self.languageSettingsOpener = languageSettingsOpener
    }

    var canQuit: Bool { !cleanupBusy && !switchRequested && phase != .fixing && phase != .switching }

    func start() {
        guard !started else { return }
        started = true
        refresh()
    }

    func prepareToShow() {
        hideAfterSwitch?.cancel()
        if phase == .done { phase = .ready }
    }

    private func run(_ args: [String], timeout: TimeInterval = 45, done: @escaping (CoreResult) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("switcher-out-\(UUID().uuidString).txt")
            guard FileManager.default.createFile(atPath: tmp.path, contents: nil, attributes: [.posixPermissions: 0o600]),
                  let fh = try? FileHandle(forWritingTo: tmp) else {
                DispatchQueue.main.async { done(CoreResult(output: "无法创建检查输出文件", succeeded: false)) }
                return
            }
            defer {
                try? fh.close()
                try? FileManager.default.removeItem(at: tmp)
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = [self.core] + args
            p.standardOutput = fh
            p.standardError = fh
            do { try p.run() } catch {
                DispatchQueue.main.async { done(CoreResult(output: "无法启动脚本：\(error.localizedDescription)", succeeded: false)) }
                return
            }
            let deadline = ProcessDeadline()
            let watchdog = DispatchWorkItem { deadline.expire(p) }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)
            p.waitUntilExit()
            watchdog.cancel()
            let data = (try? Data(contentsOf: tmp)) ?? Data()
            let out = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            let result: CoreResult
            if deadline.timedOut {
                result = CoreResult(output: "操作超时，请重试", succeeded: false)
            } else if p.terminationStatus != 0 {
                result = CoreResult(output: "脚本执行失败（退出码 \(p.terminationStatus)）\n\(out)", succeeded: false)
            } else {
                result = CoreResult(output: out, succeeded: true)
            }
            DispatchQueue.main.async { done(result) }
        }
    }

    func refresh() {
        guard !isBusy else { return }
        hideAfterSwitch?.cancel()
        if !coreAvailable() { markCoreMissing(); return }
        isBusy = true
        checkEnvironment { _ in self.isBusy = false }
    }

    private func coreAvailable() -> Bool {
        FileManager.default.isReadableFile(atPath: core)
    }

    private func markCoreMissing() {
        coreMissing = true
        phase = .failed
        lastCheckSummary = "未安装核心脚本"
        banner = "未安装核心脚本：请下载 installer-kit 安装包并双击 installer/安装.command，或运行源码安装"
        for i in rows.indices {
            rows[i].state = .fail
            rows[i].detail = "未安装核心脚本"
        }
    }

    func refreshAccountStatus() {
        // 菜单展开时只读取本地账号状态，不触发网络审计；启动审计优先。
        guard started, !isBusy else { return }
        isBusy = true
        run(["status"], timeout: checkTimeout) { result in
            self.updateStatus(result)
            self.isBusy = false
        }
    }

    private func checkEnvironment(done: @escaping (Bool) -> Void) {
        if !coreAvailable() {
            markCoreMissing()
            done(false)
            return
        }
        coreMissing = false
        phase = .checking
        lastCheckSummary = "检查中"
        banner = ""
        note = ""
        for i in rows.indices { rows[i].state = .pending; rows[i].detail = "" }
        run(["checkdetail"], timeout: checkTimeout) { result in
            var allOk = result.succeeded
            var matched = Set<Int>()
            var duplicated = false
            let keyMap: [String: String] = [
                "代理内核": "代理内核", "TUN网卡": "TUN 网卡", "出口IP": "出口 IP",
                "IP一致": "IP 与上次一致", "时区": "时区", "地区": "地区",
                "IPv6": "IPv6 关闭", "DNS": "DNS 走隧道", "WebRTC": "WebRTC",
                "保镖": "保镖在线", "规则置顶": "规则置顶",
                "客户端语言": "客户端语言", "ASN属性": "ASN 属性", "生态可达性": "生态可达性",
            ]
            for line in result.output.split(separator: "\n") {
                let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
                guard parts.count == 2, let rowName = keyMap[parts[0]],
                      let idx = self.rows.firstIndex(where: { $0.name == rowName }) else { continue }
                // 重复行不能补齐缺失项，也不能用后来的 ok 覆盖先前的 fail。
                guard matched.insert(idx).inserted else { allOk = false; duplicated = true; continue }
                let status = parts[1].split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
                switch status.first ?? "" {
                case "ok": self.rows[idx].state = .ok
                case "warn": self.rows[idx].state = .warn
                case "skip": self.rows[idx].state = .skip
                default: self.rows[idx].state = .fail; allOk = false
                }
                self.rows[idx].detail = status.count > 1 ? status[1] : ""
            }
            if let languageRow = self.rows.firstIndex(where: { $0.name == "客户端语言" }),
               self.rows[languageRow].state != .skip, self.rows[languageRow].state != .pending {
                self.loadLanguagePreferences()
                let existing = self.rows[languageRow].detail
                self.rows[languageRow].detail = [existing, self.languagePreferences.auditDetail]
                    .filter { !$0.isEmpty }.joined(separator: "；")
            }
            if !result.succeeded {
                self.banner = result.output.isEmpty ? "核心脚本执行失败" : result.output
                for i in self.rows.indices where self.rows[i].state == .pending {
                    self.rows[i].state = .fail
                    self.rows[i].detail = "核心脚本执行失败"
                }
            } else if duplicated {
                self.banner = "检测输出存在重复项，请重新检查"
            } else if matched.count != self.rows.count {
                self.banner = "检测输出不完整(\(matched.count)/\(self.rows.count))，请点重新检查"
            }
            let passed = allOk && matched.count == self.rows.count
            self.lastCheckSummary = passed
                ? (self.rows.contains { $0.state == .warn }
                    ? "有警告"
                    : (self.rows.contains { $0.state == .skip } ? "通过（含跳过）" : "通过"))
                : "失败"
            self.run(["get-client"], timeout: self.checkTimeout) { clientResult in
                self.updateClient(clientResult)
                self.run(["status"], timeout: self.checkTimeout) { status in
                    let statusOK = self.updateStatus(status)
                    if !statusOK {
                        if !passed || self.banner.isEmpty {
                            self.banner = "无法读取账号状态，请重新检查"
                        }
                        self.lastCheckSummary = "失败"
                    }
                    self.phase = passed && statusOK ? .ready : .failed
                    done(passed && statusOK)
                }
            }
        }
    }

    @discardableResult
    private func updateStatus(_ result: CoreResult) -> Bool {
        let fields = result.output.split(whereSeparator: { $0.isWhitespace }).reduce(into: [String: String]()) { values, field in
            let pair = field.split(separator: "=", maxSplits: 1).map(String.init)
            if pair.count == 2 { values[pair[0]] = pair[1] }
        }
        func reset() -> Bool {
            current = "未知"; archived = []; stamps = [:]
            return false
        }
        guard result.succeeded, let account = fields["current"],
              ["A", "B", "C", "D", "none"].contains(account) else {
            return reset()
        }
        var known = Set<String>()
        if let list = fields["accounts"] {
            if list != "none" {
                for piece in list.split(separator: ",", omittingEmptySubsequences: true) {
                    let letter = String(piece)
                    guard Self.accountLetters.contains(letter) else { return reset() }
                    known.insert(letter)
                }
            }
        } else {
            // 旧版核心只输出 current/hasA/hasB,继续兼容已安装的旧脚本。
            for letter in ["A", "B"] {
                guard let flag = fields["has\(letter)"], ["0", "1"].contains(flag) else { return reset() }
                if flag == "1" { known.insert(letter) }
            }
        }
        var parsed: [String: AccountStamp] = [:]
        for letter in Self.accountLetters {
            guard let raw = fields["acc\(letter)"] else { continue }
            let parts = raw.split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2, !parts[0].isEmpty, !parts[1].isEmpty else { return reset() }
            parsed[letter] = AccountStamp(size: String(parts[0]), date: String(parts[1]))
        }
        current = account == "none" ? "未初始化" : account
        stamps = parsed
        archived = known
        return true
    }

    static let accountLetters = ["A", "B", "C", "D"]

    private func updateClient(_ result: CoreResult) {
        guard result.succeeded else { return }
        for line in result.output.split(separator: "\n") {
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false).map(String.init)
            if parts.count == 2 && parts[0] == "client" {
                currentClient = parts[1]
            }
        }
    }

    func loadLanguagePreferences() {
        languagePreferences = languagePreferencesReader()
    }

    func openLanguageSettings() {
        languageSettingsMessage = languageSettingsOpener()
            ? "调整后请完全退出并重新打开 Claude，再重新检查。"
            : "无法打开系统设置，请手动进入“通用 → 语言与地区”。"
    }

    func loadCheckConfig() {
        guard !settingsBusy, coreAvailable() else { return }
        settingsBusy = true
        settingsMessage = "正在读取设置…"
        run(["get-check-config"]) { result in
            self.settingsBusy = false
            guard result.succeeded,
                  let data = result.output.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let config = object as? [String: Any],
                  let disabled = config["disabled"] as? [String],
                  let allow = config["allowOptionalFailures"] as? Bool,
                  let logging = config["logging"] as? Bool,
                  Set(disabled).count == disabled.count,
                  disabled.allSatisfy({ name in Self.optionalChecks.contains { $0.key == name } }) else {
                self.settingsMessage = "读取检查设置失败，请恢复默认后重试"
                return
            }
            self.disabledChecks = Set(disabled)
            self.allowOptionalFailures = allow
            self.loggingEnabled = logging
            self.settingsMessage = ""
        }
    }

    func saveCheckConfig() {
        guard !isBusy, !settingsBusy, coreAvailable() else { return }
        let allowed = Set(Self.optionalChecks.map { $0.key })
        guard disabledChecks.isSubset(of: allowed) else {
            settingsMessage = "包含不允许关闭的安全检查"
            return
        }
        let config: [String: Any] = [
            "disabled": Self.optionalChecks.map { $0.key }.filter { disabledChecks.contains($0) },
            "allowOptionalFailures": allowOptionalFailures,
            "logging": loggingEnabled,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: config, options: [.sortedKeys]),
              let json = String(data: data, encoding: .utf8) else { return }
        settingsBusy = true
        settingsMessage = "正在保存…"
        run(["set-check-config", json]) { result in
            self.settingsBusy = false
            self.settingsMessage = result.succeeded ? "设置已保存，正在重新检查" : result.output
            if result.succeeded { self.refresh() }
        }
    }

    func restoreCheckDefaults() {
        disabledChecks = []
        allowOptionalFailures = false
        loggingEnabled = true
        settingsMessage = "已恢复默认选项，点击保存后生效"
    }

    func exportDiagnostics() {
        guard !settingsBusy, coreAvailable() else { return }
        let panel = NSSavePanel()
        panel.title = "导出脱敏诊断日志"
        panel.message = "仅包含检查状态、设置和脱敏操作日志，不含 IP、凭据、账号或代理配置。"
        panel.nameFieldStringValue = "claude-switcher-diagnostics.zip"
        panel.allowedContentTypes = [.zip]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        settingsBusy = true
        settingsMessage = "正在导出诊断日志…"
        run(["export-diagnostics", destination.path], timeout: operationTimeout) { result in
            self.settingsBusy = false
            let message = result.succeeded ? result.output : "导出失败：\(result.output)"
            self.settingsMessage = message
            let alert = NSAlert()
            alert.messageText = result.succeeded ? "诊断日志已导出" : "诊断日志导出失败"
            alert.informativeText = message
            alert.alertStyle = result.succeeded ? .informational : .warning
            alert.runModal()
        }
    }

    func setClient(_ key: String) {
        guard !isBusy, !isSwitchingClient else { return }
        guard Self.clientChoices.contains(where: { $0.key == key }) else { return }
        guard key != currentClient else { return }
        guard coreAvailable() else { markCoreMissing(); return }
        isBusy = true
        isSwitchingClient = true
        banner = "正在切换代理客户端…"
        run(["set-client", key], timeout: operationTimeout) { result in
            self.isSwitchingClient = false
            guard result.succeeded else {
                self.banner = result.output
                self.isBusy = false
                return
            }
            // 切换后重新完成环境审计,刷新「代理内核」等检查项
            self.currentClient = key
            self.checkEnvironment { _ in self.isBusy = false }
        }
    }

    func fixAll() {
        guard !isBusy else { return }
        guard coreAvailable() else { markCoreMissing(); return }
        hideAfterSwitch?.cancel()
        isBusy = true
        phase = .fixing
        banner = "正在修复…（若弹出密码框请输入开机密码）"
        run(["fix"], timeout: operationTimeout) { result in
            guard result.succeeded else {
                self.banner = result.output; self.phase = .failed; self.isBusy = false
                return
            }
            self.checkEnvironment { _ in self.isBusy = false }
        }
    }

    func inspectFingerprint() {
        guard !isBusy else { return }
        isBusy = true
        fingerprint = "正在体检…"
        run(["fingerprint"], timeout: 90) { result in
            self.fingerprint = result.output
            self.isBusy = false
        }
    }

    func checkUpdate() {
        guard !checkingUpdate else { return }
        checkingUpdate = true
        updateText = "正在检查更新…"
        run(["update-check"], timeout: 120) { result in
            self.updateText = result.output.isEmpty
                ? (result.succeeded ? "检查完成，核心脚本没有返回结果" : "检查失败，请重试")
                : result.output
            self.checkingUpdate = false
        }
    }

    var aiPrompt: String {
        let home = NSHomeDirectory()
        let agents = home + "/.claude-switcher/AGENTS.md"
        let agentsLine = FileManager.default.fileExists(atPath: agents)
            ? "本机交接文档：\(agents)"
            : "本机交接文档：\(agents)（~/.claude-switcher 不存在或其中没有 AGENTS.md，请从仓库根目录读取 AGENTS.md）"
        return """
        这是「Claude 切换器」的 AI 交接提示词。请先完整阅读交接文档，再开始安装、配置或排障。

        仓库地址：https://github.com/YOURSJEYO/claude-switcher
        \(agentsLine)

        执行要求：
        1. 先读 AGENTS.md，按其中的安装步骤、验收标准与安全红线执行，不要只凭本段提示词猜测。
        2. 安装或排障时先跑环境审计：硬性安全项必须通过；只有显式配置的非关键项可跳过或降为警告，skip 不表示检查通过。
        3. 账号切换前先完全退出 Claude；账号快照按 AGENTS.md 的规则保留，不要改动 ~/.claude-switcher/accounts/ 的结构。
        4. 保镖等常驻脚本只放在 root 所有的系统目录，不要改回用户可写路径；需要变更时引导我重跑安装器。
        5. 不要未经我明确同意更新、覆盖或重装已安装版本（~/.local/bin、桌面 App、launchd 与系统目录），也不要执行 update 或安装器；先说明影响并征得同意。
        6. 检查新版本只运行 `~/.local/bin/claude-switch.sh update-check`，看到结果后先问我是否执行更新。
        7. 不要在日志、文档或回复里暴露我的出口 IP、代理地址或任何凭据。
        """
    }

    func switchTo(_ target: String) {
        guard !isBusy, Self.accountLetters.contains(target) else { return }
        hideAfterSwitch?.cancel()
        isBusy = true
        switchRequested = true
        // 常驻后旧检查可能已经过期。每次切换都重新审计，不使用落盘的旧结果放行。
        checkEnvironment { passed in
            guard passed else {
                self.switchRequested = false; self.isBusy = false
                return
            }
            self.performSwitch(target)
        }
    }

    private func performSwitch(_ target: String) {
        phase = .switching
        banner = "正在切换到账号 \(target)…"
        run(["switch", target], timeout: operationTimeout) { [self] result in
            let lines = result.output.split(separator: "\n").map(String.init)
            let failure = lines.first { $0.hasPrefix("FAIL:") }
            let success = lines.contains { $0.hasPrefix("OK:") || $0.hasPrefix("INFO:") }
            guard result.succeeded, failure == nil, success else {
                self.phase = .failed
                self.banner = failure.map { String($0.dropFirst(5)) } ?? (result.output.isEmpty ? "切换未返回成功结果，请重新检查" : result.output)
                self.switchRequested = false; self.isBusy = false
                return
            }
            var extra = ""
            if target != "A" && !self.archived.contains(target) {
                extra = "\(target) 号首次使用：请在打开的 Claude 里登录 \(target) 账号，登录一次即可。"
            }
            let unchanged = lines.contains { $0.hasPrefix("INFO:") }
            if unchanged { extra = "" }
            self.current = target
            self.phase = .done
            self.banner = unchanged ? "当前已是账号 \(target)" : "已切换到账号 \(target)"
            self.note = extra
            self.switchRequested = false; self.isBusy = false
            let hide = DispatchWorkItem { [weak self] in
                guard let self = self, self.phase == .done, !self.isBusy else { return }
                self.onSwitchCompleted?()
                self.phase = .ready
            }
            self.hideAfterSwitch = hide
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.2, execute: hide)
        }
    }
}

// MARK: - Claude 风格外观

enum ClaudeTheme {
    // 纸感暖色基底 (Warm Parchment / Alabaster)
    static let bgTop = Color(red: 0.984, green: 0.976, blue: 0.961)         // #FAF9F5
    static let bgBottom = Color(red: 0.957, green: 0.945, blue: 0.925)      // #F4F1EC
    static let card = Color.white                                           // #FFFFFF
    static let cardBorder = Color(red: 0.898, green: 0.882, blue: 0.855)   // #E5E1DA
    static let divider = Color(red: 0.941, green: 0.929, blue: 0.910)      // #F0ECE8
    static let iconWell = Color(red: 0.965, green: 0.953, blue: 0.937)     // #F6F3EF

    // 字体色阶 (Editorial Ink & Warm Stone)
    static let ink = Color(red: 0.165, green: 0.153, blue: 0.141)          // #2A2724
    static let muted = Color(red: 0.455, green: 0.435, blue: 0.412)        // #746F69

    // Claude 标志性陶土橙 (Anthropic Claude Terracotta)
    static let terracotta = Color(red: 0.847, green: 0.447, blue: 0.329)   // #D87254
    static let terracottaDeep = Color(red: 0.761, green: 0.369, blue: 0.259) // #C25E42
    static let terracottaTint = Color(red: 0.847, green: 0.447, blue: 0.329).opacity(0.09)

    // 自然柔和状态色
    static let success = Color(red: 0.180, green: 0.490, blue: 0.322)       // #2E7D52 (Sage Green)

    static let warning = Color(red: 0.722, green: 0.431, blue: 0.094)       // #B86E18 (Warm Amber)
    static let warningBorder = Color(red: 0.961, green: 0.863, blue: 0.718) // #F5DCB7

    static let error = Color(red: 0.761, green: 0.282, blue: 0.212)         // #C24836 (Claude Terracotta Red)
}

// MARK: - Claude 优雅背景

struct ClaudeBackground: View {
    var body: some View {
        ZStack {
            LinearGradient(
                colors: [ClaudeTheme.bgTop, ClaudeTheme.bgBottom],
                startPoint: .top,
                endPoint: .bottom
            )
            RadialGradient(
                colors: [ClaudeTheme.terracotta.opacity(0.04), Color.clear],
                center: .top,
                startRadius: 0,
                endRadius: 400
            )
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

// MARK: - 辅助样式组件

struct ClaudeIconButton: View {
    let systemName: String
    let help: String
    var disabled: Bool = false
    let action: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .font(.system(size: 13, weight: .medium))
                .foregroundColor(isHovered ? ClaudeTheme.ink : ClaudeTheme.muted)
                .frame(width: 28, height: 28)
                .background(isHovered ? ClaudeTheme.iconWell : ClaudeTheme.card.opacity(0.85))
                .clipShape(RoundedRectangle(cornerRadius: 7.5))
                .overlay(
                    RoundedRectangle(cornerRadius: 7.5)
                        .stroke(isHovered ? ClaudeTheme.cardBorder : ClaudeTheme.divider, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .accessibilityLabel(help)
        .help(help)
        .onHover { isHovered = $0 }
    }
}

// MARK: - 视图

struct ContentView: View {
    @ObservedObject var vm: VM
    var height: CGFloat = 900
    @State private var showNotice = false
    @State private var showFingerprint = false
    @State private var showAIPrompt = false
    @State private var showCheckDetails = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(spacing: 12) {
                header
                Picker("使用场景", selection: Binding(get: { vm.journey }, set: { vm.selectJourney($0) })) {
                    ForEach(UsageJourney.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("usage-journey")
                // 主任务优先，小屏继续滚动查看完整检查与工具。
                ScrollView {
                    VStack(spacing: 14) {
                        journeyCard
                        if vm.journey != .recovery {
                            if vm.coreMissing { setupCard }
                            else if vm.phase == .ready || vm.phase == .switching || vm.phase == .done { accountCard }
                            else if vm.phase == .failed || vm.phase == .fixing { retryCard }
                        }
                        DisclosureGroup(isExpanded: $showCheckDetails) {
                            checksCard.padding(.top, 8)
                        } label: {
                            HStack {
                                Text("环境检查 · \(vm.rows.count) 项").font(.callout.weight(.semibold))
                                Spacer()
                                statusPill
                            }
                        }
                        .tint(ClaudeTheme.terracottaDeep)
                        clientCard
                        if vm.journey != .recovery { cleanupEntry }
                    }
                    .frame(maxWidth: .infinity)
                }
                footer
            }
            .padding(16)
        }
        .sheet(isPresented: $showAIPrompt) { AIPromptSheet(text: vm.aiPrompt) }
        .sheet(isPresented: $vm.showSettingsSheet) { CheckSettingsView(vm: vm) }
        .sheet(isPresented: $vm.showCleanupSheet) { CleanupView(vm: vm) }
        .frame(width: 440, height: height)
        .preferredColorScheme(.light)
    }

    var header: some View {
        HStack(spacing: 11) {
            // Claude 风格陶土盾牌徽标
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(
                        LinearGradient(
                            colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .frame(width: 40, height: 40)
                    .overlay(
                        RoundedRectangle(cornerRadius: 10)
                            .stroke(Color.white.opacity(0.22), lineWidth: 1)
                    )
                    .shadow(color: ClaudeTheme.terracotta.opacity(0.24), radius: 3.5, x: 0, y: 1.5)

                Image(systemName: "shield.lefthalf.filled")
                    .font(.system(size: 19, weight: .medium))
                    .foregroundColor(.white)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text("Claude 账号切换器")
                    .font(.system(size: 16, weight: .bold, design: .serif))
                    .foregroundColor(ClaudeTheme.ink)
                Text("设置 · 日常启动 · 受限与清理")
                    .font(.system(size: 11, weight: .regular))
                    .foregroundColor(ClaudeTheme.muted)
            }

            Spacer()

            ClaudeIconButton(
                systemName: "waveform.path.ecg",
                help: "设备指纹体检(字体/GPU/虚拟机)",
                disabled: vm.isBusy
            ) {
                showFingerprint = true
                vm.inspectFingerprint()
            }

            ClaudeIconButton(
                systemName: "gearshape",
                help: "检查设置与诊断日志"
            ) {
                vm.showSettingsSheet = true
            }

            ClaudeIconButton(
                systemName: "info.circle",
                help: "安全防护体系公告"
            ) {
                showNotice = true
            }

        }
        .sheet(isPresented: $showNotice) { NoticeView() }
        .sheet(isPresented: $showFingerprint) { FingerprintView(text: vm.fingerprint) }
    }

    var journeyCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch vm.journey {
            case .firstUse:
                Label("先准备环境，再登录账号", systemImage: "sparkles")
                    .font(.headline).foregroundColor(ClaudeTheme.ink)
                Text("已有 Claude 登录资料会继续保留。首次使用无需清理数据。")
                    .font(.caption).foregroundColor(ClaudeTheme.muted)
                guideStep("1", title: "安装切换器与 Claude", detail: vm.coreMissing
                          ? "下载完整 installer-kit，按安装说明完成设置。"
                          : "已找到核心脚本；请确认 Claude 桌面端已安装。")
                guideStep("2", title: "完成环境检查", detail: vm.environmentReady
                          ? "检查已完成；跳过项与警告可展开核对。"
                          : "打开代理并确认节点地区；未通过项处理后重新检查。")
                guideStep("3", title: "选择账号槽位并登录", detail: "A–D 是本机槽位。选当前槽位可启动，选其他槽位会切换；首次登录在 Claude 中完成。")
                if vm.environmentReady {
                    Button("已了解，进入日常使用") { vm.selectJourney(.daily) }
                        .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                }
            case .daily:
                Label("继续使用 Claude", systemImage: "play.circle")
                    .font(.headline).foregroundColor(ClaudeTheme.ink)
                Text(VM.accountLetters.contains(vm.current) ? "当前槽位 \(vm.current) · 点击当前槽位启动，点击其他槽位切换。" : "当前槽位待确认，请完成环境检查。")
                    .font(.callout).fixedSize(horizontal: false, vertical: true)
                Text(vm.environmentReady ? "启动或切换前仍会重新检查。检查通过只代表本地环境，不代表账号可用。" : "环境尚未就绪，处理下面的提示后继续。")
                    .font(.caption).foregroundColor(ClaudeTheme.muted)
                if !vm.failedChecks.isEmpty && !vm.coreMissing {
                    Text("待处理：" + vm.failedChecks.map { $0.name }.joined(separator: "、"))
                        .font(.caption).foregroundColor(ClaudeTheme.error)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Claude Code：在终端运行 ~/.local/bin/claude-switch.sh cli，首次在对应槽位中单独登录。")
                    .font(.caption).foregroundColor(ClaudeTheme.muted).textSelection(.enabled)
            case .recovery:
                Label("账号受限或需要清理", systemImage: "lifepreserver")
                    .font(.headline).foregroundColor(ClaudeTheme.ink)
                Text("本工具无法判断或解除账号封禁。先区分环境故障、登录过期和官方限制提示，再决定下一步。")
                    .font(.callout).fixedSize(horizontal: false, vertical: true)
                guideStep("1", title: "只有网络或检查异常", detail: "展开环境检查；修复网络后重新检查，无需先卸载或清理。")
                Button("重新检查环境") { vm.refresh() }.disabled(vm.isBusy)
                guideStep("2", title: "Claude 明确提示账号受限", detail: "保留官方错误提示，联系官方支持。可导出脱敏环境诊断用于排查。")
                HStack {
                    Button("导出诊断…") { vm.exportDiagnostics() }.disabled(vm.settingsBusy || vm.isBusy || vm.coreMissing)
                    Button("官方支持") { openURL("https://support.claude.com") }
                }
                if !vm.settingsMessage.isEmpty {
                    Text(vm.settingsMessage).font(.caption).textSelection(.enabled)
                }
                guideStep("3", title: "确认要重置数据或卸载", detail: "先退出 Claude 与全部 Code 会话；下一页选择范围并查看实际路径。默认保留账号快照。")
                Button { vm.openCleanup() } label: {
                    Label("查看清理范围…", systemImage: "trash")
                }
                .buttonStyle(.bordered).disabled(vm.isBusy || vm.coreMissing)
                if vm.coreMissing {
                    Text("清理与诊断需要先安装核心脚本；可切到首次使用查看安装入口。")
                        .font(.caption).foregroundColor(ClaudeTheme.muted)
                }
                Text("本地清理不会解除服务端风控；移动的文件在废纸篓中保留恢复映射。")
                    .font(.caption).foregroundColor(ClaudeTheme.muted)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.cardBorder))
        .accessibilityIdentifier("journey-" + vm.journey.rawValue)
    }

    private func guideStep(_ number: String, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(number).font(.caption.bold()).foregroundColor(ClaudeTheme.terracottaDeep)
                .frame(width: 22, height: 22).background(ClaudeTheme.terracottaTint).clipShape(Circle())
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.callout.weight(.semibold))
                Text(detail).font(.caption).foregroundColor(ClaudeTheme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    var cleanupEntry: some View {
        Button { vm.openCleanup() } label: {
            HStack(spacing: 10) {
                Image(systemName: "trash").foregroundColor(ClaudeTheme.terracottaDeep)
                VStack(alignment: .leading, spacing: 3) {
                    Text("清理 Claude / Claude Code").font(.callout.weight(.semibold))
                    Text("查看程序与本地数据，选择后再确认").font(.caption).foregroundColor(ClaudeTheme.muted)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption)
            }
            .foregroundColor(ClaudeTheme.ink)
            .padding(12)
            .background(ClaudeTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(ClaudeTheme.cardBorder))
        }
        .buttonStyle(.plain)
        .disabled(vm.isBusy)
        .accessibilityIdentifier("cleanup-entry")
        .disabled(vm.coreMissing)
    }

    var statusPill: some View {
        let (txt, color): (String, Color) = {
            switch vm.phase {
            case .checking: return ("检查中", ClaudeTheme.terracottaDeep)
            case .ready:
                if vm.rows.contains(where: { $0.state == .warn }) { return ("有警告", ClaudeTheme.warning) }
                if vm.rows.contains(where: { $0.state == .skip }) { return ("通过 · 含跳过", ClaudeTheme.muted) }
                return ("检查通过", ClaudeTheme.success)
            case .failed: return ("检查未过", ClaudeTheme.error)
            case .switching: return ("切换中", ClaudeTheme.terracottaDeep)
            case .fixing: return ("修复中", ClaudeTheme.terracottaDeep)
            case .done: return ("完成", ClaudeTheme.success)
            }
        }()
        return Text(txt)
            .font(.caption.bold())
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(color.opacity(0.18))
            .foregroundColor(color)
            .clipShape(Capsule())
    }

    var checksCard: some View {
        VStack(spacing: 0) {
            ForEach(Array(vm.rows.enumerated()), id: \.element.id) { idx, row in
                HStack(spacing: 10) {
                    // 左侧图标容器
                    ZStack {
                        RoundedRectangle(cornerRadius: 6)
                            .fill(ClaudeTheme.iconWell)
                            .frame(width: 24, height: 24)
                            .overlay(
                                RoundedRectangle(cornerRadius: 6)
                                    .stroke(ClaudeTheme.divider, lineWidth: 0.5)
                            )
                        Image(systemName: row.icon)
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundColor(ClaudeTheme.muted)
                    }

                    Text(row.name)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(ClaudeTheme.ink)

                    Spacer(minLength: 8)

                    if !row.detail.isEmpty {
                        Text(row.detail)
                            .font(.system(size: 11.5, weight: .regular, design: isMonospace(row.detail) ? .monospaced : .default))
                            .foregroundColor(row.state == .warn ? ClaudeTheme.warning : ClaudeTheme.muted)
                            .lineLimit(row.name == "客户端语言" ? nil : 1)
                            .fixedSize(horizontal: false, vertical: true)
                            .truncationMode(.middle)
                            .help(row.detail)
                    }

                    stateIcon(row.state)
                }
                .padding(.vertical, 5.5)
                .padding(.horizontal, 12)

                if idx < vm.rows.count - 1 {
                    Divider()
                        .overlay(ClaudeTheme.divider)
                        .padding(.leading, 46)
                }
            }
        }
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(
            RoundedRectangle(cornerRadius: 13)
                .stroke(ClaudeTheme.cardBorder, lineWidth: 1)
        )
        .shadow(color: Color(red: 0.2, green: 0.16, blue: 0.12).opacity(0.035), radius: 6, x: 0, y: 2)
    }

    private func isMonospace(_ str: String) -> Bool {
        str.contains(".") || str.contains(":") || str.contains("/")
    }

    var clientCard: some View {
        HStack(spacing: 10) {
            ZStack {
                RoundedRectangle(cornerRadius: 6)
                    .fill(ClaudeTheme.iconWell)
                    .frame(width: 24, height: 24)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(ClaudeTheme.divider, lineWidth: 0.5))
                Image(systemName: "network")
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundColor(ClaudeTheme.muted)
            }
            Text("代理客户端").font(.system(size: 13, weight: .medium)).foregroundColor(ClaudeTheme.ink)
            Spacer()
            if vm.isSwitchingClient {
                ProgressView().scaleEffect(0.6).frame(width: 16, height: 16)
            }
            Picker("", selection: Binding(
                get: { vm.currentClient },
                set: { vm.setClient($0) }
            )) {
                if !VM.clientChoices.contains(where: { $0.key == vm.currentClient }) {
                    Text(vm.currentClient.isEmpty ? (vm.coreMissing ? "安装后选择" : "尚未识别") : vm.currentClient)
                        .tag(vm.currentClient)
                }
                ForEach(VM.clientChoices, id: \.key) { choice in
                    Text(choice.label).tag(choice.key)
                }
            }
            .labelsHidden()
            .frame(width: 150)
            .disabled(vm.isBusy || vm.isSwitchingClient || vm.coreMissing)
        }
        .padding(.vertical, 7)
        .padding(.horizontal, 12)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(ClaudeTheme.cardBorder, lineWidth: 1))
        .shadow(color: Color(red: 0.2, green: 0.16, blue: 0.12).opacity(0.035), radius: 6, x: 0, y: 2)
    }

    func stateIcon(_ s: RowState) -> some View {
        Group {
            switch s {
            case .pending: ProgressView().scaleEffect(0.55).frame(width: 16, height: 16)
            case .ok: Image(systemName: "checkmark.circle.fill").foregroundColor(ClaudeTheme.success)
            case .warn: Image(systemName: "exclamationmark.triangle.fill").foregroundColor(ClaudeTheme.warning)
            case .fail: Image(systemName: "xmark.octagon.fill").foregroundColor(ClaudeTheme.error)
            case .skip: Image(systemName: "minus.circle").foregroundColor(ClaudeTheme.muted)
            }
        }
        .font(.callout)
    }

    var accountCard: some View {
        VStack(spacing: 12) {
            if vm.phase == .switching {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(ClaudeTheme.ink)
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else if vm.phase == .done {
                VStack(spacing: 4) {
                    Label(vm.banner, systemImage: "checkmark.circle.fill").foregroundColor(ClaudeTheme.success).font(.callout.bold())
                    if !vm.note.isEmpty { Text(vm.note).font(.caption).foregroundColor(ClaudeTheme.warning).multilineTextAlignment(.center) }
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else {
                HStack(spacing: 6) {
                    Text("当前账号").font(.caption).foregroundColor(ClaudeTheme.muted)
                    Text(vm.current).font(.caption.bold())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(ClaudeTheme.terracottaTint).foregroundColor(ClaudeTheme.terracottaDeep)
                        .clipShape(Capsule())
                    Spacer()
                }
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                    ForEach(VM.accountLetters, id: \.self) { t in
                        accountButton(t, subtitle: accountSubtitle(t))
                    }
                }
            }
        }
        .padding(12)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.cardBorder))
    }

    func accountSubtitle(_ t: String) -> String {
        if vm.current == t { return "当前" }
        guard vm.archived.contains(t) else { return "首次登录" }
        guard let stamp = vm.stamps[t] else { return "已建档" }
        return "已建档 · \(stamp.size) · \(shortDate(stamp.date))"
    }

    func shortDate(_ value: String) -> String {
        let parts = value.split(separator: "-", omittingEmptySubsequences: false)
        guard parts.count == 3, parts[0].count == 4 else { return value }
        return "\(parts[1])-\(parts[2])"
    }

    func accountButton(_ t: String, subtitle: String) -> some View {
        Button { vm.switchTo(t) } label: {
            VStack(spacing: 4) {
                Text(vm.current == t ? "启动 \(t)" : "切换到 \(t)").font(.headline).foregroundColor(vm.current == t ? .white : ClaudeTheme.ink)
                Text(subtitle).font(.caption2).foregroundColor(vm.current == t ? .white.opacity(0.9) : ClaudeTheme.muted)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 14)
            .background(
                vm.current == t
                ? LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .top, endPoint: .bottom)
                : LinearGradient(colors: [ClaudeTheme.iconWell, ClaudeTheme.iconWell], startPoint: .top, endPoint: .bottom)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(vm.current == t ? ClaudeTheme.terracotta : ClaudeTheme.cardBorder, lineWidth: 1.2))
        }
        .buttonStyle(.plain)
        .disabled(vm.isBusy)
    }

    var setupCard: some View {
        VStack(spacing: 10) {
            Text("还差一步：安装核心脚本").font(.callout.bold()).foregroundColor(ClaudeTheme.ink)
            Text(vm.banner).font(.caption).foregroundColor(ClaudeTheme.muted).multilineTextAlignment(.center)
            HStack(spacing: 10) {
                Button { openURL("https://github.com/imchao9/claude-switcher/releases/latest") } label: {
                    Label("打开下载页", systemImage: "arrow.down.circle")
                        .font(.callout.bold())
                        .padding(.horizontal, 18).padding(.vertical, 8)
                        .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                        .foregroundColor(.white)
                        .clipShape(Capsule())
                }
                .buttonStyle(.plain)
                Button { openURL("https://github.com/imchao9/claude-switcher#安装") } label: {
                    Label("安装说明", systemImage: "book")
                        .font(.callout.bold())
                        .padding(.horizontal, 18).padding(.vertical, 8)
                        .background(ClaudeTheme.iconWell)
                        .foregroundColor(ClaudeTheme.ink)
                        .clipShape(Capsule())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity)
        .background(ClaudeTheme.error.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.error.opacity(0.25)))
    }

    private func openURL(_ text: String) {
        if let url = URL(string: text) { NSWorkspace.shared.open(url) }
    }

    var retryCard: some View {
        VStack(spacing: 10) {
            if vm.phase == .fixing {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(ClaudeTheme.ink)
                }
            } else {
                if !vm.banner.isEmpty {
                    Text(vm.banner).font(.callout).foregroundColor(ClaudeTheme.error).multilineTextAlignment(.center)
                }
                Text("存在未通过项，可一键修复或手动处理后重试").font(.caption).foregroundColor(ClaudeTheme.muted)
                HStack(spacing: 10) {
                    Button { vm.fixAll() } label: {
                        Label("一键修复", systemImage: "wand.and.stars")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(vm.isBusy)
                    Button { vm.refresh() } label: {
                        Label("重新检查", systemImage: "arrow.clockwise")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(ClaudeTheme.iconWell)
                            .foregroundColor(ClaudeTheme.ink)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(vm.isBusy)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(12)
        .background(ClaudeTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(ClaudeTheme.warningBorder))
    }

    var footer: some View {
        HStack(spacing: 10) {
            Image(systemName: "lock.shield").font(.caption2)
            Text("硬性安全项必须通过；灰色跳过不表示检查通过").font(.caption2)
            Spacer()
            Button {
                vm.checkUpdate()
                vm.showUpdateSheet = true
            } label: {
                Text("检查更新").font(.caption2).foregroundColor(ClaudeTheme.muted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("检查更新")
            Button { showAIPrompt = true } label: {
                Text("AI 配置").font(.caption2).foregroundColor(ClaudeTheme.muted)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("AI 配置")
        }
        .foregroundColor(ClaudeTheme.muted)
        .sheet(isPresented: $vm.showUpdateSheet) { UpdateSheet(vm: vm) }
    }
}

// MARK: - 本地数据清理

struct CleanupView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: VM

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(vm.cleanupReview ? "确认清理范围" : "清理 Claude / Claude Code").font(.title3.bold())
                        Text("本地清理不会解除服务端风控或封禁").font(.caption).foregroundColor(ClaudeTheme.error)
                    }
                    Spacer()
                    Button("关闭") { dismiss() }.disabled(vm.cleanupBusy)
                }
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if let report = vm.cleanupReport {
                            reportView(report)
                        } else if let plan = vm.cleanupPlan {
                            if vm.cleanupReview {
                                reviewView
                            } else {
                                selectionView(plan)
                            }
                        }
                        if !vm.cleanupReview || vm.cleanupBusy {
                            Text(vm.cleanupMessage).font(.callout).foregroundColor(ClaudeTheme.ink)
                                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                HStack {
                    if vm.cleanupBusy { ProgressView().controlSize(.small) }
                    Button("刷新预览") { vm.loadCleanupPlan() }.disabled(vm.isBusy)
                    Spacer()
                    if vm.cleanupReview {
                        Button("返回选择") { vm.cleanupReview = false; vm.cleanupConfirmation = "" }.disabled(vm.cleanupBusy)
                        Button("确认移入废纸篓", role: .destructive) { vm.executeCleanup() }
                            .disabled(!vm.canExecuteCleanup)
                    } else {
                        Button("查看确认清单") { vm.cleanupReview = true; vm.cleanupConfirmation = "" }
                            .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                            .disabled(!vm.canReviewCleanup)
                    }
                }
            }.padding(20)
        }
        .frame(width: 620, height: 650)
        .preferredColorScheme(.light)
        .interactiveDismissDisabled(vm.cleanupBusy)
        .onAppear { vm.cleanupSelection = []; vm.loadCleanupPlan() }
    }

    private func selectionView(_ plan: CleanupPlan) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("先完全退出 Claude 和全部 Claude Code 会话。首次使用和日常切换无需清理；账号受限时可先导出诊断并联系官方支持。")
                .font(.callout).fixedSize(horizontal: false, vertical: true)
            ForEach(VM.cleanupScopes, id: \.key) { scope in
                VStack(alignment: .leading, spacing: 6) {
                    Toggle(isOn: Binding(get: { vm.cleanupSelection.contains(scope.key) }, set: { enabled in
                        if enabled { vm.cleanupSelection.insert(scope.key) } else { vm.cleanupSelection.remove(scope.key) }
                    })) {
                        Text(scope.label).font(.callout.bold())
                    }.disabled(vm.cleanupBusy)
                    Text(scope.detail).font(.caption).foregroundColor(ClaudeTheme.muted)
                    ForEach(plan.items.filter { $0.scope == scope.key && $0.state != "missing" }) { item in
                        pathRow(item)
                    }
                    if !plan.items.contains(where: { $0.scope == scope.key && $0.state != "missing" }) {
                        Text("未发现可处理项目").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(ClaudeTheme.card).clipShape(RoundedRectangle(cornerRadius: 10))
            }
            ForEach(plan.notes, id: \.self) { Text($0).font(.caption).foregroundColor(ClaudeTheme.muted) }
            Text("其他安装方式与残留").font(.callout.bold())
            ForEach(plan.manual, id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
            Text("钥匙串可在 macOS“密码”或“钥匙串访问”中核对 Claude 相关项目后手动移除；不要批量删除其他应用凭据。")
                .font(.caption).foregroundColor(ClaudeTheme.muted)
        }
    }

    private func pathRow(_ item: CleanupItem) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(item.path).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text(item.state == "blocked" ? "无法处理：路径不可读取或父目录包含链接" : "约 \(item.sizeLabel)")
                .font(.caption2).foregroundColor(item.state == "blocked" ? ClaudeTheme.error : ClaudeTheme.muted)
        }
    }

    private var reviewView: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("以下项目将移入废纸篓。不会自动退出正在工作的会话；检测到进程仍在运行时会停止。")
                .font(.callout).fixedSize(horizontal: false, vertical: true)
            ForEach(vm.cleanupChosen.filter { $0.state == "present" }) { pathRow($0) }
            Text("账号快照\(vm.cleanupSelection.contains("archives") ? "也会移入废纸篓" : "保留在原处，仍可能包含旧登录资料")。清空废纸篓后无法通过本工具恢复。")
                .font(.callout).foregroundColor(ClaudeTheme.error).fixedSize(horizontal: false, vertical: true)
            Text("输入“移入废纸篓”确认").font(.callout.bold())
            TextField("移入废纸篓", text: $vm.cleanupConfirmation).textFieldStyle(.roundedBorder)
                .disabled(vm.cleanupBusy).accessibilityIdentifier("cleanup-confirmation")
        }
    }

    private func reportView(_ report: CleanupReport) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(report.results.enumerated()), id: \.offset) { _, item in
                VStack(alignment: .leading, spacing: 3) {
                    Label(item.detail, systemImage: item.state == "ok" ? "checkmark.circle" : (item.state == "fail" ? "exclamationmark.triangle" : "minus.circle"))
                        .foregroundColor(item.state == "fail" ? ClaudeTheme.error : ClaudeTheme.ink)
                    Text(item.path).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                }
            }
            Text("恢复位置：\(report.recovery)\n在 Finder 中打开废纸篓，按 restore-map.json 的路径映射恢复；请先退出相关程序。")
                .font(.caption).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - 检查设置与诊断

struct CheckSettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: VM

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("检查设置").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("安全项始终强制执行；跳过不等于通过").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭")
                }

                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        GroupBox("系统首选语言") {
                            VStack(alignment: .leading, spacing: 10) {
                                if let languages = vm.languagePreferences.system {
                                    ForEach(Array(languages.enumerated()), id: \.offset) { index, language in
                                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                                            Text("\(index + 1).")
                                                .foregroundColor(ClaudeTheme.muted)
                                            Text(LanguagePreferences.displayName(language))
                                                .fixedSize(horizontal: false, vertical: true)
                                            Spacer(minLength: 6)
                                            Text(language).foregroundColor(ClaudeTheme.muted)
                                            if index == 0 {
                                                Text("首选").font(.caption2)
                                                    .foregroundColor(ClaudeTheme.terracottaDeep)
                                            }
                                        }
                                        .font(.callout)
                                    }
                                } else {
                                    Text("无法读取系统首选语言，请在系统设置中核对。")
                                        .font(.caption).foregroundColor(ClaudeTheme.warning)
                                }
                                Text("Claude 语言：\(vm.languagePreferences.claudeSummary)")
                                    .font(.caption).foregroundColor(ClaudeTheme.muted)
                                    .fixedSize(horizontal: false, vertical: true)
                                Text("按系统设置中的顺序展示，第一项为首选语言；地区格式和时区需分别设置。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                                HStack {
                                    Button("打开系统语言与地区…") { vm.openLanguageSettings() }
                                    Spacer()
                                    Button("刷新语言") { vm.loadLanguagePreferences() }
                                }
                                if !vm.languageSettingsMessage.isEmpty {
                                    Text(vm.languageSettingsMessage)
                                        .font(.caption2).foregroundColor(ClaudeTheme.muted)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .padding(.vertical, 6)
                        }

                        GroupBox("可自定义的非关键检查") {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(VM.optionalChecks, id: \.key) { item in
                                    Toggle(item.label, isOn: Binding(
                                        get: { !vm.disabledChecks.contains(item.key) },
                                        set: { enabled in
                                            if enabled { vm.disabledChecks.remove(item.key) }
                                            else { vm.disabledChecks.insert(item.key) }
                                        }
                                    ))
                                }
                                Text("关闭后会真正跳过对应探测，并以灰色“已跳过”显示。出口国家、代理/TUN、Kill-Switch、保镖、账号归档和更新校验不可关闭。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                            }
                            .padding(.vertical, 6)
                        }

                        GroupBox("失败处理与日志") {
                            VStack(alignment: .leading, spacing: 10) {
                                Toggle("将上述非关键项的失败降为警告", isOn: $vm.allowOptionalFailures)
                                Text("只影响这 6 项；不会放宽任何硬性安全项，也不会自动关闭检测。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                                Toggle("记录脱敏客户端日志", isOn: $vm.loggingEnabled)
                                Text("仅记录版本、命令、退出码、耗时和检查状态，不记录 IP、凭据或账号内容。")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
                            }
                            .padding(.vertical, 6)
                        }

                        GroupBox("问题排查") {
                            VStack(alignment: .leading, spacing: 9) {
                                Button("导出脱敏诊断日志…") { vm.exportDiagnostics() }
                                    .disabled(vm.settingsBusy)
                                Text("日志：~/Library/Logs/claude-switcher/client.log")
                                    .font(.caption2).foregroundColor(ClaudeTheme.muted).textSelection(.enabled)
                            }
                            .padding(.vertical, 6)
                        }
                    }
                }

                if !vm.settingsMessage.isEmpty {
                    Text(vm.settingsMessage).font(.caption).foregroundColor(ClaudeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    Button("恢复默认") { vm.restoreCheckDefaults() }.disabled(vm.settingsBusy)
                    Spacer()
                    if vm.settingsBusy { ProgressView().scaleEffect(0.65) }
                    Button("保存并重新检查") { vm.saveCheckConfig() }
                        .buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
                        .disabled(vm.settingsBusy || vm.isBusy)
                }
            }
            .padding(20)
        }
        .frame(width: 520, height: 610)
        .preferredColorScheme(.light)
        .onAppear {
            vm.loadLanguagePreferences()
            vm.loadCheckConfig()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            vm.loadLanguagePreferences()
        }
    }
}

// MARK: - 设备指纹体检

struct FingerprintView: View {
    @Environment(\.dismiss) private var dismiss
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("设备指纹体检").font(.headline).foregroundColor(ClaudeTheme.ink)
                Spacer()
                Button("关闭") { dismiss() }.buttonStyle(.borderedProminent).tint(ClaudeTheme.terracottaDeep)
            }
            Text("字体地区特征 + GPU/虚拟机特征；Canvas/WebGL 请用 Web 检测页")
                .font(.caption).foregroundColor(ClaudeTheme.muted)
            ScrollView {
                Text(text.isEmpty ? "正在体检…" : text)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundColor(ClaudeTheme.ink)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(20)
        .frame(width: 520, height: 420)
        .preferredColorScheme(.light)
        .background(ClaudeTheme.bgTop)
    }
}

// MARK: - 版本更新

struct UpdateSheet: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var vm: VM
    @State private var copied = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("版本更新").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("只检查，不写入任何文件").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭")
                }

                ScrollView {
                    Text(vm.updateText.isEmpty ? "尚未检查" : vm.updateText)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(ClaudeTheme.ink)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(12)
                .frame(height: 110)
                .background(ClaudeTheme.card)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(ClaudeTheme.cardBorder))

                HStack(spacing: 10) {
                    Button { vm.checkUpdate() } label: {
                        Label("重新检查", systemImage: "arrow.clockwise")
                            .font(.callout.bold())
                            .padding(.horizontal, 16).padding(.vertical, 7)
                            .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Button { copyUpdateCommand() } label: {
                        Label(copied ? "已复制" : "复制更新命令", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(.callout.bold())
                            .padding(.horizontal, 16).padding(.vertical, 7)
                            .background(ClaudeTheme.iconWell)
                            .foregroundColor(ClaudeTheme.ink)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }

                Text("复制后到终端执行即可更新；更新前会做 SHA256 校验，自检不通过会自动回滚。")
                    .font(.caption2).foregroundColor(ClaudeTheme.muted)
            }
            .padding(20)
        }
        .frame(width: 520, height: 300)
        .preferredColorScheme(.light)
    }

    private func copyUpdateCommand() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        _ = pasteboard.setString("~/.local/bin/claude-switch.sh update", forType: .string)
        copied = true
    }
}

// MARK: - AI 交接提示词

struct AIPromptSheet: View {
    @Environment(\.dismiss) private var dismiss
    let text: String
    @State private var copied = false

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("AI 交接提示词").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("复制后粘贴给本地 AI，让它先读 AGENTS.md 再动手").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("关闭")
                }

                ScrollView {
                    Text(text)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(ClaudeTheme.ink)
                        .textSelection(.enabled)
                        .lineSpacing(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(12)
                .background(ClaudeTheme.card)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(ClaudeTheme.cardBorder))

                HStack {
                    Button { copyPrompt() } label: {
                        Label(copied ? "已复制" : "复制", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 7)
                            .background(LinearGradient(colors: [ClaudeTheme.terracotta, ClaudeTheme.terracottaDeep], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }
            }
            .padding(20)
        }
        .frame(width: 520, height: 480)
        .preferredColorScheme(.light)
    }

    private func copyPrompt() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        _ = pasteboard.setString(text, forType: .string)
        copied = true
    }
}

// MARK: - 公告页

struct NoticeView: View {
    @Environment(\.dismiss) private var dismiss

    let sections: [(String, String)] = [
        ("Full-Tunnel 全流量接管", "基于 TUN 虚拟网卡的内核级流量劫持，配合 Fake-IP DNS 解析机制，包括 UDP 在内的全部出站流量在网络栈底层被强制路由至加密隧道，应用层无法旁路。"),
        ("Environment Audit 环境一致性审计", "每次启动前执行 14 项环境指纹审计：Egress IP 归属地与 ASN 属性、时区与 Geo-location 一致性、Locale、IPv6 双栈旁路、DNS Leak、WebRTC/STUN 多节点交叉探测等。硬性安全项失败即 Block-on-Fail；仅显式配置的非关键项可跳过或降级，skip 不表示通过。"),
        ("Kill-Switch 断网急停", "常驻守护进程以 10 秒周期巡检隧道健康度，隧道中断即刻终止受保护进程，将裸连窗口压缩到秒级。进程级急停，非防火墙级保证，边界详见 SECURITY.md。"),
        ("Pinned Routing 域名置顶路由", "目标服务全家族域名及其生态依赖（Turnstile 人机验证、Telemetry 遥测、Payment 支付、Support 组件）置顶锁定代理路由，优先级高于任何第三方分流规则集，免疫规则库误判。"),
        ("Session Isolation 会话隔离快照", "多账号采用完整快照隔离：各账号持有独立的 Device Fingerprint、OAuth Credential 与本地状态，切换即物理交换数据卷，账号间零交叉污染。"),
        ("Drift Self-Healing 配置漂移自愈", "针对系统时区漂移、代理组件升级导致的配置重置等常见问题，内置 Watchdog 周期性校验并自动回滚至基线状态，全程留痕、可审计。"),
        ("Anti-Tracking 反追踪卫生", "验证链接（Magic Link）闭环于受保护环境内打开；邮件追踪像素（Tracking Pixel）经由邮箱服务商代理层屏蔽；全程固定单一住宅出口，不产生 Geo 跳变。"),
    ]

    var body: some View {
        ZStack {
            ClaudeBackground()
            VStack(spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("安全防护体系公告").font(.title3.bold()).foregroundColor(ClaudeTheme.ink)
                        Text("Security Architecture Bulletin").font(.caption).foregroundColor(ClaudeTheme.muted)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(ClaudeTheme.muted)
                    }
                    .buttonStyle(.plain)
                }
                .padding(20)

                ScrollView {
                    VStack(spacing: 10) {
                        Text("本产品面向对账号环境一致性有严格要求的专业用户，基于 Fail-Closed（故障即断）原则构建，针对目标平台的 Risk Control 体系提供全链路防护，确保本地真实出口在任何场景下零暴露。")
                            .font(.callout)
                            .foregroundColor(ClaudeTheme.muted)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.bottom, 6)

                        ForEach(Array(sections.enumerated()), id: \.offset) { idx, s in
                            VStack(alignment: .leading, spacing: 5) {
                                Text("\(idx + 1). \(s.0)").font(.callout.bold()).foregroundColor(ClaudeTheme.terracottaDeep)
                                Text(s.1).font(.caption).foregroundColor(ClaudeTheme.muted).lineSpacing(3)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(ClaudeTheme.card)
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                        }

                        Text("本防护体系基于对目标平台风控机制的持续逆向分析与实战经验迭代。")
                            .font(.caption2)
                            .foregroundColor(ClaudeTheme.muted)
                            .padding(.top, 8)
                    }
                    .padding(.horizontal, 20).padding(.bottom, 20)
                }
            }
        }
        .frame(width: 460, height: 620)
        .preferredColorScheme(.light)
    }
}

// MARK: - App

class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let vm: VM
    private(set) var mainWindow: NSWindow?

    override init() {
        vm = VM()
        super.init()
    }

    // 隔离测试可注入假核心脚本，不需要修改 HOME 或接触真实账号。
    init(vm: VM) {
        self.vm = vm
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        vm.onSwitchCompleted = { [weak self] in self?.mainWindow?.orderOut(nil) }
        showMainWindow()
        vm.start()
    }

    func showMainWindow() {
        vm.prepareToShow()
        if mainWindow == nil {
            let style: NSWindow.StyleMask = [.titled, .closable, .miniaturizable]
            let titleHeight = NSWindow.frameRect(forContentRect: .zero, styleMask: style).height
            let height = min(CGFloat(900), (NSScreen.main?.visibleFrame.height ?? 900) - titleHeight)
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 440, height: height),
                styleMask: style,
                backing: .buffered, defer: false
            )
            window.title = "Claude切换器"
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.isMovableByWindowBackground = true
            window.isReleasedWhenClosed = false
            window.standardWindowButton(.zoomButton)?.isEnabled = false
            window.contentView = NSHostingView(rootView: ContentView(vm: vm, height: height))
            window.delegate = self
            window.center()
            mainWindow = window
        }
        if mainWindow?.isMiniaturized == true { mainWindow?.deminiaturize(nil) }
        mainWindow?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        vm.canQuit ? .terminateNow : .terminateCancel
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return false
    }
}

struct MenuBarContent: View {
    @ObservedObject var vm: VM
    let showWindow: () -> Void

    var body: some View {
        Text("Claude 切换器")
        Text("当前账号：\(vm.current)")
        Text("最近检查：\(vm.lastCheckSummary)")
        if vm.phase == .fixing { Text("正在修复…") }
        if vm.phase == .switching { Text("正在切换账号…") }
        Divider()
        Button("打开主窗口", action: showWindow)
            .keyboardShortcut("o")
        Button("重新检查") {
            showWindow()
            vm.refresh()
        }
        .disabled(vm.isBusy)
        Button("检查更新") {
            showWindow()
            vm.checkUpdate()
            vm.showUpdateSheet = true
        }
        Button("检查设置") {
            showWindow()
            vm.showSettingsSheet = true
        }
        Button("清理 Claude / Claude Code") {
            showWindow()
            vm.openCleanup()
        }.disabled(vm.isBusy)
        Button("导出诊断日志") { vm.exportDiagnostics() }
            .disabled(vm.settingsBusy)
        Divider()
        Button("退出 Claude 切换器") { NSApplication.shared.terminate(nil) }
            .keyboardShortcut("q")
            .disabled(!vm.canQuit)
            .onAppear { vm.refreshAccountStatus() }
    }
}

// 生产应用与 UI 测试共用完全相同的菜单和窗口管理。
struct SwitcherMenu: Scene {
    @ObservedObject var vm: VM
    let controller: AppDelegate

    private static let statusImage: NSImage = {
        let image = NSImage(systemSymbolName: "shield.lefthalf.filled", accessibilityDescription: "Claude 切换器")!
        image.isTemplate = true
        return image
    }()

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent(vm: vm, showWindow: controller.showMainWindow)
        } label: {
            Image(nsImage: Self.statusImage)
                .accessibilityLabel("Claude 切换器")
        }
        .menuBarExtraStyle(.menu)
        .commands {
            CommandGroup(replacing: .appTermination) {
                Button("退出 Claude 切换器") { NSApplication.shared.terminate(nil) }
                    .keyboardShortcut("q")
                    .disabled(!vm.canQuit)
            }
        }
    }
}

#if !CLAUDE_SWITCHER_TESTS
@main
struct SwitcherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        SwitcherMenu(vm: appDelegate.vm, controller: appDelegate)
    }
}
#endif
