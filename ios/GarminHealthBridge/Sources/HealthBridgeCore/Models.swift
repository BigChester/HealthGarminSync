import Foundation

public enum BridgeError: Error, Equatable, LocalizedError {
    case invalidPacket(String)
    case authorizationDenied
    case healthDataUnavailable
    case conflict(String)
    case readbackFailed(String)
    case transport(String)
    case superseded(String)
    case ledger(String)

    public var errorDescription: String? {
        switch self {
        case .invalidPacket(let message): return "Invalid import packet: \(message)"
        case .authorizationDenied:
            return "Allow writing blood pressure and measurement pulse in Health."
        case .healthDataUnavailable: return "Health data is unavailable on this device."
        case .conflict(let id): return "Existing Health measurements conflict with record \(id)."
        case .readbackFailed(let id): return "Health readback did not confirm record \(id)."
        case .transport(let message): return "Folder exchange failed: \(message)"
        case .superseded(let id):
            return
                "A newer Health revision exists for record \(id); this older packet cannot be acknowledged."
        case .ledger(let message): return "Revision protection failed: \(message)"
        }
    }
}

public struct BridgeBatch: Sendable {
    public let schemaVersion: Int
    public let kind: String
    public let batchID: String
    public let accountID: String
    public let createdAt: String
    public let records: [BridgeRecord]
    /// SHA256 of the received bytes, including whitespace and field order.
    public let payloadSHA256: String
}

public struct BridgeRecord: Sendable, Equatable {
    public let id: String
    public let revision: Int
    public let sourceID: String
    public let at: String
    public let instant: Date
    public let localTimestamp: String
    public let sourceType: String
    public let systolic: Double
    public let diastolic: Double
    public let pulse: Double?
    public let multiMeasurement: Bool

    public func syncIdentifier(for kind: QuantityKind) -> String {
        "healthgarminsync.\(id).\(kind.rawValue)"
    }

    public var bloodPressureSyncIdentifier: String {
        "healthgarminsync.\(id).blood-pressure"
    }
}

public enum QuantityKind: String, Sendable, CaseIterable {
    case systolic, diastolic, pulse
}

public struct ReceiptRecord: Encodable, Sendable, Equatable {
    public let id: String
    public let revision: Int
}

public struct EncodableReceipt: Encodable, Sendable {
    public let schemaVersion = 1
    public let kind = "garmin_blood_pressure_receipt"
    public let batchID: String
    public let payloadSHA256: String
    public let importedAt: String
    public let records: [ReceiptRecord]

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case kind
        case batchID = "batch_id"
        case payloadSHA256 = "payload_sha256"
        case importedAt = "imported_at"
        case records
    }

    public func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}

public struct ImportSummary: Sendable, Equatable {
    public var bloodPressureWritten = 0
    public var pulseWritten = 0
    /// Number of records that needed no write.
    public var alreadyPresent = 0
}

public struct ImportResult: Sendable {
    public let receipt: EncodableReceipt
    public let summary: ImportSummary
}

public enum WriteAuthorization: Sendable {
    case authorized, denied, notDetermined
}

public struct StoredQuantity: Sendable {
    public let kind: QuantityKind
    public let value: Double
    public let instant: Date
    public let syncIdentifier: String?
    public let revision: Int?
    public let ownedByBridge: Bool

    public init(
        kind: QuantityKind, value: Double, instant: Date, syncIdentifier: String? = nil,
        revision: Int? = nil, ownedByBridge: Bool = false
    ) {
        self.kind = kind
        self.value = value
        self.instant = instant
        self.syncIdentifier = syncIdentifier
        self.revision = revision
        self.ownedByBridge = ownedByBridge
    }
}

public struct StoredBloodPressure: Sendable {
    public let systolic: Double
    public let diastolic: Double
    public let instant: Date
    public let syncIdentifier: String?
    public let revision: Int?
    public let ownedByBridge: Bool

    public init(
        systolic: Double, diastolic: Double, instant: Date,
        syncIdentifier: String? = nil, revision: Int? = nil, ownedByBridge: Bool = false
    ) {
        self.systolic = systolic
        self.diastolic = diastolic
        self.instant = instant
        self.syncIdentifier = syncIdentifier
        self.revision = revision
        self.ownedByBridge = ownedByBridge
    }
}

public struct HealthSnapshot: Sendable {
    public var quantities: [StoredQuantity]
    public var bloodPressures: [StoredBloodPressure]
    public init(quantities: [StoredQuantity] = [], bloodPressures: [StoredBloodPressure] = []) {
        self.quantities = quantities
        self.bloodPressures = bloodPressures
    }
}

@MainActor public protocol HealthStorage {
    func requestAuthorization() async throws
    func authorizationStatus(for kind: QuantityKind) -> WriteAuthorization
    /// Read quantities/correlations at the record's instant AND this record's stable sync IDs.
    /// Read errors must propagate; unreadable data cannot confirm an import.
    func snapshot(for record: BridgeRecord) async throws -> HealthSnapshot
    /// Blood pressure saves both quantity children in one blood-pressure correlation.
    /// If pulse is requested, save it in the same HealthKit save call as that correlation.
    func saveMeasurement(_ record: BridgeRecord, bloodPressure: Bool, pulse: Bool) async throws
    /// Delete only this app's samples bearing this record's pulse sync ID.
    func deleteOwnedPulse(for record: BridgeRecord) async throws
}
