import Foundation
import AppKit

private let cleanupFixturePlan = #"{"version":1,"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","items":[{"id":"0","scope":"desktop-data","path":"~/Library/Application Support/Claude","state":"present","bytes":4096}],"notes":[],"manual":[]}"#

private let auditKeys = ["代理内核", "TUN网卡", "出口IP", "IP一致", "时区", "地区", "IPv6", "DNS", "WebRTC", "保镖", "规则置顶", "客户端语言", "ASN属性", "生态可达性"]

private struct TestFailure: Error, CustomStringConvertible {
    let description: String
}

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure(description: message) }
}

private func spin(until condition: () -> Bool, timeout: TimeInterval = 6) throws {
    let limit = Date().addingTimeInterval(timeout)
    while !condition(), Date() < limit {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    try expect(condition(), "等待异步结果超时")
}

private func pump(_ seconds: TimeInterval) {
    RunLoop.current.run(until: Date().addingTimeInterval(seconds))
}

private final class Fixture {
    let root: URL
    let core: URL

    init() throws {
        let trash = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("_trash")
        root = trash.appendingPathComponent("menu-bar-model-\(UUID().uuidString)")
        core = root.appendingPathComponent("fake-core.sh")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try write("audit", auditKeys.map { "\($0)=ok" }.joined(separator: "\n") + "\n")
        try write("status", "current=A hasA=1 hasB=0\n")
        try write("switch-output", "OK:已切换到账号 B\n")
        try write("core-script", """
        #!/bin/bash
        root="$(cd "$(dirname "$0")" && pwd)"
        printf '%s\\n' "$*" >> "$root/calls"
        delay() { if [ -f "$root/$1-delay" ]; then /bin/sleep "$(cat "$root/$1-delay")"; fi; }
        result() { if [ -f "$root/$1-exit" ]; then exit "$(cat "$root/$1-exit")"; fi; }
        case "$1" in
          checkdetail) delay check; cat "$root/audit"; result check ;;
          status) cat "$root/status"; result status ;;
          get-client) echo 'client=clash-verge' ;;
          get-check-config) cat "$root/check-config"; result get-config ;;
          set-check-config) printf '%s\n' "$2" > "$root/saved-config"; result set-config ;;
          switch) delay switch; cat "$root/switch-output"; result switch ;;
          fix) delay fix; echo 'FIX:演示修复完成'; result fix ;;
          cleanup-info) delay cleanup-info; cat "$root/cleanup-plan"; result cleanup-info ;;
          cleanup-data) delay cleanup-data; cat "$root/cleanup-result"; result cleanup-data ;;
          fingerprint) delay fingerprint; echo 'GPU=ok:演示设备' ;;
          *) exit 2 ;;
        esac
        """)
        try FileManager.default.moveItem(at: root.appendingPathComponent("core-script"), to: core)
    }

    deinit { try? FileManager.default.removeItem(at: root) }

    func write(_ name: String, _ value: String) throws {
        try value.write(to: root.appendingPathComponent(name), atomically: true, encoding: .utf8)
    }

    func audit(_ changes: [String: String] = [:]) throws {
        try write("audit", auditKeys.map { "\($0)=\(changes[$0] ?? "ok")" }.joined(separator: "\n") + "\n")
    }

    func vm(checkTimeout: TimeInterval = 2, operationTimeout: TimeInterval = 2) -> VM {
        VM(core: core.path, checkTimeout: checkTimeout, operationTimeout: operationTimeout,
           languagePreferencesReader: { LanguagePreferences(system: ["en-US", "zh-Hans-CN"], claude: nil) },
           languageSettingsOpener: { false })
    }

    var calls: [String] {
        ((try? String(contentsOf: root.appendingPathComponent("calls"), encoding: .utf8)) ?? "")
            .split(separator: "\n").map(String.init)
    }

    func idle(_ vm: VM) throws { try spin { !vm.isBusy } }

    func deniedSwitch(_ vm: VM) throws {
        vm.switchTo("B")
        try idle(vm)
        try expect(vm.phase == .failed, "审计异常应阻断切换")
        try expect(!calls.contains { $0.hasPrefix("switch ") }, "审计异常仍执行了 switch")
        try expect(vm.canQuit, "失败后应该可以退出")
    }
}

@main
struct MenuBarModelTests {
    static func main() {
        do {
            guard CommandLine.arguments.count == 2 else { throw TestFailure(description: "缺少测试场景") }
            let name = CommandLine.arguments[1]
            let f = try Fixture()
            switch name {
            case "cleanup_preview_only":
                try f.write("cleanup-plan", cleanupFixturePlan)
                let vm = f.vm()
                vm.loadCleanupPlan(); try f.idle(vm)
                try expect(vm.cleanupPlan != nil && vm.cleanupSelection.isEmpty, "清理范围默认应为空")
                try expect(!vm.canReviewCleanup && !vm.canExecuteCleanup, "未选择不能清理")
                try expect(f.calls == ["cleanup-info"], "预览不得执行清理或网络检查")
            case "cleanup_confirmation_gate":
                try f.write("cleanup-plan", cleanupFixturePlan)
                let vm = f.vm()
                vm.loadCleanupPlan(); try f.idle(vm)
                vm.cleanupSelection = ["desktop-data"]
                try expect(vm.canReviewCleanup, "有效项目应可预览确认清单")
                vm.executeCleanup(); try f.idle(vm)
                vm.cleanupReview = true
                vm.cleanupConfirmation = "delete"
                vm.executeCleanup(); try f.idle(vm)
                try expect(f.calls == ["cleanup-info"], "未完成二次确认仍执行了清理")
            case "cleanup_execution_and_guard":
                try f.write("cleanup-plan", cleanupFixturePlan)
                try f.write("cleanup-result", #"{"results":[{"path":"~/Library/Application Support/Claude","state":"ok","detail":"已移入废纸篓"}],"recovery":"~/.Trash/fixture","complete":true}"#)
                try f.write("cleanup-data-delay", "0.15")
                let vm = f.vm()
                vm.loadCleanupPlan(); try f.idle(vm)
                vm.cleanupSelection = ["desktop-data"]
                vm.cleanupReview = true; vm.cleanupConfirmation = "移入废纸篓"
                vm.executeCleanup()
                try expect(vm.isBusy && !vm.canQuit, "清理时必须阻止退出及其他操作")
                vm.switchTo("B"); vm.refresh()
                try f.idle(vm)
                try expect(vm.cleanupReport?.complete == true, "成功回执未展示")
                try expect(f.calls.count == 2 && f.calls.last?.hasPrefix("cleanup-data desktop-data ") == true, "执行范围不正确或发生并发操作")
                try expect(vm.cleanupPlan == nil && vm.cleanupConfirmation.isEmpty, "执行后必须丢弃令牌和确认文字")
                try expect(vm.phase == .failed && vm.lastCheckSummary.contains("重新检查"), "清理后旧检查结果失效")
            case "cleanup_failure_preserved":
                try f.write("cleanup-plan", cleanupFixturePlan)
                try f.write("cleanup-result", #"{"results":[{"path":"/Applications/Claude.app","state":"fail","detail":"权限不足"}],"recovery":"~/.Trash/fixture","complete":false}"#)
                try f.write("cleanup-data-exit", "1")
                let vm = f.vm()
                vm.loadCleanupPlan(); try f.idle(vm)
                vm.cleanupSelection = ["desktop-data"]; vm.cleanupReview = true; vm.cleanupConfirmation = "移入废纸篓"
                vm.executeCleanup(); try f.idle(vm)
                try expect(vm.cleanupReport?.results.first?.state == "fail", "非零退出不能丢失逐项失败结果")
                try expect(vm.cleanupMessage.contains("未完成"), "部分失败不能当成成功")
            case "cleanup_preflight_error":
                try f.write("cleanup-plan", cleanupFixturePlan)
                try f.write("cleanup-result", #"{"error":"请完全退出 Claude"}"#)
                try f.write("cleanup-data-exit", "1")
                let vm = f.vm()
                vm.loadCleanupPlan(); try f.idle(vm)
                vm.cleanupSelection = ["desktop-data"]; vm.cleanupReview = true; vm.cleanupConfirmation = "移入废纸篓"
                vm.executeCleanup(); try f.idle(vm)
                try expect(vm.cleanupMessage == "请完全退出 Claude", "前置失败原因应保留")
                try expect(vm.cleanupReport == nil && vm.cleanupPlan == nil, "失败后必须重新预览")
            case "cleanup_bad_plan":
                try f.write("cleanup-plan", "{}")
                let vm = f.vm()
                vm.loadCleanupPlan(); try f.idle(vm)
                try expect(vm.cleanupPlan == nil && !vm.canReviewCleanup, "无效预览不可放行")
                try expect(vm.cleanupMessage.contains("更新"), "旧核心应提示一起更新")
            case "startup_once":
                let vm = f.vm()
                vm.start(); vm.start()
                try f.idle(vm)
                try expect(f.calls == ["checkdetail", "get-client", "status"], "启动不应重复检查")
                try expect(vm.phase == .ready && vm.lastCheckSummary == "通过" && vm.current == "A", "初始状态不一致")
            case "multi_account_status":
                try f.write("status", "current=A hasA=1 hasB=1 hasC=1 hasD=0 accounts=A,B,C accA=1.2G@2026-10-20 accB=900M@2026-10-19 accC=2.0G@2026-10-18\n")
                try f.write("switch-output", "OK:已切换到账号 C\n")
                let vm = f.vm()
                vm.start(); try f.idle(vm)
                try expect(vm.archived == Set(["A", "B", "C"]), "应解析三个已建档账号")
                try expect(vm.stamps["A"] == AccountStamp(size: "1.2G", date: "2026-10-20"), "应解析账号大小与日期")
                let card = ContentView(vm: vm)
                try expect(card.accountSubtitle("A") == "当前", "当前账号副标题不正确")
                try expect(card.accountSubtitle("B") == "已建档 · 900M · 10-19", "已建档副标题应带大小与日期")
                try expect(card.accountSubtitle("D") == "未初始化", "空槽位副标题不正确")
                vm.switchTo("C"); try f.idle(vm)
                try expect(vm.current == "C" && vm.note.isEmpty, "已建档的 C 不应提示首次登录")
                try expect(f.calls.contains("switch C"), "C 应被允许切换")
            case "first_use_cd_note":
                try f.write("status", "current=A hasA=1 hasB=1 hasC=1 hasD=0 accounts=A,B,C\n")
                try f.write("switch-output", "OK:已切换到账号 D\n")
                let vm = f.vm()
                vm.start(); try f.idle(vm)
                vm.switchTo("D"); try f.idle(vm)
                try expect(vm.current == "D", "D 应被允许切换")
                try expect(vm.note.contains("D 号首次使用") && vm.note.contains("登录"), "D 首次使用应提示登录")
            case "legacy_status_fallback":
                try f.write("status", "current=B hasA=1 hasB=1\n")
                let vm = f.vm()
                vm.start(); try f.idle(vm)
                try expect(vm.current == "B" && vm.archived == Set(["A", "B"]), "旧版 status 应回退为 A/B")
            case "invalid_multi_account_status":
                try f.write("status", "current=A hasA=1 hasB=0 hasC=0 hasD=0 accounts=A,E\n")
                let vm = f.vm()
                try f.deniedSwitch(vm)
                try expect(vm.current == "未知", "非法账号列表不可进入界面")
            case "warnings_allowed":
                try f.audit(["ASN属性": "warn:演示机房"])
                let vm = f.vm()
                vm.switchTo("B")
                try f.idle(vm)
                try expect(vm.lastCheckSummary == "有警告" && vm.current == "B", "警告应展示并允许切换")
                try expect(f.calls == ["checkdetail", "get-client", "status", "switch B"], "切换前必须先完整审计")
            case "skipped_optional_allowed":
                try f.audit(["DNS": "skip:用户已禁用", "WebRTC": "skip:用户已禁用"])
                let vm = f.vm()
                vm.switchTo("B")
                try f.idle(vm)
                try expect(vm.phase == .done && vm.current == "B", "显式跳过的非关键项不应阻断切换")
                try expect(vm.rows.first { $0.name == "DNS 走隧道" }?.state == .skip, "跳过项必须显示 skip")
            case "language_system_readback":
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/defaults")
                process.arguments = ["read", "-g", "AppleLanguages"]
                let output = Pipe()
                process.standardOutput = output
                process.standardError = Pipe()
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                let expected = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String]
                let actual = LanguagePreferences.read()
                if process.terminationStatus == 0 {
                    try expect(expected != nil && actual.system == expected, "原生读取必须与系统 defaults 的语言顺序一致")
                } else {
                    try expect(actual.system == nil, "系统未配置语言时不得猜测英文")
                }
            case "language_order_and_override":
                let preferences = LanguagePreferences(system: ["en-US", "zh-Hans-CN"], claude: ["ja-JP", "en-US"])
                try expect(preferences.system == ["en-US", "zh-Hans-CN"], "系统语言应保持原顺序")
                try expect(preferences.systemSummary == "en-US", "第一项才是系统首选")
                try expect(preferences.claudeSummary == "单独设置：ja-JP", "Claude 应展示单独设置的第一项")
                try expect(LanguagePreferences.displayName("zh-Hans-CN").contains("中文"), "语言应有可读名称")
            case "language_inherited_and_unavailable":
                let inherited = LanguagePreferences(system: ["fr-FR", "en-US"], claude: nil)
                try expect(inherited.claudeSummary == "跟随系统：fr-FR", "跟随系统应使用首选项，不能查找后续英文")
                let unavailable = LanguagePreferences(system: nil, claude: nil)
                try expect(unavailable.systemSummary == "无法读取", "未知系统语言不能显示为英文或通过")
                try expect(unavailable.claudeSummary.contains("无法读取"), "无法读取系统语言时应保留未知")
            case "language_refresh_and_open":
                var current = LanguagePreferences(system: ["zh-Hans-CN", "en-US"], claude: nil)
                var opens = 0
                var canOpen = true
                let vm = VM(core: f.core.path, languagePreferencesReader: { current },
                            languageSettingsOpener: { opens += 1; return canOpen })
                vm.loadLanguagePreferences()
                try expect(vm.languagePreferences.systemSummary == "zh-Hans-CN", "应读取当前首选语言")
                current = LanguagePreferences(system: ["en-US", "zh-Hans-CN"], claude: ["en-GB"])
                vm.loadLanguagePreferences()
                try expect(vm.languagePreferences.systemSummary == "en-US", "刷新应反映系统顺序变化")
                try expect(vm.languagePreferences.claudeSummary == "单独设置：en-GB", "刷新应更新 Claude 配置")
                vm.openLanguageSettings()
                try expect(opens == 1 && vm.languageSettingsMessage.contains("重新打开"), "打开后应提示重启 Claude")
                canOpen = false
                vm.openLanguageSettings()
                try expect(opens == 2 && vm.languageSettingsMessage.contains("无法打开"), "打开失败必须给出手动路径")
                try expect(f.calls.isEmpty, "查看语言或打开设置不得调用核心脚本、修复或启动")
            case "language_audit_detail":
                let vm = f.vm()
                vm.start(); try f.idle(vm)
                let row = vm.rows.first { $0.name == "客户端语言" }
                try expect(row?.state == .ok && vm.rows.count == 14, "语言展示不得改变原有状态或检查总数")
                try expect(row?.detail.contains("系统首选：en-US") == true, "检查详情应展示系统首选语言")
                try expect(row?.detail.contains("Claude 跟随系统：en-US") == true, "检查详情应显示配置来源")
            case "language_skip_no_read":
                try f.audit(["客户端语言": "skip:用户已禁用"])
                var reads = 0
                let vm = VM(core: f.core.path, languagePreferencesReader: {
                    reads += 1
                    return LanguagePreferences(system: ["en-US"], claude: nil)
                })
                vm.start(); try f.idle(vm)
                let row = vm.rows.first { $0.name == "客户端语言" }
                try expect(reads == 0 && row?.state == .skip && row?.detail == "用户已禁用", "跳过检查时不可额外探测或改成通过")
            case "language_failure_preserved":
                try f.audit(["客户端语言": "fail:跟随系统(中文)"])
                let vm = f.vm()
                vm.start(); try f.idle(vm)
                let row = vm.rows.first { $0.name == "客户端语言" }
                try expect(row?.state == .fail && vm.phase == .failed, "附加语言信息不能覆盖原检查失败")
                try expect(row?.detail.hasPrefix("跟随系统(中文)；") == true, "原检查原因应保留")
            case "settings_round_trip":
                try f.write("check-config", #"{"disabled":["DNS"],"allowOptionalFailures":true,"logging":false}"#)
                let vm = f.vm()
                vm.loadCheckConfig()
                try spin { !vm.settingsBusy }
                try expect(vm.disabledChecks == Set(["DNS"]), "应读取已禁用的 DNS 检查")
                try expect(vm.allowOptionalFailures && !vm.loggingEnabled, "应读取非关键失败与日志设置")
                vm.disabledChecks.insert("WebRTC")
                vm.saveCheckConfig()
                try spin { !vm.settingsBusy }
                try f.idle(vm)
                let saved = try String(contentsOf: f.root.appendingPathComponent("saved-config"), encoding: .utf8)
                guard let data = saved.data(using: .utf8) else { throw TestFailure(description: "配置不是有效 UTF-8") }
                let config = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                let disabled = config?["disabled"] as? [String]
                try expect(Set(disabled ?? []) == Set(["DNS", "WebRTC"]), "保存必须保留已有禁用项并加入 WebRTC")
                try expect(config?["allowOptionalFailures"] as? Bool == true, "保存必须保留非关键失败设置")
                try expect(config?["logging"] as? Bool == false, "保存必须保留日志开关")
                try expect(f.calls.contains("checkdetail"), "保存成功后必须重新检查")
            case "local_status_refresh":
                let vm = f.vm()
                vm.refreshAccountStatus()
                try expect(f.calls.isEmpty, "菜单提前出现时不可抢占启动审计")
                vm.start(); try f.idle(vm)
                try f.write("status", "current=B hasA=1 hasB=1\n")
                vm.refreshAccountStatus(); try f.idle(vm)
                try expect(f.calls == ["checkdetail", "get-client", "status", "status"], "展开菜单只应读取本地状态")
                try expect(vm.current == "B" && vm.lastCheckSummary == "通过", "账号更新不应改变上次审计结果")
            case "fresh_audit":
                let vm = f.vm()
                vm.refresh(); try f.idle(vm)
                try f.audit(["TUN网卡": "fail:已断开"])
                try f.deniedSwitch(vm)
                try expect(f.calls.filter { $0 == "checkdetail" }.count == 2, "不可复用上次通过结果")
            case "failed_check":
                try f.audit(["WebRTC": "fail:演示失败"])
                try f.deniedSwitch(f.vm())
            case "incomplete_check":
                try f.write("audit", auditKeys.dropLast().map { "\($0)=ok" }.joined(separator: "\n"))
                try f.deniedSwitch(f.vm())
            case "duplicate_check":
                try f.write("audit", auditKeys.dropLast().map { "\($0)=ok" }.joined(separator: "\n") + "\n代理内核=ok\n")
                try f.deniedSwitch(f.vm())
            case "duplicate_overwrite":
                try f.audit(["TUN网卡": "fail:断开"])
                let s = try String(contentsOf: f.root.appendingPathComponent("audit"), encoding: .utf8)
                try f.write("audit", s + "TUN网卡=ok\n")
                try f.deniedSwitch(f.vm())
            case "empty_check":
                try f.write("audit", "")
                try f.deniedSwitch(f.vm())
            case "unknown_check_code":
                try f.audit(["TUN网卡": "unknown"])
                try f.deniedSwitch(f.vm())
            case "empty_check_code":
                try f.audit(["TUN网卡": ""])
                try f.deniedSwitch(f.vm())
            case "nonzero_check":
                try f.write("check-exit", "7")
                try f.deniedSwitch(f.vm())
            case "timeout_check":
                try f.write("check-delay", "3")
                let vm = f.vm(checkTimeout: 0.1)
                try f.deniedSwitch(vm)
                try expect(vm.lastCheckSummary == "失败" && vm.banner.contains("超时"), "超时不可展示通过")
            case "invalid_status":
                try f.write("status", "current=B hasB=1")
                let vm = f.vm()
                try f.deniedSwitch(vm)
                try expect(vm.current == "未知", "账号读取失败不可沿用旧状态")
                try expect(vm.lastCheckSummary == "失败", "账号读取失败时菜单不可显示通过")
            case "nonzero_status":
                try f.write("status-exit", "1")
                try f.deniedSwitch(f.vm())
            case "switch_note_then_failure":
                try f.write("switch-output", "NOTE:开始切换\nFAIL:模拟归档失败\n")
                let vm = f.vm()
                vm.switchTo("B"); try f.idle(vm)
                try expect(vm.phase == .failed && vm.current == "A", "NOTE 后的 FAIL 不可当作成功")
            case "empty_switch":
                try f.write("switch-output", "")
                let vm = f.vm()
                vm.switchTo("B"); try f.idle(vm)
                try expect(vm.phase == .failed && vm.current == "A", "空切换结果不可当作成功")
            case "nonzero_switch":
                try f.write("switch-exit", "4")
                let vm = f.vm()
                vm.switchTo("B"); try f.idle(vm)
                try expect(vm.phase == .failed && vm.current == "A", "非零退出即使打印 OK 也不可当作成功")
            case "timeout_switch":
                try f.write("switch-delay", "3")
                let vm = f.vm(operationTimeout: 0.1)
                vm.switchTo("B"); try f.idle(vm)
                try expect(vm.phase == .failed && vm.current == "A" && vm.canQuit, "切换超时应恢复可操作状态")
            case "busy_guard":
                try f.write("switch-delay", "0.2")
                let vm = f.vm()
                vm.switchTo("B")
                try expect(!vm.canQuit, "切换请求开始后不可退出")
                vm.switchTo("A"); vm.refresh(); vm.fixAll(); vm.inspectFingerprint()
                try f.idle(vm)
                try expect(f.calls == ["checkdetail", "get-client", "status", "switch B"], "忙碌时应忽略重复操作")
                try expect(vm.canQuit && vm.current == "B", "切换完成后应允许退出")
            case "fix_guard":
                try f.write("fix-delay", "0.2")
                let vm = f.vm()
                vm.fixAll()
                try expect(!vm.canQuit, "修复期间不可退出")
                vm.refresh(); vm.switchTo("B"); vm.fixAll()
                try f.idle(vm)
                try expect(f.calls == ["fix", "checkdetail", "get-client", "status"], "修复结束必须重新检查，不能并行执行")
                try expect(vm.canQuit && vm.phase == .ready, "修复后应该可以退出")
            case "failed_fix":
                try f.write("fix-exit", "1")
                let vm = f.vm()
                vm.fixAll(); try f.idle(vm)
                try expect(vm.phase == .failed && vm.canQuit && f.calls == ["fix"], "修复失败不可被后续状态覆盖")
            case "checking_allows_quit":
                try f.write("check-delay", "0.2")
                let vm = f.vm()
                vm.refresh()
                try expect(vm.canQuit, "普通检查期间应允许退出")
                try f.idle(vm)
            case "hide_after_success":
                let vm = f.vm()
                var hidden = false
                vm.onSwitchCompleted = { hidden = true }
                vm.switchTo("B"); try f.idle(vm)
                try expect(vm.phase == .done && !hidden, "成功提示应该短暂保留")
                try spin(until: { hidden })
                try expect(vm.phase == .ready && vm.current == "B", "成功后应恢复就绪，不应退出进程")
            case "reopen_cancels_hide":
                let vm = f.vm()
                var hidden = false
                vm.onSwitchCompleted = { hidden = true }
                vm.switchTo("B"); try f.idle(vm)
                vm.prepareToShow()
                pump(2.4)
                try expect(!hidden && vm.phase == .ready, "主动重开窗口后不能被旧回调隐藏")
            case "window_lifecycle":
                let vm = f.vm()
                let app = NSApplication.shared
                let delegate = AppDelegate(vm: vm)
                app.setActivationPolicy(.accessory)
                delegate.showMainWindow()
                let first = delegate.mainWindow
                try expect(first?.isVisible == true, "首次启动应该显示窗口")
                first?.performClose(nil)
                try expect(first?.isVisible == false, "关窗应该隐藏")
                try expect(!delegate.applicationShouldTerminateAfterLastWindowClosed(app), "关窗不应退出")
                delegate.showMainWindow()
                try expect(delegate.mainWindow === first && first?.isVisible == true, "重开应该复用同一窗口")
                delegate.mainWindow?.miniaturize(nil)
                delegate.showMainWindow()
                try expect(first?.isMiniaturized == false, "重开应该恢复最小化窗口")
                first?.orderOut(nil)
            case "termination_guard":
                let vm = f.vm()
                let delegate = AppDelegate(vm: vm)
                let app = NSApplication.shared
                try expect(delegate.applicationShouldTerminate(app) == .terminateNow, "空闲时应该允许退出")
                try f.write("fix-delay", "0.2")
                vm.fixAll()
                try expect(delegate.applicationShouldTerminate(app) == .terminateCancel, "系统退出也必须保护修复操作")
                try f.idle(vm)
                try expect(delegate.applicationShouldTerminate(app) == .terminateNow, "修复结束应该解除退出保护")
            default:
                throw TestFailure(description: "未知测试场景：\(name)")
            }
            print("PASS \(name)")
        } catch {
            fputs("FAIL: \(error)\n", stderr)
            exit(1)
        }
    }
}
