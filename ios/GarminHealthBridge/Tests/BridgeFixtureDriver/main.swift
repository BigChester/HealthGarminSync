import Foundation
import HealthBridgeCore

/// An in-memory store for the offline Python↔Swift contract check, never HealthKit.
@MainActor final class FixtureHealthStorage: HealthStorage {
    private var snapshot = HealthSnapshot()
    func requestAuthorization() async throws {}
    func authorizationStatus(for kind: QuantityKind) -> WriteAuthorization { .authorized }
    func snapshot(for record: BridgeRecord) async throws -> HealthSnapshot { snapshot }
    func saveMeasurement(_ record: BridgeRecord, bloodPressure: Bool, pulse: Bool) async throws {
        if bloodPressure {
            snapshot.bloodPressures.removeAll {
                $0.syncIdentifier == record.bloodPressureSyncIdentifier
            }
            snapshot.bloodPressures.append(
                StoredBloodPressure(
                    systolic: record.systolic, diastolic: record.diastolic,
                    instant: record.instant, syncIdentifier: record.bloodPressureSyncIdentifier,
                    revision: record.revision, ownedByBridge: true))
            put(record, kind: .systolic, value: record.systolic)
            put(record, kind: .diastolic, value: record.diastolic)
        }
        if pulse, let value = record.pulse { put(record, kind: .pulse, value: value) }
    }
    private func put(_ record: BridgeRecord, kind: QuantityKind, value: Double) {
        snapshot.quantities.removeAll { $0.syncIdentifier == record.syncIdentifier(for: kind) }
        snapshot.quantities.append(
            StoredQuantity(
                kind: kind, value: value, instant: record.instant,
                syncIdentifier: record.syncIdentifier(for: kind), revision: record.revision,
                ownedByBridge: true))
    }
    func deleteOwnedPulse(for record: BridgeRecord) async throws {
        snapshot.quantities.removeAll {
            $0.ownedByBridge && $0.syncIdentifier == record.syncIdentifier(for: .pulse)
        }
    }
}

@main struct BridgeFixtureDriver {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else {
            throw BridgeError.transport("usage: BridgeFixtureDriver /path/to/temporary/folder")
        }
        let folder = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let exchange = FolderImporter(
            importer: Importer(store: FixtureHealthStorage(), ledger: MemoryRevisionLedger()))
        if let result = try await exchange.synchronize(in: folder) {
            print(
                "offline mock receipt: blood_pressure=\(result.summary.bloodPressureWritten), pulse=\(result.summary.pulseWritten), present=\(result.summary.alreadyPresent)"
            )
        } else {
            print("offline mock: no pending packet")
        }
    }
}
