import Foundation
import HealthBridgeCore
import HealthKit

/// This app is the HealthKit writer; metadata identifies the original Garmin CN measurement.
@MainActor public final class HealthKitStorage: HealthStorage {
    private let healthStore: HKHealthStore

    public init(healthStore: HKHealthStore = HKHealthStore()) { self.healthStore = healthStore }

    public static func quantityType(for kind: QuantityKind) -> HKQuantityType {
        let identifier: HKQuantityTypeIdentifier
        switch kind {
        case .systolic: identifier = .bloodPressureSystolic
        case .diastolic: identifier = .bloodPressureDiastolic
        case .pulse: identifier = .heartRate
        }
        return HKObjectType.quantityType(forIdentifier: identifier)!
    }

    /// Correlation types must not appear in authorization sets: request their child types.
    public static var authorizationTypes: Set<HKSampleType> {
        Set(QuantityKind.allCases.map { quantityType(for: $0) })
    }

    public func requestAuthorization() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw BridgeError.healthDataUnavailable }
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            healthStore.requestAuthorization(
                toShare: Self.authorizationTypes, read: Set(Self.authorizationTypes)
            ) { success, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if !success {
                    continuation.resume(throwing: BridgeError.authorizationDenied)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    public func authorizationStatus(for kind: QuantityKind) -> WriteAuthorization {
        switch healthStore.authorizationStatus(for: Self.quantityType(for: kind)) {
        case .sharingAuthorized: return .authorized
        case .sharingDenied: return .denied
        case .notDetermined: return .notDetermined
        @unknown default: return .denied
        }
    }

    public static func samples(for record: BridgeRecord, bloodPressure: Bool, pulse: Bool)
        -> [HKSample]
    {
        func metadata(_ syncID: String) -> [String: Any] {
            [
                HKMetadataKeySyncIdentifier: syncID, HKMetadataKeySyncVersion: record.revision,
                HKMetadataKeyExternalUUID: record.id,
                "HealthGarminSync.origin": "GarminCN",
                "HealthGarminSync.sourceVersion": record.sourceID,
                "HealthGarminSync.deviceSourceType": record.sourceType,
                "HealthGarminSync.multiMeasurement": record.multiMeasurement,
                "HealthGarminSync.localTimestamp": record.localTimestamp,
            ]
        }
        func quantity(_ kind: QuantityKind, _ value: Double) -> HKQuantitySample {
            let unit =
                kind == .pulse ? HKUnit.count().unitDivided(by: .minute()) : .millimeterOfMercury()
            return HKQuantitySample(
                type: quantityType(for: kind), quantity: HKQuantity(unit: unit, doubleValue: value),
                start: record.instant, end: record.instant,
                metadata: metadata(record.syncIdentifier(for: kind)))
        }
        var objects: [HKSample] = []
        if bloodPressure {
            let sys = quantity(.systolic, record.systolic)
            let dia = quantity(.diastolic, record.diastolic)
            objects.append(
                HKCorrelation(
                    type: HKObjectType.correlationType(forIdentifier: .bloodPressure)!,
                    start: record.instant, end: record.instant, objects: [sys, dia],
                    metadata: metadata(record.bloodPressureSyncIdentifier)))
        }
        if pulse, let value = record.pulse { objects.append(quantity(.pulse, value)) }
        return objects
    }

    public func saveMeasurement(_ record: BridgeRecord, bloodPressure: Bool, pulse: Bool)
        async throws
    {
        let samples = Self.samples(for: record, bloodPressure: bloodPressure, pulse: pulse)
        guard !samples.isEmpty else { return }
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            healthStore.save(samples) { success, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if !success {
                    continuation.resume(throwing: BridgeError.readbackFailed(record.id))
                } else {
                    continuation.resume()
                }
            }
        }
    }

    public func snapshot(for record: BridgeRecord) async throws -> HealthSnapshot {
        var snapshot = HealthSnapshot()
        for kind in QuantityKind.allCases {
            let found = try await query(
                type: Self.quantityType(for: kind),
                predicate: combinedPredicate(record, syncID: record.syncIdentifier(for: kind)))
            for sample in found.compactMap({ $0 as? HKQuantitySample }) {
                let unit =
                    kind == .pulse
                    ? HKUnit.count().unitDivided(by: .minute()) : .millimeterOfMercury()
                snapshot.quantities.append(
                    StoredQuantity(
                        kind: kind, value: sample.quantity.doubleValue(for: unit),
                        instant: sample.startDate, syncIdentifier: syncID(sample),
                        revision: revision(sample), ownedByBridge: isOwned(sample)))
            }
        }
        let correlations = try await query(
            type: HKObjectType.correlationType(forIdentifier: .bloodPressure)!,
            predicate: combinedPredicate(record, syncID: record.bloodPressureSyncIdentifier))
        for correlation in correlations.compactMap({ $0 as? HKCorrelation }) {
            let systolic = correlation.objects(for: Self.quantityType(for: .systolic)).compactMap {
                $0 as? HKQuantitySample
            }
            let diastolic = correlation.objects(for: Self.quantityType(for: .diastolic)).compactMap
            { $0 as? HKQuantitySample }
            guard systolic.count == 1, diastolic.count == 1 else {
                throw BridgeError.conflict(record.id)
            }
            snapshot.bloodPressures.append(
                StoredBloodPressure(
                    systolic: systolic[0].quantity.doubleValue(for: .millimeterOfMercury()),
                    diastolic: diastolic[0].quantity.doubleValue(for: .millimeterOfMercury()),
                    instant: correlation.startDate, syncIdentifier: syncID(correlation),
                    revision: revision(correlation), ownedByBridge: isOwned(correlation)))
        }
        return snapshot
    }

    public func deleteOwnedPulse(for record: BridgeRecord) async throws {
        let predicate = HKQuery.predicateForObjects(
            withMetadataKey: HKMetadataKeySyncIdentifier,
            allowedValues: [record.syncIdentifier(for: .pulse)])
        let found = try await query(type: Self.quantityType(for: .pulse), predicate: predicate)
        let owned = found.filter { isOwned($0) && (revision($0) ?? 0) <= record.revision }
        guard !owned.isEmpty else { return }
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, Error>) in
            healthStore.delete(owned) { success, error in
                if let error {
                    continuation.resume(throwing: error)
                } else if !success {
                    continuation.resume(throwing: BridgeError.readbackFailed(record.id))
                } else {
                    continuation.resume()
                }
            }
        }
    }

    private func combinedPredicate(_ record: BridgeRecord, syncID: String) -> NSPredicate {
        // Exact comparisons are checked again in Core; this narrow range admits submicrosecond Date rounding.
        let time = HKQuery.predicateForSamples(
            withStart: record.instant.addingTimeInterval(-0.000002),
            end: record.instant.addingTimeInterval(0.000002),
            options: [.strictStartDate, .strictEndDate])
        let identity = HKQuery.predicateForObjects(
            withMetadataKey: HKMetadataKeySyncIdentifier, allowedValues: [syncID])
        return NSCompoundPredicate(orPredicateWithSubpredicates: [time, identity])
    }

    private func query(type: HKSampleType, predicate: NSPredicate) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { continuation in
            let query = HKSampleQuery(
                sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit,
                sortDescriptors: nil
            ) { _, samples, error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume(returning: samples ?? [])
                }
            }
            healthStore.execute(query)
        }
    }
    private func isOwned(_ sample: HKSample) -> Bool {
        sample.sourceRevision.source.bundleIdentifier == HKSource.default().bundleIdentifier
    }
    private func syncID(_ sample: HKSample) -> String? {
        sample.metadata?[HKMetadataKeySyncIdentifier] as? String
    }
    private func revision(_ sample: HKSample) -> Int? {
        (sample.metadata?[HKMetadataKeySyncVersion] as? NSNumber)?.intValue
    }
}
