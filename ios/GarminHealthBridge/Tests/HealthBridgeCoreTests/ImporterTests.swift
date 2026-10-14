import Foundation
import HealthBridgeCore
import Testing

@MainActor final class ImporterTests {
    @Test func testPairedSaveAndIdempotentReimport() async throws {
        let store = MockHealthStorage()
        let data = try fixture()
        let importer = Importer(store: store, ledger: MemoryRevisionLedger())
        let first = try await importer.importBatch(data)
        #expect(first.summary.bloodPressureWritten == 1)
        #expect(first.summary.pulseWritten == 1)
        #expect(store.saveCalls.count == 1)
        #expect(store.saveCalls[0].0 && store.saveCalls[0].1)
        #expect(store.state.bloodPressures.count == 1)
        #expect(store.state.quantities.count == 3)
        #expect(store.state.quantities.first { $0.kind == .pulse }?.value == 67)
        let second = try await importer.importBatch(data)
        #expect(second.summary.alreadyPresent == 1)
        #expect(store.saveCalls.count == 1)
        let receipt =
            try JSONSerialization.jsonObject(with: first.receipt.encoded()) as! [String: Any]
        #expect(receipt["payload_sha256"] as? String == BatchDecoder.sha256(data))
        #expect(receipt["kind"] as? String == "garmin_blood_pressure_receipt")
    }
    @Test func testDeniedAuthorizationMakesNoSave() async throws {
        for kind in QuantityKind.allCases {
            let store = MockHealthStorage()
            store.authorizations[kind] = .denied
            do {
                _ = try await Importer(store: store, ledger: MemoryRevisionLedger()).importBatch(
                    fixture())
                Issue.record("accepted denied authorization")
            } catch { #expect(error as? BridgeError == .authorizationDenied) }
            #expect(store.saveCalls.isEmpty)
        }
    }
    @Test func testNativeBPOnlyWritesMissingPulse() async throws {
        let data = try fixture()
        let record = try decodeBatch(data).records[0]
        let store = MockHealthStorage()
        store.addNative(record, systolic: 121, diastolic: 79)
        let result = try await Importer(store: store, ledger: MemoryRevisionLedger()).importBatch(
            data)
        #expect(result.summary.bloodPressureWritten == 0)
        #expect(result.summary.pulseWritten == 1)
        #expect(!(store.saveCalls[0].0))
        #expect(store.saveCalls[0].1)
    }
    @Test func testNativeAllMatchingNeedsNoSave() async throws {
        let data = try fixture()
        let record = try decodeBatch(data).records[0]
        let store = MockHealthStorage()
        store.addNative(record, systolic: 121, diastolic: 79, pulse: 67)
        let result = try await Importer(store: store, ledger: MemoryRevisionLedger()).importBatch(
            data)
        #expect(result.summary.alreadyPresent == 1)
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testNativePartialOrDifferentBPConflicts() async throws {
        for dia in [Double?.none, Double?.some(80)] {
            let data = try fixture()
            let record = try decodeBatch(data).records[0]
            let store = MockHealthStorage()
            store.addNative(record, systolic: 121, diastolic: dia)
            do {
                _ = try await Importer(store: store, ledger: MemoryRevisionLedger()).importBatch(
                    data)
                Issue.record("accepted conflicting native BP")
            } catch { #expect(error as? BridgeError == .conflict(record.id)) }
            #expect(store.saveCalls.isEmpty)
        }
    }
    @Test func testPartialFailureRetriesOnlyMissingPulse() async throws {
        let store = MockHealthStorage()
        let data = try fixture()
        let importer = Importer(store: store, ledger: MemoryRevisionLedger())
        store.failAfterBP = true
        do {
            _ = try await importer.importBatch(data)
            Issue.record("ACK on partial failure")
        } catch {}
        store.failAfterBP = false
        let result = try await importer.importBatch(data)
        #expect(result.summary.bloodPressureWritten == 0)
        #expect(result.summary.pulseWritten == 1)
        #expect(store.state.quantities.count == 3)
        #expect(store.state.bloodPressures.count == 1)
    }
    @Test func testMissingReadbackFails() async throws {
        let store = MockHealthStorage()
        store.hideReadback = true
        do {
            _ = try await Importer(store: store, ledger: MemoryRevisionLedger()).importBatch(
                fixture())
            Issue.record("ACK without readback")
        } catch {
            guard case BridgeError.readbackFailed = error else {
                Issue.record("wrong error")
                return
            }
        }
    }
    @Test func testQueryFailureMakesNoSave() async throws {
        let store = MockHealthStorage()
        store.failQuery = true
        do {
            _ = try await Importer(store: store, ledger: MemoryRevisionLedger()).importBatch(
                fixture())
            Issue.record("ACK on failed query")
        } catch {}
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testVersionUpdateAndOldPacketProtection() async throws {
        let store = MockHealthStorage()
        let importer = Importer(store: store, ledger: MemoryRevisionLedger())
        let old = try fixture()
        _ = try await importer.importBatch(old)
        let newer = try fixture(
            revision: 2, pulse: 69, at: "2026-10-14T16:16:30.123456Z",
            local: "2026-10-15T00:16:30.123456+08:00", systolic: 125)
        _ = try await importer.importBatch(newer)
        do {
            _ = try await importer.importBatch(old)
            Issue.record("ACK on stale packet")
        } catch {
            guard case BridgeError.superseded = error else {
                Issue.record("wrong error")
                return
            }
        }
        #expect(store.saveCalls.count == 2)
        #expect(store.state.bloodPressures[0].systolic == 125)
        #expect(store.state.quantities.first { $0.kind == .pulse }?.value == 69)
        #expect(store.state.quantities.allSatisfy { $0.revision == 2 })
    }
    @Test func testNullPulseDeletesOnlyOwnSample() async throws {
        let store = MockHealthStorage()
        let importer = Importer(store: store, ledger: MemoryRevisionLedger())
        let old = try fixture()
        _ = try await importer.importBatch(old)
        let record = try decodeBatch(old).records[0]
        store.addNative(record, pulse: 72)
        _ = try await importer.importBatch(fixture(revision: 2, pulse: nil))
        #expect(store.deletionCalls == 1)
        let pulses = store.state.quantities.filter { $0.kind == .pulse }
        #expect(pulses.count == 1)
        #expect(!(pulses[0].ownedByBridge))
        #expect(pulses[0].value == 72)
        do {
            _ = try await importer.importBatch(old)
            Issue.record("ACK on stale packet")
        } catch {
            guard case BridgeError.superseded = error else {
                Issue.record("wrong error")
                return
            }
        }
        #expect(store.deletionCalls == 1)
    }
    @Test func testSameRevisionChangedContentConflicts() async throws {
        let store = MockHealthStorage()
        let importer = Importer(store: store, ledger: MemoryRevisionLedger())
        _ = try await importer.importBatch(fixture())
        do {
            _ = try await importer.importBatch(fixture(systolic: 122))
            Issue.record("accepted same revision with different values")
        } catch {
            guard case BridgeError.conflict = error else {
                Issue.record("wrong error")
                return
            }
        }
        #expect(store.saveCalls.count == 1)
    }
}
