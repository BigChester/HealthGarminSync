import Foundation
import HealthBridgeCore
import HealthBridgeHealthKit
import HealthKit
import Testing

@MainActor final class HealthKitMappingTests {
    @Test func testAuthorizationOnlyUsesThreeQuantityTypes() {
        #expect(HealthKitStorage.authorizationTypes.count == 3)
        #expect(HealthKitStorage.authorizationTypes.allSatisfy { $0 is HKQuantityType })
        #expect(
            HealthKitStorage.authorizationTypes.contains(
                HKObjectType.quantityType(forIdentifier: .heartRate)!))
    }
    @Test func testCorrelationAndPulseHaveStableIDsUnitsAndUTCInstant() throws {
        let account = String(repeating: "a", count: 64)
        let source = "42"
        let local = "2026-10-15T00:15:30.123456+08:00"
        let id = BatchDecoder.sha256(Data("cn|\(account)|2026-10-15|\(source)".utf8))
        let object: [String: Any] = [
            "schema_version": 1, "kind": "garmin_blood_pressure",
            "batch_id": String(repeating: "b", count: 32),
            "account_id": account, "created_at": "2026-10-15T01:00:00.000Z",
            "units": ["blood_pressure": "mmHg", "pulse": "count/min"],
            "records": [
                [
                    "id": id, "revision": 3, "source_id": source,
                    "at": "2026-10-14T16:15:30.123456Z",
                    "local_timestamp": local, "source_type": "MANUAL", "systolic": 121,
                    "diastolic": 79, "pulse": 67, "multi_measurement": false,
                ]
            ],
        ]
        let record = try decodeBatch(JSONSerialization.data(withJSONObject: object)).records[0]
        // Construct sample objects only; no HKHealthStore authorization, reads or writes occur.
        let samples = HealthKitStorage.samples(for: record, bloodPressure: true, pulse: true)
        #expect(samples.count == 2)
        let correlation = try #require(samples[0] as? HKCorrelation)
        #expect(
            correlation.correlationType.identifier
                == HKCorrelationTypeIdentifier.bloodPressure.rawValue)
        #expect(correlation.objects.count == 2)
        for kind in [QuantityKind.systolic, .diastolic] {
            let sample = try #require(
                correlation.objects(for: HealthKitStorage.quantityType(for: kind)).first
                    as? HKQuantitySample)
            #expect(
                sample.quantity.doubleValue(for: .millimeterOfMercury())
                    == (kind == .systolic ? 121 : 79))
            #expect(
                sample.metadata?[HKMetadataKeySyncIdentifier] as? String
                    == record.syncIdentifier(for: kind))
            #expect(sample.metadata?[HKMetadataKeySyncVersion] as? Int == 3)
            #expect(sample.startDate == record.instant)
        }
        let pulse = try #require(samples[1] as? HKQuantitySample)
        #expect(pulse.quantityType.identifier == HKQuantityTypeIdentifier.heartRate.rawValue)
        #expect(pulse.quantity.doubleValue(for: .count().unitDivided(by: .minute())) == 67)
        #expect(pulse.startDate == record.instant)
        #expect(
            pulse.metadata?[HKMetadataKeySyncIdentifier] as? String
                == record.syncIdentifier(for: .pulse))
        #expect(pulse.metadata?["HealthGarminSync.origin"] as? String == "GarminCN")
        #expect(pulse.metadata?["HealthGarminSync.sourceVersion"] as? String == source)
        #expect(pulse.metadata?["HealthGarminSync.deviceSourceType"] as? String == "MANUAL")
    }
}
