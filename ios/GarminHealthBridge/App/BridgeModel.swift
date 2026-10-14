import Foundation
import HealthBridgeCore
import HealthBridgeHealthKit
import SwiftUI

@MainActor enum BridgeActions {
    static let model = BridgeModel()
}

@MainActor final class BridgeModel: ObservableObject {
    @Published var batch: BridgeBatch?
    @Published var folderName = "未选择目录"
    @Published var message = "选择 Mac 血压导出的 iCloud 目录，核对记录后同步。"
    @Published var isWorking = false
    @Published var selectingFolder = false
    @Published var autoSyncOnOpen: Bool {
        didSet { UserDefaults.standard.set(autoSyncOnOpen, forKey: "autoSyncOnOpen") }
    }

    private let bookmarkKey = "outboxBookmark"
    private var previewData: Data?
    private var synchronization: Task<String, Error>?
    private let folderImporter = FolderImporter(
        importer: Importer(
            store: HealthKitStorage(),
            ledger: FileRevisionLedger(
                directory: FileManager.default.urls(
                    for: .applicationSupportDirectory, in: .userDomainMask
                )[0].appendingPathComponent("GarminHealthBridge", isDirectory: true)
            )
        )
    )

    init() {
        autoSyncOnOpen = UserDefaults.standard.bool(forKey: "autoSyncOnOpen")
    }

    func chooseFolder(_ url: URL) {
        do {
            guard url.startAccessingSecurityScopedResource() else {
                throw BridgeError.transport("folder access not granted")
            }
            defer { url.stopAccessingSecurityScopedResource() }
            #if os(macOS)
                let options: URL.BookmarkCreationOptions = [.withSecurityScope]
            #else
                let options: URL.BookmarkCreationOptions = [.minimalBookmark]
            #endif
            let bookmark = try url.bookmarkData(
                options: options,
                includingResourceValuesForKeys: nil, relativeTo: nil)
            UserDefaults.standard.set(bookmark, forKey: bookmarkKey)
            folderName = url.lastPathComponent
            try loadPreview(in: url)
        } catch {
            message = Self.describe(error)
        }
    }

    func refresh() {
        guard !isWorking else { return }
        guard UserDefaults.standard.data(forKey: bookmarkKey) != nil else {
            batch = nil
            previewData = nil
            message = "选择 Mac 血压导出的 iCloud 目录，核对记录后同步。"
            return
        }
        do {
            let url = try resolveFolder()
            guard url.startAccessingSecurityScopedResource() else {
                throw BridgeError.transport("folder access not granted")
            }
            defer { url.stopAccessingSecurityScopedResource() }
            try loadPreview(in: url)
        } catch {
            batch = nil
            previewData = nil
            message = Self.describe(error)
        }
    }

    func onOpen() async {
        if autoSyncOnOpen {
            do { _ = try await synchronize(usePreview: false) } catch {}
        } else {
            refresh()
        }
    }

    @discardableResult
    func synchronize(usePreview: Bool = true) async throws -> String {
        if let synchronization { return try await synchronization.value }
        if usePreview && previewData == nil {
            throw BridgeError.transport("refresh the preview before synchronization")
        }
        let expected = usePreview ? previewData : nil
        let operation = Task { @MainActor in
            try await self.performSynchronization(expectedData: expected)
        }
        synchronization = operation
        isWorking = true
        defer {
            synchronization = nil
            isWorking = false
        }
        return try await operation.value
    }

    private func performSynchronization(expectedData: Data?) async throws -> String {
        do {
            let url = try resolveFolder()
            guard url.startAccessingSecurityScopedResource() else {
                throw BridgeError.transport("folder access not granted")
            }
            defer { url.stopAccessingSecurityScopedResource() }
            guard
                let result = try await folderImporter.synchronize(
                    in: url, expectedData: expectedData
                )
            else {
                batch = nil
                previewData = nil
                message = "当前没有待同步记录，等待 Mac 发布下一批。"
                return message
            }
            let summary = result.summary
            // A new Mac batch needs a fresh preview before another manual confirmation.
            batch = nil
            previewData = nil
            message =
                "已保存 \(summary.bloodPressureWritten) 条血压、\(summary.pulseWritten) 条脉搏；"
                + "\(summary.alreadyPresent) 条记录已存在。回执已交给 Mac。"
            return message
        } catch {
            message = Self.describe(error)
            throw error
        }
    }

    private func loadPreview(in url: URL) throws {
        let data = try FolderImporter.pendingData(in: url)
        let decoded = try data.map(decodeBatch)
        previewData = data
        batch = decoded
        folderName = url.lastPathComponent
        message = decoded == nil ? "当前没有待同步记录，等待 Mac 发布下一批。" : "核对测量后，授权并同步到 Apple 健康。"
    }

    private func resolveFolder() throws -> URL {
        guard let data = UserDefaults.standard.data(forKey: bookmarkKey) else {
            throw BridgeError.transport("select the outbox folder first")
        }
        var stale = false
        #if os(macOS)
            let options: URL.BookmarkResolutionOptions = [.withSecurityScope, .withoutUI]
        #else
            let options: URL.BookmarkResolutionOptions = [.withoutUI]
        #endif
        let url = try URL(
            resolvingBookmarkData: data, options: options,
            relativeTo: nil, bookmarkDataIsStale: &stale)
        if stale {
            UserDefaults.standard.removeObject(forKey: bookmarkKey)
            throw BridgeError.transport("select the outbox folder again")
        }
        return url
    }

    static func describe(_ error: Error) -> String {
        guard let error = error as? BridgeError else {
            return "无法访问数据或健康存储，请检查目录权限后重试。"
        }
        switch error {
        case .authorizationDenied:
            return "请在健康权限中允许写入血压和测量脉搏。允许读取可用于核对已有记录。"
        case .healthDataUnavailable:
            return "当前设备无法访问 Apple 健康，请在支持健康数据的 iPhone 或 iPad 上运行。"
        case .invalidPacket:
            return "待导入文件格式不完整，请让 Mac 检查导出结果。"
        case .conflict:
            return "同一时刻已有不同的健康记录，请核对后再导入。"
        case .readbackFailed:
            return "写入结果尚未核验，暂未确认此批次。请检查健康权限后再次同步。"
        case .superseded:
            return "Apple 健康已有更新版本，请刷新 Mac 发布的批次。"
        case .transport:
            return "交接目录或文件未就绪，请重新选择目录并刷新；已有回执需先由 Mac 处理。"
        case .ledger:
            return "本地同步状态无法读写，请检查设备存储与 App 的数据权限。"
        }
    }
}
