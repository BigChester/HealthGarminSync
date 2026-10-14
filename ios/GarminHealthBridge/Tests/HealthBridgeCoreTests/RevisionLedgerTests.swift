import Darwin
import Foundation
import HealthBridgeCore
import Testing

@MainActor final class RevisionLedgerTests {
    private var temporaryFolders: [URL] = []
    deinit { for folder in temporaryFolders { try? FileManager.default.removeItem(at: folder) } }
    private func temporaryFolder() throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
            UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        temporaryFolders.append(directory)
        return directory
    }

    @Test func testNativeBPNullPulseRemainsProtectedAcrossPersistentLedgerRecreation() async throws
    {
        let directory = try temporaryFolder()
        let store = MockHealthStorage()
        let old = try fixture()
        let record = try decodeBatch(old).records[0]
        store.addNative(record, systolic: 121, diastolic: 79)
        _ = try await Importer(store: store, ledger: FileRevisionLedger(directory: directory))
            .importBatch(old)
        #expect(store.saveCalls.count == 1)
        #expect(store.state.bloodPressures.isEmpty)
        let removed = try fixture(revision: 2, pulse: nil)
        _ = try await Importer(store: store, ledger: FileRevisionLedger(directory: directory))
            .importBatch(removed)
        #expect(store.state.quantities.filter { $0.ownedByBridge }.isEmpty)
        #expect(store.deletionCalls == 1)
        let reopened = Importer(store: store, ledger: FileRevisionLedger(directory: directory))
        do {
            _ = try await reopened.importBatch(old)
            Issue.record("stale packet resurrected pulse")
        } catch { #expect(error as? BridgeError == .superseded(record.id)) }
        #expect(store.saveCalls.count == 1)
        let retry = try await reopened.importBatch(removed)
        #expect(retry.summary.alreadyPresent == 1)
        #expect(store.state.quantities.filter { $0.ownedByBridge }.isEmpty)
        let values =
            try JSONSerialization.jsonObject(
                with: Data(contentsOf: directory.appendingPathComponent("revisions.json")))
            as! [String: Int]
        #expect(values == [record.id: 2])
        let directoryAttributes = try FileManager.default.attributesOfItem(atPath: directory.path)
        let fileAttributes = try FileManager.default.attributesOfItem(
            atPath: directory.appendingPathComponent("revisions.json").path)
        #expect(directoryAttributes[.posixPermissions] as? Int == 0o700)
        #expect(fileAttributes[.posixPermissions] as? Int == 0o600)
    }

    @Test func testHigherFailedAttemptProtectsAgainstOldPacketAndAllowsSameRevisionRetry()
        async throws
    {
        let directory = try temporaryFolder()
        let store = MockHealthStorage()
        // A query failure makes no Health modification, but the durable attempt floor is retained.
        store.failQuery = true
        do {
            _ = try await Importer(store: store, ledger: FileRevisionLedger(directory: directory))
                .importBatch(fixture(revision: 2))
            Issue.record("query failure succeeded")
        } catch {}
        #expect(store.saveCalls.isEmpty)
        store.failQuery = false
        let importer = Importer(store: store, ledger: FileRevisionLedger(directory: directory))
        do {
            _ = try await importer.importBatch(fixture())
            Issue.record("old revision accepted after newer attempt")
        } catch {
            guard case BridgeError.superseded = error else {
                Issue.record("wrong error")
                return
            }
        }
        #expect(store.saveCalls.isEmpty)
        let result = try await importer.importBatch(fixture(revision: 2))
        #expect(result.summary.bloodPressureWritten == 1)
        #expect(result.summary.pulseWritten == 1)
    }

    @Test func testPartialHigherSaveRemainsProtectedAcrossLedgerRecreation() async throws {
        let directory = try temporaryFolder()
        let store = MockHealthStorage()
        let newer = try fixture(revision: 2)
        store.failAfterBP = true
        do {
            _ = try await Importer(store: store, ledger: FileRevisionLedger(directory: directory))
                .importBatch(newer)
            Issue.record("partial save succeeded")
        } catch {}
        #expect(store.saveCalls.count == 1)
        store.failAfterBP = false
        let reopened = Importer(store: store, ledger: FileRevisionLedger(directory: directory))
        do {
            _ = try await reopened.importBatch(fixture())
            Issue.record("stale retry after partial higher save")
        } catch {
            guard case BridgeError.superseded = error else {
                Issue.record("wrong error")
                return
            }
        }
        #expect(store.saveCalls.count == 1)
        let retry = try await reopened.importBatch(newer)
        #expect(retry.summary.bloodPressureWritten == 0)
        #expect(retry.summary.pulseWritten == 1)
        #expect(store.state.quantities.count == 3)
    }

    @Test func testCorruptSymlinkAndNonregularLedgerFailBeforeAnyHealthMutation() async throws {
        let id = String(repeating: "a", count: 64)
        let corrupt = [
            "broken", "[]", "{\"unknown\":2}", "{\"\(id)\":true}", "{\"\(id)\":0}",
            "{\"\(id)\":1.0}", "{\"\(id)\":3,\"\(id)\":1}",
        ]
        for contents in corrupt {
            let directory = try temporaryFolder()
            let store = MockHealthStorage()
            try Data(contents.utf8).write(to: directory.appendingPathComponent("revisions.json"))
            do {
                _ = try await Importer(
                    store: store, ledger: FileRevisionLedger(directory: directory)
                ).importBatch(fixture())
                Issue.record("corrupt ledger accepted")
            } catch {}
            #expect(store.saveCalls.isEmpty)
            #expect(store.deletionCalls == 0)
        }
        let directory = try temporaryFolder()
        let outside = directory.appendingPathComponent("outside.json")
        let store = MockHealthStorage()
        try Data("{}".utf8).write(to: outside)
        let ledgerFile = directory.appendingPathComponent("revisions.json")
        try FileManager.default.createSymbolicLink(at: ledgerFile, withDestinationURL: outside)
        do {
            _ = try await Importer(store: store, ledger: FileRevisionLedger(directory: directory))
                .importBatch(fixture())
            Issue.record("followed ledger symlink")
        } catch {}
        #expect(try Data(contentsOf: outside) == Data("{}".utf8))
        try FileManager.default.removeItem(at: ledgerFile)
        #expect(mkfifo(ledgerFile.path, 0o600) == 0)
        do {
            _ = try await Importer(store: store, ledger: FileRevisionLedger(directory: directory))
                .importBatch(fixture())
            Issue.record("read ledger FIFO")
        } catch {}
        #expect(store.saveCalls.isEmpty)
    }

    @Test func testFileLedgerInitializationDefersIOAndDirectorySymlinkFailsClosed() async throws {
        let root = try temporaryFolder()
        let store = MockHealthStorage()
        let directory = root.appendingPathComponent("link", isDirectory: true)
        try FileManager.default.createSymbolicLink(at: directory, withDestinationURL: root)
        let ledger = FileRevisionLedger(directory: directory)
        do {
            _ = try await Importer(store: store, ledger: ledger).importBatch(fixture())
            Issue.record("followed directory symlink")
        } catch {}
        #expect(store.saveCalls.isEmpty)
        #expect(
            !FileManager.default.fileExists(
                atPath: root.appendingPathComponent("revisions.json").path))
    }

    @MainActor private final class FailingClaimLedger: RevisionLedger {
        func validate(_ records: [BridgeRecord]) throws {}
        func claim(_ records: [BridgeRecord]) throws {
            throw BridgeError.ledger("simulated I/O failure")
        }
    }
    @Test func testPersistFailureBlocksEntirePacketBeforeSaveOrDelete() async throws {
        let store = MockHealthStorage()
        do {
            _ = try await Importer(store: store, ledger: FailingClaimLedger()).importBatch(
                fixture(count: 2))
            Issue.record("modified Health before ledger save")
        } catch { #expect(error as? BridgeError == .ledger("simulated I/O failure")) }
        #expect(store.saveCalls.isEmpty)
        #expect(store.deletionCalls == 0)
    }

    @Test func testMemoryLedgerRejectsLowerAndClaimsPacketAtomically() throws {
        let ledger = MemoryRevisionLedger()
        try ledger.claim(decodeBatch(fixture(revision: 2)).records)
        let rejectedPacket = try decodeBatch(
            fixture(
                count: 2,
                mutate: {
                    var records = $0["records"] as! [[String: Any]]
                    records[1]["revision"] = 3
                    $0["records"] = records
                }))
        #expect(throws: (any Error).self) { try ledger.claim(rejectedPacket.records) }
        // The rejected packet must not claim its second record.
        let second = try decodeBatch(fixture(count: 2)).records[1]
        try ledger.claim([second])
        try ledger.claim(decodeBatch(fixture(revision: 2)).records)
    }

    @Test func testSeparateFileLedgerInstancesReloadBeforeClaims() throws {
        let directory = try temporaryFolder()
        let first = FileRevisionLedger(directory: directory)
        let second = FileRevisionLedger(directory: directory)
        let records = try decodeBatch(fixture(revision: 2, count: 2)).records
        try first.claim([records[0]])
        try second.claim([records[1]])
        let stored =
            try JSONSerialization.jsonObject(
                with: Data(contentsOf: directory.appendingPathComponent("revisions.json")))
            as! [String: Int]
        #expect(stored == [records[0].id: 2, records[1].id: 2])
    }
}
