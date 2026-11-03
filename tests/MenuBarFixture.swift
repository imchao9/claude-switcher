import SwiftUI
import AppKit

// 仅供隔离 UI 验证。没有路径参数就拒绝启动，绝不回退到真实安装的核心脚本。
final class FixtureDelegate: AppDelegate {
    override init() {
        guard CommandLine.arguments.count >= 2,
              FileManager.default.fileExists(atPath: CommandLine.arguments[1]) else {
            fatalError("隔离演示需要显式传入 fake-core.sh 路径")
        }
        super.init(vm: VM(core: CommandLine.arguments[1], languagePreferencesReader: {
            if CommandLine.arguments.contains("--language-unavailable") {
                return LanguagePreferences(system: nil, claude: nil)
            }
            return LanguagePreferences(system: ["en-US", "zh-Hans-CN"], claude: nil)
        }, languageSettingsOpener: { false }))
    }

    override func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.appearance = NSAppearance(named: CommandLine.arguments.contains("--dark") ? .darkAqua : .aqua)
        super.applicationDidFinishLaunching(notification)
        mainWindow?.title = "Claude 切换器 · 隔离演示"
        print("DEMO activation policy: \(NSApplication.shared.activationPolicy().rawValue)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            let buttons = NSApplication.shared.windows.flatMap { window in
                Self.statusButtons(in: window.contentView)
            }
            print("DEMO native status buttons: \(buttons.count)")
            for button in buttons {
                print("DEMO status icon template: \(button.image?.isTemplate == true)")
                print("DEMO status label: \(button.accessibilityLabel() ?? "")")
                print("DEMO status title: \(button.accessibilityTitle() ?? "")")
                print("DEMO status help: \(button.accessibilityHelp() ?? "")")
                print("DEMO image description: \(button.image?.accessibilityDescription ?? "")")
            }
            fflush(stdout)
        }
        if CommandLine.arguments.contains("--language-settings") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.vm.showSettingsSheet = true
            }
        }
        if CommandLine.arguments.contains("--cleanup") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.vm.showCleanupSheet = true
            }
            if CommandLine.arguments.contains("--cleanup-review") || CommandLine.arguments.contains("--cleanup-result") {
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                    self.vm.cleanupSelection = ["desktop-data", "code-data"]
                    self.vm.cleanupReview = true
                    if CommandLine.arguments.contains("--cleanup-result") {
                        self.vm.cleanupConfirmation = "移入废纸篓"
                        self.vm.executeCleanup() // Only the explicitly supplied fake core is called.
                    }
                }
            }
        }
        if let index = CommandLine.arguments.firstIndex(of: "--capture"), index + 1 < CommandLine.arguments.count {
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
                let window = self.mainWindow?.attachedSheet ?? self.mainWindow
                if let view = window?.contentView,
                   let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                    view.cacheDisplay(in: view.bounds, to: bitmap)
                    if let data = bitmap.representation(using: .png, properties: [:]) {
                        try? data.write(to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
                        print("DEMO captured rendered window")
                    }
                }
                NSApplication.shared.terminate(nil)
            }
        }
        if let window = mainWindow {
            print("DEMO window frame: \(window.frame), visible screen: \(window.screen?.visibleFrame ?? .zero)")
        }
    }

    private static func statusButtons(in view: NSView?) -> [NSStatusBarButton] {
        guard let view = view else { return [] }
        return (view as? NSStatusBarButton).map { [$0] } ?? view.subviews.flatMap { statusButtons(in: $0) }
    }
}

@main
struct MenuBarFixture: App {
    @NSApplicationDelegateAdaptor(FixtureDelegate.self) var appDelegate

    var body: some Scene {
        SwitcherMenu(vm: appDelegate.vm, controller: appDelegate)
            .commands {
                // 同一份生产菜单内容；供无法定位 AXExtrasMenuBar 的 UI 工具验收。
                CommandMenu("菜单内容预览") {
                    MenuBarContent(vm: appDelegate.vm, showWindow: appDelegate.showMainWindow)
                }
                CommandMenu("演示外观") {
                    Button("浅色") { NSApplication.shared.appearance = NSAppearance(named: .aqua) }
                    Button("深色") { NSApplication.shared.appearance = NSAppearance(named: .darkAqua) }
                }
            }
    }
}
