import Foundation
import HealthBridgeCore

func fixture(
    revision: Int = 1, pulse: Double? = 67, count: Int = 1,
    at: String = "2026-10-14T16:15:30.123456Z",
    local: String = "2026-10-15T00:15:30.123456+08:00",
    systolic: Double = 121, mutate: ((inout [String: Any]) -> Void)? = nil
) throws -> Data {
    let account = String(repeating: "a", count: 64)
    let records: [[String: Any]] = (1...count).map { index in
        let sourceID = String(index)
        return [
            "id": BatchDecoder.sha256(Data("cn|\(account)|\(local.prefix(10))|\(sourceID)".utf8)),
            "revision": revision, "source_id": sourceID, "at": at, "local_timestamp": local,
            "source_type": "MANUAL", "systolic": systolic, "diastolic": 79,
            "pulse": pulse.map { $0 as Any } ?? NSNull(), "multi_measurement": false,
        ]
    }
    var object: [String: Any] = [
        "schema_version": 1, "kind": "garmin_blood_pressure",
        "batch_id": String(repeating: "b", count: 32), "account_id": account,
        "created_at": "2026-10-15T01:00:00.000Z",
        "units": ["blood_pressure": "mmHg", "pulse": "count/min"],
        "records": records,
    ]
    mutate?(&object)
    return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
}

func mutateRecord(_ object: inout [String: Any], _ change: (inout [String: Any]) -> Void) {
    var rows = object["records"] as! [[String: Any]]
    change(&rows[0])
    object["records"] = rows
}

enum MockError: Error { case failed }

@MainActor final class MockHealthStorage: HealthStorage {
    var authorizations: [QuantityKind: WriteAuthorization] = Dictionary(
        uniqueKeysWithValues: QuantityKind.allCases.map { ($0, .authorized) })
    var state = HealthSnapshot()
    var saveCalls: [(Bool, Bool)] = []
    var deletionCalls = 0
    var failAfterBP = false
    var failQuery = false
    var hideReadback = false
    var onSave: (() throws -> Void)?

    func requestAuthorization() async throws {}
    func authorizationStatus(for kind: QuantityKind) -> WriteAuthorization { authorizations[kind]! }
    func snapshot(for record: BridgeRecord) async throws -> HealthSnapshot {
        if failQuery { throw MockError.failed }
        if hideReadback && !saveCalls.isEmpty { return HealthSnapshot() }
        return state
    }
    func saveMeasurement(_ record: BridgeRecord, bloodPressure: Bool, pulse: Bool) async throws {
        saveCalls.append((bloodPressure, pulse))
        if bloodPressure {
            state.bloodPressures.removeAll {
                $0.ownedByBridge && $0.syncIdentifier == record.bloodPressureSyncIdentifier
            }
            state.bloodPressures.append(
                StoredBloodPressure(
                    systolic: record.systolic, diastolic: record.diastolic,
                    instant: record.instant, syncIdentifier: record.bloodPressureSyncIdentifier,
                    revision: record.revision, ownedByBridge: true))
            addOwned(record, .systolic, record.systolic)
            addOwned(record, .diastolic, record.diastolic)
            if failAfterBP { throw MockError.failed }
        }
        if pulse, let value = record.pulse { addOwned(record, .pulse, value) }
        try onSave?()
    }
    private func addOwned(_ record: BridgeRecord, _ kind: QuantityKind, _ value: Double) {
        state.quantities.removeAll {
            $0.ownedByBridge && $0.syncIdentifier == record.syncIdentifier(for: kind)
        }
        state.quantities.append(
            StoredQuantity(
                kind: kind, value: value, instant: record.instant,
                syncIdentifier: record.syncIdentifier(for: kind), revision: record.revision,
                ownedByBridge: true))
    }
    func deleteOwnedPulse(for record: BridgeRecord) async throws {
        deletionCalls += 1
        state.quantities.removeAll {
            $0.ownedByBridge && $0.syncIdentifier == record.syncIdentifier(for: .pulse)
                && ($0.revision ?? 0) <= record.revision
        }
    }
    func addNative(
        _ record: BridgeRecord, systolic: Double? = nil, diastolic: Double? = nil,
        pulse: Double? = nil
    ) {
        if let systolic {
            state.quantities.append(
                StoredQuantity(kind: .systolic, value: systolic, instant: record.instant))
        }
        if let diastolic {
            state.quantities.append(
                StoredQuantity(kind: .diastolic, value: diastolic, instant: record.instant))
        }
        if let pulse {
            state.quantities.append(
                StoredQuantity(kind: .pulse, value: pulse, instant: record.instant))
        }
    }
}
