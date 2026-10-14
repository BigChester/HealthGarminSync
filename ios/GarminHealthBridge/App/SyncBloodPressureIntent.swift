import AppIntents

struct SyncBloodPressureIntent: AppIntent {
    static var title: LocalizedStringResource = "同步 Garmin 血压"
    static var description = IntentDescription("读取已选 iCloud 目录中的 Garmin 血压与脉搏，核对并保存到健康。")
    static var openAppWhenRun = true

    @MainActor func perform() async throws -> some IntentResult & ProvidesDialog {
        let message = try await BridgeActions.model.synchronize(usePreview: false)
        return .result(dialog: IntentDialog(stringLiteral: message))
    }
}

struct BloodPressureShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: SyncBloodPressureIntent(),
            phrases: ["同步 \(.applicationName) 血压"],
            shortTitle: "同步血压", systemImageName: "heart.text.clipboard")
    }
}
