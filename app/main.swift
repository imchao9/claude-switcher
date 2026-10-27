import SwiftUI
import AppKit

// MARK: - 模型

enum RowState: String, Decodable { case pending, ok, warn, fail, skipped }
enum CodeRoute: String, CaseIterable { case tun = "TUN 受检", antify = "Antify 诊断" }

enum CheckMode: String, CaseIterable { case desktop = "Claude 桌面版", code = "Claude Code" }

struct CodeReport: Decodable {
    struct Item: Decodable { let id: String; let status: RowState; let detail: String }
    let schema_version: Int
    let profile: String
    let checks: [Item]
    static let ids = ["cli", "profile", "proxy", "tun", "country", "subscription"]

    static func decode(_ text: String, profile: String) -> CodeReport? {
        guard let data = text.data(using: .utf8), data.count <= 16384,
              let report = try? JSONDecoder().decode(CodeReport.self, from: data),
              report.schema_version == 1, report.profile == profile,
              report.checks.count == ids.count, Set(report.checks.map(\.id)) == Set(ids),
              report.checks.allSatisfy({ $0.status != .pending && !$0.detail.isEmpty && $0.detail.count <= 300 }) else { return nil }
        return report
    }
}

struct AntifyReport: Decodable {
    let schema_version: Int
    let kind: String
    let profile: String
    let launch_supported: Bool
    let checks: [CodeReport.Item]
    static let ids = ["cli", "profile", "antify_install", "antify_extension", "antify_route", "antify_protection"]

    static func decode(_ text: String, profile: String, exit: Int32) -> AntifyReport? {
        guard let data = text.data(using: .utf8), data.count <= 16384,
              let report = try? JSONDecoder().decode(AntifyReport.self, from: data),
              report.schema_version == 1, report.kind == "antify_diagnostics", report.profile == profile,
              !report.launch_supported, report.checks.count == ids.count,
              Set(report.checks.map(\.id)) == Set(ids),
              report.checks.allSatisfy({ $0.status != .pending && !$0.detail.isEmpty && $0.detail.count <= 300 }),
              exit == (report.checks.contains { $0.status == .fail } ? 1 : 0),
              report.checks.first(where: { $0.id == "antify_route" })?.status == .warn,
              let protection = report.checks.first(where: { $0.id == "antify_protection" }),
              protection.status == .warn || protection.status == .fail else { return nil }
        return report
    }
}

struct CodeInstallReport: Decodable {
    enum Status: String, Decodable { case installed, already_present, failed, cancelled }
    let schema_version: Int
    let kind: String
    let status: Status
    let detail: String
    let version: String?

    static func decode(_ text: String, exit: Int32) -> CodeInstallReport? {
        let details: [Status: String] = [
            .installed: "Claude Code 已安装；首次使用请自行登录",
            .already_present: "已找到 Claude Code；保留现有安装，未执行或验证安装来源",
            .failed: "Claude Code 安装未完成；请检查网络后重试，现有或部分安装未删除",
            .cancelled: "Claude Code 安装已中断；现有或部分安装未删除",
        ]
        guard let data = text.data(using: .utf8), data.count <= 4096,
              let report = try? JSONDecoder().decode(CodeInstallReport.self, from: data),
              report.schema_version == 1, report.kind == "claude_code_install",
              report.detail == details[report.status] else { return nil }
        let expectedExit: Int32 = report.status == .failed ? 1 : (report.status == .cancelled ? 130 : 0)
        guard exit == expectedExit else { return nil }
        if report.status == .installed {
            guard let version = report.version, version.count <= 100,
                  version.range(of: #"\A[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}(?:-[A-Za-z0-9.-]{1,40})?(?: \(Claude Code\))?\z"#,
                                options: .regularExpression) != nil else { return nil }
        } else if report.version != nil { return nil }
        return report
    }
}

struct CheckRow: Identifiable {
    let id = UUID()
    let name: String
    let icon: String
    var state: RowState = .pending
    var detail: String = ""
}

enum Phase { case checking, ready, failed, switching, done, fixing }

final class VM: ObservableObject {
    static let desktopRows: [CheckRow] = [
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
    static let codeRows = [
        CheckRow(name: "Claude Code CLI", icon: "terminal"),
        CheckRow(name: "配置目录", icon: "folder"),
        CheckRow(name: "代理客户端", icon: "cpu"),
        CheckRow(name: "TUN 网卡", icon: "cable.connector"),
        CheckRow(name: "探针出口国家", icon: "globe.americas"),
        CheckRow(name: "订阅 IP 绑定", icon: "link"),
    ]
    static let antifyRows = [
        CheckRow(name: "Claude Code CLI", icon: "terminal"),
        CheckRow(name: "配置目录", icon: "folder"),
        CheckRow(name: "Antify 安装", icon: "app.badge.checkmark"),
        CheckRow(name: "系统扩展授权", icon: "puzzlepiece.extension"),
        CheckRow(name: "Claude 实际路由", icon: "network"),
        CheckRow(name: "DNS 与断线保护", icon: "hand.raised"),
    ]
    @Published var codeRoute: CodeRoute = .tun
    let codeOnly: Bool
    @Published var rows = VM.desktopRows
    @Published var mode: CheckMode = .desktop
    @Published var codeProfile = "default"
    @Published var subscriptionBusy = false
    @Published var codeInstallBusy = false
    @Published var codeInstallMessage = ""
    private var generation = UUID()
    var isMutating: Bool { codeInstallBusy || subscriptionBusy || phase == .fixing || phase == .switching || phase == .done }
    @Published var phase: Phase = .checking
    @Published var current = "?"
    @Published var hasB = false
    @Published var banner = ""
    @Published var note = ""
    @Published var fingerprint = ""
    @Published var subscriptionState = "未配置"
    @Published var subscriptionBanner = ""

    let core: String

    init() {
        codeOnly = Bundle.main.object(forInfoDictionaryKey: "ClaudeCodeOnly") as? Bool == true
        var path = NSHomeDirectory() + "/.local/bin/claude-switch.sh"
        if codeOnly {
            path = NSHomeDirectory() + "/.claude-switcher/code-tools/claude-switch.sh"
            mode = .code
            codeRoute = .antify
            rows = VM.antifyRows
        }
        #if DEBUG
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--preview-core"), index + 1 < arguments.count {
            path = arguments[index + 1]
        }
        if arguments.contains("--preview-code") { mode = .code; rows = VM.codeRows }
        if arguments.contains("--preview-antify") { mode = .code; codeRoute = .antify; rows = VM.antifyRows }
        #endif
        core = path
    }

    func run(_ args: [String], timeout: TimeInterval = 45, done: @escaping (String) -> Void) {
        runProcess(args, input: nil, timeout: timeout) { output, _ in done(output) }
    }

    func runWithInput(_ args: [String], input: String, timeout: TimeInterval = 45, done: @escaping (String) -> Void) {
        runProcess(args, input: input, timeout: timeout) { output, _ in done(output) }
    }

    private func runProcess(_ args: [String], input: String?, timeout: TimeInterval, done: @escaping (String, Int32) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("switcher-out-" + UUID().uuidString)
            guard FileManager.default.createFile(atPath: tmp.path, contents: nil, attributes: [.posixPermissions: 0o600]),
                  let output = try? FileHandle(forWritingTo: tmp) else {
                DispatchQueue.main.async { done("FAIL:无法准备检测输出", -1) }
                return
            }
            defer { try? output.close(); try? FileManager.default.removeItem(at: tmp) }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/bin/bash")
            process.arguments = [self.core] + args
            process.standardOutput = output
            process.standardError = output
            if args.first == "code" {
                var environment = ProcessInfo.processInfo.environment
                let paths = [NSHomeDirectory() + "/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"]
                environment["PATH"] = paths.joined(separator: ":")
                process.environment = environment
                process.standardInput = FileHandle.nullDevice
            }
            let inputPipe = input.map { _ in Pipe() }
            if let inputPipe { process.standardInput = inputPipe }
            let finished = DispatchSemaphore(value: 0)
            process.terminationHandler = { _ in finished.signal() }
            do {
                try process.run()
                if let input, let inputPipe {
                    inputPipe.fileHandleForWriting.write(input.data(using: .utf8) ?? Data())
                    inputPipe.fileHandleForWriting.closeFile()
                }
            } catch {
                DispatchQueue.main.async { done("FAIL:无法启动脚本", -1) }
                return
            }
            if finished.wait(timeout: .now() + timeout) == .timedOut {
                if process.isRunning { process.terminate() }
                if finished.wait(timeout: .now() + 3) == .timedOut, process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                }
                process.waitUntilExit()
                DispatchQueue.main.async { done("FAIL:操作超时，请重试", -1) }
                return
            }
            process.waitUntilExit()
            let data: Data
            if args.first == "code", let reader = try? FileHandle(forReadingFrom: tmp) {
                data = (try? reader.read(upToCount: 16385)) ?? Data()
                try? reader.close()
            } else {
                data = (try? Data(contentsOf: tmp)) ?? Data()
            }
            let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async { done(text, process.terminationStatus) }
        }
    }

    func installCode() {
        guard mode == .code, phase != .checking, !isMutating else { return }
        generation = UUID()
        codeInstallBusy = true
        codeInstallMessage = "正在从官方下载安装 Claude Code…"
        runProcess(["code", "install", "--json"], input: nil, timeout: 260) { output, exit in
            let report = CodeInstallReport.decode(output, exit: exit)
            self.codeInstallBusy = false
            self.codeInstallMessage = report?.detail ?? "未取得有效安装结果，请重新检查 CLI 状态后重试"
            if report?.status == .installed || report?.status == .already_present {
                self.refresh()
            }
        }
    }

    func refresh() {
        guard !isMutating else { return }
        generation = UUID()
        let request = generation
        rows = mode == .code ? (codeRoute == .antify ? VM.antifyRows : VM.codeRows) : VM.desktopRows
        if mode == .code { refreshCode(request); return }
        phase = .checking
        banner = ""
        note = ""
        for i in rows.indices { rows[i].state = .pending; rows[i].detail = "" }
        run(["checkdetail"]) { out in
            guard self.generation == request else { return }
            // 输出为空时回退到脚本落盘的结果文件(双保险)
            var raw = out
            if raw.isEmpty, let diskData = try? Data(contentsOf: URL(fileURLWithPath: NSHomeDirectory() + "/.claude-switcher/last-check.txt")), !diskData.isEmpty {
                raw = String(decoding: diskData, as: UTF8.self)
            }
            let out = raw
            var allOk = true
            var matched = 0
            let keyMap: [String: String] = [
                "代理内核": "代理内核", "TUN网卡": "TUN 网卡", "出口IP": "出口 IP",
                "IP一致": "IP 与上次一致", "时区": "时区", "地区": "地区",
                "IPv6": "IPv6 关闭", "DNS": "DNS 走隧道", "WebRTC": "WebRTC",
                "保镖": "保镖在线", "规则置顶": "规则置顶",
                "客户端语言": "客户端语言", "ASN属性": "ASN 属性", "生态可达性": "生态可达性",
            ]
            for line in out.split(separator: "\n") {
                let parts = line.split(separator: "=", maxSplits: 1).map(String.init)
                guard parts.count == 2 else { continue }
                let rhs = parts[1]
                let status = rhs.split(separator: ":", maxSplits: 1).map(String.init)
                let code = status[0]
                let detail = status.count > 1 ? status[1] : ""
                guard let rowName = keyMap[parts[0]], let idx = self.rows.firstIndex(where: { $0.name == rowName }) else { continue }
                matched += 1
                switch code {
                case "ok": self.rows[idx].state = .ok
                case "warn": self.rows[idx].state = .warn
                default: self.rows[idx].state = .fail; allOk = false
                }
                self.rows[idx].detail = detail
            }
            self.run(["status"]) { st in
                guard self.generation == request else { return }
                self.current = st.contains("current=B") ? "B" : (st.contains("current=A") ? "A" : "未初始化")
                self.hasB = st.contains("hasB=1")
                self.refreshSubscription()
                if matched < self.rows.count {
                    self.banner = "检测输出不完整(\(matched)/\(self.rows.count))，请点重新检查"
                    self.phase = .failed
                } else {
                    self.phase = allOk ? .ready : .failed
                }
            }
        }
    }

    private func refreshCode(_ request: UUID) {
        phase = .checking
        banner = ""
        let profile = codeProfile
        let route = codeRoute
        runProcess(["code", "check", profile, "--route", route == .antify ? "antify" : "tun", "--json"], input: nil, timeout: 40) { output, exit in
            guard self.generation == request else { return }
            let items: [CodeReport.Item]?
            let ids: [String]
            if route == .antify {
                items = AntifyReport.decode(output, profile: profile, exit: exit)?.checks
                ids = AntifyReport.ids
            } else {
                items = CodeReport.decode(output, profile: profile)?.checks
                ids = CodeReport.ids
            }
            guard let items else {
                for index in self.rows.indices {
                    self.rows[index].state = .skipped
                    self.rows[index].detail = "未取得有效检测结果"
                }
                self.phase = .failed
                self.banner = "检测脚本缺失、版本不兼容或运行失败。请确认已安装支持 Code 检测的脚本与助手文件，再重试。"
                return
            }
            for (index, id) in ids.enumerated() {
                let item = items.first { $0.id == id }!
                self.rows[index].state = item.status
                self.rows[index].detail = item.detail
            }
            let failed = exit != 0 || items.contains { $0.status == .fail || $0.status == .skipped }
            self.phase = failed ? .failed : .ready
            self.banner = failed ? "检查未通过，请按下方结果处理后重试。" : ""
            if exit != 0 && !items.contains(where: { $0.status == .fail }) {
                self.banner = "检测程序异常结束，结果不能视为通过，请重试。"
            }
        }
    }

    func refreshSubscription() {
        let request = generation
        run(["subscription", "status"]) { out in
            guard self.generation == request, self.mode == .desktop else { return }
            if out.contains("subscription=ok") {
                self.subscriptionState = "已配置"
            } else if out.contains("subscription=warn") {
                self.subscriptionState = "待同步"
            } else {
                self.subscriptionState = "未配置"
            }
        }
    }

    func saveSubscription(_ value: String) {
        guard mode == .desktop, phase != .checking, !isMutating else { return }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            subscriptionBanner = "请输入订阅地址"
            return
        }
        subscriptionBusy = true
        generation = UUID()
        subscriptionBanner = "正在保存并同步…"
        runWithInput(["subscription", "set"], input: trimmed + "\n") { out in
            guard out.hasPrefix("OK:") else {
                self.subscriptionBusy = false
                self.subscriptionBanner = out.isEmpty ? "订阅地址保存失败" : out
                return
            }
            self.run(["subscription", "fetch"], timeout: 45) { fetched in
                guard fetched.hasPrefix("OK:") else {
                    self.subscriptionBusy = false
                    self.subscriptionBanner = fetched
                    return
                }
                self.run(["subscription", "install"]) { installed in
                    self.subscriptionBusy = false
                    self.subscriptionBanner = installed.hasPrefix("OK:") || installed.hasPrefix("INFO:")
                        ? "订阅已同步，绑定 IP 已记录"
                        : installed
                    self.refreshSubscription()
                }
            }
        }
    }

    func clearSubscription() {
        guard mode == .desktop, phase != .checking, !isMutating else { return }
        subscriptionBusy = true
        generation = UUID()
        run(["subscription", "clear"]) { out in
            self.subscriptionBusy = false
            self.subscriptionState = "未配置"
            self.subscriptionBanner = out
        }
    }

    func fixAll() {
        guard mode == .desktop, !isMutating else { return }
        generation = UUID()
        phase = .fixing
        banner = "正在修复…（若弹出密码框请输入开机密码）"
        run(["fix"], timeout: 180) { out in
            let steps = out.split(separator: "\n").map(String.init).filter { $0.hasPrefix("FIX:") }.map { String($0.dropFirst(4)) }
            self.banner = steps.last ?? ""
            self.phase = .failed
            self.refresh()
        }
    }

    func switchTo(_ target: String) {
        guard mode == .desktop, !isMutating else { return }
        generation = UUID()
        phase = .switching
        banner = "正在切换到账号 \(target)…"
        run(["switch", target], timeout: 180) { out in
            if out.hasPrefix("FAIL:") {
                self.phase = .failed
                self.banner = String(out.dropFirst(5))
                return
            }
            var extra = ""
            if target == "B" && !self.hasB { extra = "B 号首次使用：请在打开的 Claude 里登录 B 账号，登录一次即可。" }
            if out.hasPrefix("INFO:") { extra = "" }
            self.current = target
            self.phase = .done
            self.banner = out.hasPrefix("INFO:") ? "当前已是账号 \(target)" : "已切换到账号 \(target)"
            self.note = extra
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
                NSApplication.shared.terminate(nil)
            }
        }
    }
}

// MARK: - 视图

struct ContentView: View {
    @StateObject private var vm = VM()
    @State private var showNotice = false
    @State private var showFingerprint = false
    @State private var subscriptionURL = ""

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.07, green: 0.08, blue: 0.13), Color(red: 0.10, green: 0.07, blue: 0.16)],
                           startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            VStack(spacing: 16) {
                header
                if !vm.codeOnly {
                Picker("检测对象", selection: $vm.mode) {
                    ForEach(CheckMode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .disabled(vm.isMutating)
                .onChange(of: vm.mode) { _ in vm.refresh() }
                }
                ScrollView {
                    VStack(spacing: 14) {
                        if vm.mode == .code { codeProfilePicker }
                        checksCard
                        if vm.mode == .code {
                            codeResultCard
                        } else {
                            subscriptionCard.disabled(vm.isMutating || vm.phase == .checking)
                            if vm.phase == .ready || vm.phase == .switching || vm.phase == .done {
                                accountCard.disabled(vm.subscriptionBusy)
                            } else if vm.phase == .failed || vm.phase == .fixing {
                                retryCard.disabled(vm.subscriptionBusy)
                            }
                        }
                    }
                    .padding(.bottom, 2)
                }
                footer
            }
            .padding(20)
        }
        .frame(width: 480, height: 820)
        .preferredColorScheme(.dark)
        .onAppear { vm.refresh() }
    }

    var codeProfilePicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("网络方式", selection: $vm.codeRoute) {
                ForEach(CodeRoute.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .onChange(of: vm.codeRoute) { _ in vm.refresh() }
            Text("检测配置").font(.caption.bold()).foregroundColor(.white.opacity(0.7))
            Picker("检测配置", selection: $vm.codeProfile) {
                Text("默认").tag("default")
                Text("账号 A").tag("A")
                Text("账号 B").tag("B")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .onChange(of: vm.codeProfile) { _ in vm.refresh() }
            Text("选择配置仅用于检测，不会登录或切换账号。")
                .font(.caption).foregroundColor(.white.opacity(0.5))
        }
        .disabled(vm.isMutating)
    }

    var codeResultCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !vm.codeInstallMessage.isEmpty {
                Text(vm.codeInstallBusy ? vm.codeInstallMessage : "上次安装结果：" + vm.codeInstallMessage).font(.callout).foregroundColor(.white.opacity(0.85))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if vm.codeInstallBusy {
                HStack { ProgressView().controlSize(.small); Text("正在安装，请稍候…").font(.caption) }
                Text("关闭窗口可中断安装，可能保留部分安装文件。")
                    .font(.caption).foregroundColor(.white.opacity(0.65))
            } else if vm.rows.first?.state == .fail {
                Text("安装将联网下载并运行 Claude 官方安装程序。已有 CLI 会保留；安装后需自行登录。")
                    .font(.caption).foregroundColor(.white.opacity(0.65)).fixedSize(horizontal: false, vertical: true)
                Button("安装 Claude Code（官方）") { vm.installCode() }
                    .buttonStyle(.borderedProminent)
                    .disabled(vm.isMutating || vm.phase == .checking)
            }
            if !vm.banner.isEmpty {
                Text(vm.banner).font(.callout).foregroundColor(.red.opacity(0.95)).fixedSize(horizontal: false, vertical: true)
            }
            Text("不修改系统时区；未验证登录状态和 Claude 实际请求路径。")
                .font(.callout).foregroundColor(.white.opacity(0.8)).fixedSize(horizontal: false, vertical: true)
            Text(vm.codeRoute == .antify ? "Antify 分应用规则需手动配置。系统扩展已批准不等于代理正在运行。进程 TZ 可单独配置；终端 alias 不会自动传入此窗口。" : "当前入口要求 TUN。此处不验证 DNS、断线阻断或进程时区；进程 TZ 可单独配置。终端 alias 不会自动传入此窗口。")
                .font(.caption).foregroundColor(.white.opacity(0.55)).fixedSize(horizontal: false, vertical: true)
            if vm.codeRoute == .antify {
                HStack {
                    Button("打开 Antify") {
                        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Antify.app"), configuration: NSWorkspace.OpenConfiguration())
                    }
                    Link("命令行规则指南", destination: URL(string: "https://antifyapp.com/en/guides/command-line-rules/")!)
                    Link("Kill Switch", destination: URL(string: "https://antifyapp.com/en/guides/kill-switch/")!)
                }.font(.caption).disabled(vm.codeInstallBusy)
            }
            Button { vm.refresh() } label: {
                Label(vm.phase == .checking ? "正在检查…" : "重新检查", systemImage: "arrow.clockwise")
                    .font(.callout.bold()).frame(maxWidth: .infinity).padding(.vertical, 8)
            }
            .buttonStyle(.borderedProminent)
            .tint(.indigo)
            .disabled(vm.phase == .checking || vm.isMutating)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    var subscriptionCard: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Label("独立出口订阅", systemImage: "link.badge.plus")
                    .font(.callout.bold()).foregroundColor(.white)
                Spacer()
                Text(vm.subscriptionState)
                    .font(.caption.bold())
                    .foregroundColor(vm.subscriptionState == "已配置" ? .green : .yellow)
            }
            SecureField("粘贴你的专属订阅地址", text: $subscriptionURL)
                .textFieldStyle(.roundedBorder)
                .font(.caption)
            HStack(spacing: 8) {
                Button { vm.saveSubscription(subscriptionURL) } label: {
                    Label("保存并同步", systemImage: "arrow.down.circle")
                        .font(.caption.bold())
                }
                .buttonStyle(.borderedProminent)
                Button { vm.clearSubscription(); subscriptionURL = "" } label: {
                    Label("清除", systemImage: "trash")
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                if !vm.subscriptionBanner.isEmpty {
                    Text(vm.subscriptionBanner)
                        .font(.caption2).foregroundColor(.white.opacity(0.55)).lineLimit(1)
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08)))
    }

    var header: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 12)
                    .fill(LinearGradient(colors: [Color.indigo, Color.purple], startPoint: .topLeading, endPoint: .bottomTrailing))
                    .frame(width: 44, height: 44)
                Image(systemName: "shield.lefthalf.filled").font(.title2).foregroundColor(.white)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(vm.codeOnly ? "Claude Code 环境检测" : "Claude 账号切换器").font(.title3.bold()).foregroundColor(.white)
                Text(vm.mode == .code ? "Claude Code · 环境检测" : "安全检查 · 切换 · 启动").font(.caption).foregroundColor(.white.opacity(0.55))
            }
            Spacer()
            if vm.mode == .desktop {
                Button {
                    vm.fingerprint = "正在体检…"
                    showFingerprint = true
                    vm.run(["fingerprint"], timeout: 90) { vm.fingerprint = $0 }
                } label: {
                    Image(systemName: "waveform.path.ecg").font(.title3).foregroundColor(.white.opacity(0.6))
                }
                .buttonStyle(.plain)
                .help("设备指纹体检(字体/GPU/虚拟机)")
                Button { showNotice = true } label: {
                    Image(systemName: "info.circle").font(.title3).foregroundColor(.white.opacity(0.6))
                }
                .buttonStyle(.plain)
                .help("安全防护体系公告")
            }
            statusPill
        }
        .sheet(isPresented: $showNotice) { NoticeView() }
        .sheet(isPresented: $showFingerprint) { FingerprintView(text: vm.fingerprint) }
    }

    var statusPill: some View {
        let (txt, color): (String, Color) = {
            switch vm.phase {
            case .checking: return ("检查中", .orange)
            case .ready:
                if vm.mode == .code && vm.codeRoute == .antify { return ("待核验", .yellow) }
                if vm.mode == .code { return vm.rows.contains { $0.state == .warn } ? ("检查有提醒", .yellow) : ("检查通过", .green) }
                return ("环境安全", .green)
            case .failed: return ("检查未过", .red)
            case .switching: return ("切换中", .blue)
            case .fixing: return ("修复中", .orange)
            case .done: return ("完成", .green)
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
                    Image(systemName: row.icon)
                        .frame(width: 20)
                        .foregroundColor(.white.opacity(0.65))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(row.name).font(.callout).foregroundColor(.white.opacity(0.9))
                        if !row.detail.isEmpty {
                            Text(row.detail).font(.caption).foregroundColor(.white.opacity(0.55))
                                .lineLimit(vm.mode == .code || row.name == "时区" ? nil : 1)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 4)
                    stateIcon(row.state)
                }
                .padding(.vertical, 7).padding(.horizontal, 12)
                if idx < vm.rows.count - 1 {
                    Divider().background(Color.white.opacity(0.08))
                }
            }
        }
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08)))
    }

    func stateIcon(_ s: RowState) -> some View {
        Group {
            switch s {
            case .pending: ProgressView().scaleEffect(0.55).frame(width: 16, height: 16)
            case .ok: Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
            case .warn: Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.yellow)
            case .fail: Image(systemName: "xmark.octagon.fill").foregroundColor(.red)
            case .skipped: Image(systemName: "minus.circle").foregroundColor(.gray)
            }
        }
        .font(.callout)
    }

    var accountCard: some View {
        VStack(spacing: 12) {
            if vm.phase == .switching {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(.white.opacity(0.85))
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else if vm.phase == .done {
                VStack(spacing: 4) {
                    Label(vm.banner, systemImage: "checkmark.circle.fill").foregroundColor(.green).font(.callout.bold())
                    if !vm.note.isEmpty { Text(vm.note).font(.caption).foregroundColor(.yellow.opacity(0.9)).multilineTextAlignment(.center) }
                }
                .frame(maxWidth: .infinity).padding(.vertical, 10)
            } else {
                HStack(spacing: 6) {
                    Text("当前账号").font(.caption).foregroundColor(.white.opacity(0.5))
                    Text(vm.current).font(.caption.bold())
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Color.indigo.opacity(0.3)).foregroundColor(.indigo.opacity(1.4))
                        .clipShape(Capsule())
                    Spacer()
                }
                HStack(spacing: 10) {
                    accountButton("A", subtitle: vm.current == "A" ? "当前" : "已建档")
                    accountButton("B", subtitle: vm.current == "B" ? "当前" : (vm.hasB ? "已建档" : "未初始化"))
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.white.opacity(0.08)))
    }

    func accountButton(_ t: String, subtitle: String) -> some View {
        Button { vm.switchTo(t) } label: {
            VStack(spacing: 4) {
                Text("账号 \(t)").font(.headline).foregroundColor(.white)
                Text(subtitle).font(.caption2).foregroundColor(.white.opacity(0.55))
            }
            .frame(maxWidth: .infinity).padding(.vertical, 14)
            .background(
                vm.current == t
                ? LinearGradient(colors: [Color.indigo.opacity(0.6), Color.purple.opacity(0.5)], startPoint: .top, endPoint: .bottom)
                : LinearGradient(colors: [Color.white.opacity(0.08), Color.white.opacity(0.05)], startPoint: .top, endPoint: .bottom)
            )
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(vm.current == t ? Color.indigo : Color.white.opacity(0.1), lineWidth: 1.2))
        }
        .buttonStyle(.plain)
    }

    var retryCard: some View {
        VStack(spacing: 10) {
            if vm.phase == .fixing {
                HStack(spacing: 8) {
                    ProgressView().scaleEffect(0.7)
                    Text(vm.banner).font(.callout).foregroundColor(.white.opacity(0.85))
                }
            } else {
                if !vm.banner.isEmpty {
                    Text(vm.banner).font(.callout).foregroundColor(.red.opacity(0.95)).multilineTextAlignment(.center)
                }
                Text("存在未通过项，可一键修复或手动处理后重试").font(.caption).foregroundColor(.white.opacity(0.6))
                HStack(spacing: 10) {
                    Button { vm.fixAll() } label: {
                        Label("一键修复", systemImage: "wand.and.stars")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(LinearGradient(colors: [Color.indigo, Color.purple], startPoint: .leading, endPoint: .trailing))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Button { vm.refresh() } label: {
                        Label("重新检查", systemImage: "arrow.clockwise")
                            .font(.callout.bold())
                            .padding(.horizontal, 18).padding(.vertical, 8)
                            .background(Color.white.opacity(0.1))
                            .foregroundColor(.white)
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .frame(maxWidth: .infinity)
        .padding(12)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.red.opacity(0.25)))
    }

    var footer: some View {
        HStack {
            Image(systemName: "lock.shield").font(.caption2)
            Text(vm.mode == .code ? "检测与安装工具，不启动 Claude Code 会话" : "任何一项检查不过，都不会启动 Claude").font(.caption2)
        }
        .foregroundColor(.white.opacity(0.35))
    }
}

// MARK: - 设备指纹体检

struct FingerprintView: View {
    @Environment(\.dismiss) private var dismiss
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("设备指纹体检").font(.headline).foregroundColor(.white)
                Spacer()
                Button("关闭") { dismiss() }.buttonStyle(.borderedProminent).tint(.indigo)
            }
            Text("字体地区特征 + GPU/虚拟机特征；Canvas/WebGL 请用 Web 检测页")
                .font(.caption).foregroundColor(.white.opacity(0.55))
            ScrollView {
                Text(text.isEmpty ? "正在体检…" : text)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundColor(.white.opacity(0.9))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(20)
        .frame(width: 520, height: 420)
        .background(Color(red: 0.07, green: 0.08, blue: 0.13))
    }
}

// MARK: - 公告页

struct NoticeView: View {
    @Environment(\.dismiss) private var dismiss

    let sections: [(String, String)] = [
        ("Full-Tunnel 全流量接管", "基于 TUN 虚拟网卡的内核级流量劫持，配合 Fake-IP DNS 解析机制，包括 UDP 在内的全部出站流量在网络栈底层被强制路由至加密隧道，应用层无法旁路。"),
        ("Environment Audit 环境一致性审计", "每次启动前执行 14 项环境指纹审计：Egress IP 归属地与 ASN 属性、进程时区请求有效性（实际生效待验证）、Locale、IPv6 双栈旁路、DNS Leak、WebRTC/STUN 多节点交叉探测等。任一指标不达标即 Block-on-Fail，杜绝带病启动。"),
        ("Kill-Switch 断网急停", "常驻守护进程以 10 秒周期巡检隧道健康度，隧道中断即刻终止受保护进程，将裸连窗口压缩到秒级。进程级急停，非防火墙级保证，边界详见 SECURITY.md。"),
        ("Pinned Routing 域名置顶路由", "目标服务全家族域名及其生态依赖（Turnstile 人机验证、Telemetry 遥测、Payment 支付、Support 组件）置顶锁定代理路由，优先级高于任何第三方分流规则集，免疫规则库误判。"),
        ("Session Isolation 会话隔离快照", "多账号采用完整快照隔离：各账号持有独立的 Device Fingerprint、OAuth Credential 与本地状态，切换即物理交换数据卷，账号间零交叉污染。"),
        ("Drift Self-Healing 配置漂移自愈", "系统时区由用户设置；启动 Claude 时传入进程 TZ，客户端是否实际采用仍需验证。代理配置重置可通过一键修复处理。"),
        ("Anti-Tracking 反追踪卫生", "验证链接（Magic Link）闭环于受保护环境内打开；邮件追踪像素（Tracking Pixel）经由邮箱服务商代理层屏蔽；全程固定单一住宅出口，不产生 Geo 跳变。"),
    ]

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(red: 0.07, green: 0.08, blue: 0.13), Color(red: 0.10, green: 0.07, blue: 0.16)],
                           startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            VStack(spacing: 0) {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("安全防护体系公告").font(.title3.bold()).foregroundColor(.white)
                        Text("Security Architecture Bulletin").font(.caption).foregroundColor(.white.opacity(0.5))
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.title2).foregroundColor(.white.opacity(0.4))
                    }
                    .buttonStyle(.plain)
                }
                .padding(20)

                ScrollView {
                    VStack(spacing: 10) {
                        Text("本产品面向对账号环境一致性有严格要求的专业用户，基于 Fail-Closed（故障即断）原则构建，针对目标平台的 Risk Control 体系提供全链路防护，确保本地真实出口在任何场景下零暴露。")
                            .font(.callout)
                            .foregroundColor(.white.opacity(0.75))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.bottom, 6)

                        ForEach(Array(sections.enumerated()), id: \.offset) { idx, s in
                            VStack(alignment: .leading, spacing: 5) {
                                Text("\(idx + 1). \(s.0)").font(.callout.bold()).foregroundColor(.indigo.opacity(1.3))
                                Text(s.1).font(.caption).foregroundColor(.white.opacity(0.7)).lineSpacing(3)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(Color.white.opacity(0.05))
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                        }

                        Text("本防护体系基于对目标平台风控机制的持续逆向分析与实战经验迭代。")
                            .font(.caption2)
                            .foregroundColor(.white.opacity(0.35))
                            .padding(.top, 8)
                    }
                    .padding(.horizontal, 20).padding(.bottom, 20)
                }
            }
        }
        .frame(width: 460, height: 620)
    }
}

// MARK: - App

class AppDelegate: NSObject, NSApplicationDelegate {
    // 关闭窗口即退出进程,杜绝"旧实例残留"
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct SwitcherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                .background(WindowAccessor { win in
                    guard let w = win else { return }
                    w.titlebarAppearsTransparent = true
                    w.titleVisibility = .hidden
                    w.styleMask.insert(.fullSizeContentView)
                    w.isMovableByWindowBackground = true
                    w.standardWindowButton(.zoomButton)?.isEnabled = false
                })
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
    }
}

struct WindowAccessor: NSViewRepresentable {
    let cb: (NSWindow?) -> Void
    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        DispatchQueue.main.async { self.cb(v.window) }
        return v
    }
    func updateNSView(_ nsView: NSView, context: Context) {}
}
