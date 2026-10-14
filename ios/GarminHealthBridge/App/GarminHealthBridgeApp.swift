import HealthBridgeCore
import SwiftUI
import UniformTypeIdentifiers

@MainActor @main struct GarminHealthBridgeApp: App {
    @StateObject private var model = BridgeActions.model
    var body: some Scene {
        WindowGroup { BridgeView(model: model) }
    }
}

@MainActor struct BridgeView: View {
    @ObservedObject var model: BridgeModel
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label("Garmin 血压与测量脉搏", systemImage: "heart.text.clipboard")
                        .font(.headline)
                    Text("血压成对保存，脉搏保存为测量时的心率。")
                        .foregroundStyle(.secondary)
                }
                Section("同步") {
                    Button {
                        model.selectingFolder = true
                    } label: {
                        Label(model.folderName, systemImage: "folder")
                    }
                    .disabled(model.isWorking)
                    Button("刷新待同步记录") { model.refresh() }
                    Button {
                        Task { do { try await model.synchronize() } catch {} }
                    } label: {
                        if model.isWorking {
                            ProgressView("正在核对并同步…")
                        } else {
                            Label("授权并同步到健康", systemImage: "heart.fill")
                        }
                    }
                    .disabled(model.batch == nil || model.isWorking)
                    Toggle("打开 App 时自动同步", isOn: $model.autoSyncOnOpen)
                        .disabled(model.folderName == "未选择目录")
                    Text(model.message).font(.callout).foregroundStyle(.secondary)
                        .accessibilityIdentifier("syncMessage")
                }
                if let batch = model.batch {
                    Section("待同步：\(batch.records.count) 条") {
                        ForEach(batch.records, id: \.id) { record in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(record.instant.formatted(date: .numeric, time: .shortened))
                                    .font(.caption).foregroundStyle(.secondary)
                                Text(
                                    "\(number(record.systolic)) / \(number(record.diastolic)) mmHg"
                                )
                                .font(.title3.monospacedDigit())
                                if let pulse = record.pulse {
                                    Text("脉搏 \(number(pulse)) 次/分")
                                }
                                if record.sourceType == "MANUAL" {
                                    Text("Garmin 手动录入").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .padding(.vertical, 4)
                        }
                    }
                }
            }
            .tint(.teal)
            .navigationTitle("Garmin 血压")
            .fileImporter(isPresented: $model.selectingFolder, allowedContentTypes: [.folder]) {
                result in
                switch result {
                case .success(let url): model.chooseFolder(url)
                case .failure(let error): model.message = BridgeModel.describe(error)
                }
            }
            .task { await model.onOpen() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await model.onOpen() } }
            }
        }
    }

    private func number(_ value: Double) -> String {
        value.formatted(.number.precision(.fractionLength(0...2)))
    }
}
