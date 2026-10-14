import Darwin
import Foundation
import HealthBridgeCore
import Testing

@MainActor final class FolderImporterTests {
    private var temporaryFolders: [URL] = []
    deinit { for folder in temporaryFolders { try? FileManager.default.removeItem(at: folder) } }

    func temporaryFolder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(
            UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false)
        temporaryFolders.append(folder)
        return folder
    }
    @Test func testMissingPendingReturnsNil() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        let result = try await FolderImporter(
            importer: Importer(store: store, ledger: MemoryRevisionLedger())
        ).synchronize(in: folder)
        #expect(result == nil)
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testSuccessfulImportPublishesExactDigestReceipt() async throws {
        let folder = try temporaryFolder()
        let data = try fixture()
        let store = MockHealthStorage()
        try data.write(to: folder.appendingPathComponent("pending.json"))
        let result = try await FolderImporter(
            importer: Importer(store: store, ledger: MemoryRevisionLedger())
        ).synchronize(in: folder, expectedData: data)
        #expect(result?.summary.bloodPressureWritten == 1)
        let receipt =
            try JSONSerialization.jsonObject(
                with: Data(contentsOf: folder.appendingPathComponent("receipt.json")))
            as! [String: Any]
        #expect(receipt["payload_sha256"] as? String == BatchDecoder.sha256(data))
        #expect(try Data(contentsOf: folder.appendingPathComponent("pending.json")) == data)
    }
    @Test func testReceiptReuseRebuildsPersistentRevisionFloor() async throws {
        let folder = try temporaryFolder()
        let ledgerFolder = try temporaryFolder()
        let data = try fixture(revision: 2, pulse: nil)
        let record = try decodeBatch(data).records[0]
        let store = MockHealthStorage()
        store.addNative(record, systolic: record.systolic, diastolic: record.diastolic)
        try data.write(to: folder.appendingPathComponent("pending.json"))
        _ = try await FolderImporter(
            importer: Importer(store: store, ledger: MemoryRevisionLedger())
        ).synchronize(in: folder)
        #expect(store.saveCalls.isEmpty)
        // Reinstallation can leave a matching cloud receipt but an empty app ledger.
        _ = try await FolderImporter(
            importer: Importer(store: store, ledger: FileRevisionLedger(directory: ledgerFolder))
        ).synchronize(in: folder)
        let restarted = Importer(store: store, ledger: FileRevisionLedger(directory: ledgerFolder))
        do {
            _ = try await restarted.importBatch(fixture(revision: 1, pulse: 67))
            Issue.record("replayed an older pulse after receipt reuse")
        } catch let error as BridgeError {
            #expect(error == .superseded(record.id))
        }
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testPartialSaveAndMissingReadbackPublishNoReceipt() async throws {
        for missingReadback in [false, true] {
            let folder = try temporaryFolder()
            let store = MockHealthStorage()
            store.failAfterBP = !missingReadback
            store.hideReadback = missingReadback
            try fixture().write(to: folder.appendingPathComponent("pending.json"))
            do {
                _ = try await FolderImporter(
                    importer: Importer(store: store, ledger: MemoryRevisionLedger())
                ).synchronize(in: folder)
                Issue.record("published failed packet")
            } catch {}
            #expect(
                !(FileManager.default.fileExists(
                    atPath: folder.appendingPathComponent("receipt.json").path)))
        }
    }
    @Test func testChangedPreviewMakesNoSave() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        try fixture().write(to: folder.appendingPathComponent("pending.json"))
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder, expectedData: fixture(revision: 2))
            Issue.record("accepted changed preview")
        } catch {}
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testPendingChangedDuringSavePublishesNoReceipt() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        let pending = folder.appendingPathComponent("pending.json")
        try fixture().write(to: pending)
        store.onSave = { try fixture(revision: 2).write(to: pending, options: .atomic) }
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder)
            Issue.record("accepted changed batch")
        } catch {}
        #expect(
            !(FileManager.default.fileExists(
                atPath: folder.appendingPathComponent("receipt.json").path)))
    }
    @Test func testRefusesPendingSymlinkFIFOAndOversizedFile() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        let pending = folder.appendingPathComponent("pending.json")
        let outside = folder.appendingPathComponent("elsewhere.json")
        try fixture().write(to: outside)
        try FileManager.default.createSymbolicLink(at: pending, withDestinationURL: outside)
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder)
            Issue.record("followed symlink")
        } catch {}
        try FileManager.default.removeItem(at: pending)
        #expect(mkfifo(pending.path, 0o600) == 0)
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder)
            Issue.record("read FIFO")
        } catch {}
        try FileManager.default.removeItem(at: pending)
        try Data(repeating: 32, count: BatchDecoder.maximumBytes + 1).write(to: pending)
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder)
            Issue.record("accepted oversized packet")
        } catch {}
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testReceiptSymlinkIsNotFollowedOrReplaced() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        try fixture().write(to: folder.appendingPathComponent("pending.json"))
        let outside = folder.appendingPathComponent("private.json")
        let marker = Data("private".utf8)
        try marker.write(to: outside)
        try FileManager.default.createSymbolicLink(
            at: folder.appendingPathComponent("receipt.json"), withDestinationURL: outside)
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder)
            Issue.record("replaced unsafe receipt")
        } catch {}
        #expect(try Data(contentsOf: outside) == marker)
        #expect(store.saveCalls.isEmpty)
    }
    @Test func testOutstandingReceiptIsReusedAndNeverOverwritten() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        let data = try fixture()
        let pending = folder.appendingPathComponent("pending.json")
        let receipt = folder.appendingPathComponent("receipt.json")
        try data.write(to: pending)
        let exchange = FolderImporter(
            importer: Importer(store: store, ledger: MemoryRevisionLedger()))
        _ = try await exchange.synchronize(in: folder)
        let original = try Data(contentsOf: receipt)
        let reused = try await exchange.synchronize(in: folder)
        #expect(reused?.summary.alreadyPresent == 1)
        #expect(store.saveCalls.count == 1)
        #expect(try Data(contentsOf: receipt) == original)
        try fixture(revision: 2).write(to: pending)
        do {
            _ = try await exchange.synchronize(in: folder)
            Issue.record("replaced outstanding receipt")
        } catch {}
        #expect(store.saveCalls.count == 1)
        #expect(try Data(contentsOf: receipt) == original)
    }
    @Test func testCompetingReceiptPublicationIsPreserved() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        let receipt = folder.appendingPathComponent("receipt.json")
        let original = Data("{\"other\":true}".utf8)
        try fixture().write(to: folder.appendingPathComponent("pending.json"))
        store.onSave = { try original.write(to: receipt) }
        do {
            _ = try await FolderImporter(
                importer: Importer(store: store, ledger: MemoryRevisionLedger())
            ).synchronize(in: folder)
            Issue.record("overwrote competing receipt")
        } catch {}
        #expect(try Data(contentsOf: receipt) == original)
    }
    @Test func testNewerPartialWriteNeverAcknowledgesOldPacket() async throws {
        let folder = try temporaryFolder()
        let store = MockHealthStorage()
        let importer = Importer(store: store, ledger: MemoryRevisionLedger())
        store.failAfterBP = true
        do {
            _ = try await importer.importBatch(fixture(revision: 2))
            Issue.record("partial import succeeded")
        } catch {}
        store.failAfterBP = false
        try fixture().write(to: folder.appendingPathComponent("pending.json"))
        do {
            _ = try await FolderImporter(importer: importer).synchronize(in: folder)
            Issue.record("ACK for superseded partial save")
        } catch {
            guard case BridgeError.superseded = error else {
                Issue.record("wrong error")
                return
            }
        }
        #expect(
            !(FileManager.default.fileExists(
                atPath: folder.appendingPathComponent("receipt.json").path)))
    }
}
