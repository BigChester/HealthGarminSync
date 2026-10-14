import Darwin
import Foundation

/// The caller manages the picked directory's security-scoped access and bookmark.
@MainActor public final class FolderImporter {
    private let importer: Importer
    private var synchronizing = false
    public init(importer: Importer) { self.importer = importer }

    public static func pendingData(in folder: URL) throws -> Data? {
        let directory = try openDirectory(folder)
        defer { close(directory) }
        return try readRegular(directory, name: "pending.json")
    }

    public func synchronize(in folder: URL, expectedData: Data? = nil) async throws -> ImportResult?
    {
        guard folder.isFileURL, !synchronizing else {
            throw BridgeError.transport("invalid folder or exchange already running")
        }
        synchronizing = true
        defer { synchronizing = false }
        let directory = try Self.openDirectory(folder)
        defer { close(directory) }
        guard let pending = try Self.readRegular(directory, name: "pending.json") else {
            return nil
        }
        guard expectedData == nil || expectedData == pending else {
            throw BridgeError.transport("pending packet changed since preview")
        }
        let batch = try decodeBatch(pending)
        try importer.validateRevisionFloor(batch)
        if let receipt = try Self.readRegular(directory, name: "receipt.json") {
            let result = try Self.existingResult(receipt, batch: batch)
            try importer.claimConfirmedReceiptFloor(batch)
            return result
        }
        let result = try await importer.importBatch(pending)
        guard try Self.readRegular(directory, name: "pending.json") == pending else {
            throw BridgeError.transport("pending packet changed during import")
        }
        try writeReceipt(
            result.receipt.encoded(), directory: directory, pending: pending, batch: batch)
        return result
    }

    private static func openDirectory(_ folder: URL) throws -> Int32 {
        guard folder.isFileURL else { throw BridgeError.transport("file URL required") }
        let descriptor = open(
            folder.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard descriptor >= 0 else { throw failure("open directory") }
        return descriptor
    }

    private static func readRegular(_ directory: Int32, name: String) throws -> Data? {
        let descriptor = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if descriptor < 0 {
            if errno == ENOENT { return nil }
            throw failure("open \(name)")
        }
        defer { close(descriptor) }
        var attributes = stat()
        guard fstat(descriptor, &attributes) == 0 else { throw failure("stat \(name)") }
        guard attributes.st_mode & S_IFMT == S_IFREG, attributes.st_size >= 0,
            attributes.st_size <= BatchDecoder.maximumBytes
        else { throw BridgeError.transport("\(name) must be a bounded regular file") }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count < 0 {
                if errno == EINTR { continue }
                throw failure("read \(name)")
            }
            if count == 0 { break }
            guard data.count + count <= BatchDecoder.maximumBytes else {
                throw BridgeError.transport("\(name) exceeds size limit")
            }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
    }

    private struct ReceivedReceipt: Decodable {
        let schemaVersion: Int
        let kind: String
        let batchID: String
        let payloadSHA256: String
        let importedAt: String
        let records: [ReceivedRecord]

        private enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version"
            case kind, records
            case batchID = "batch_id"
            case payloadSHA256 = "payload_sha256"
            case importedAt = "imported_at"
        }
    }
    private struct ReceivedRecord: Decodable, Equatable {
        let id: String
        let revision: Int
    }
    private static func existingResult(_ data: Data, batch: BridgeBatch) throws -> ImportResult {
        guard let receipt = try? JSONDecoder().decode(ReceivedReceipt.self, from: data),
            receipt.schemaVersion == 1, receipt.kind == "garmin_blood_pressure_receipt",
            receipt.batchID == batch.batchID, receipt.payloadSHA256 == batch.payloadSHA256,
            receipt.importedAt.hasSuffix("Z"),
            BatchDecoder.parseTimestamp(receipt.importedAt) != nil,
            receipt.records
                == batch.records.map({ ReceivedRecord(id: $0.id, revision: $0.revision) })
        else {
            throw BridgeError.transport(
                "existing receipt does not match pending packet; preserve it for Mac reconciliation"
            )
        }
        var summary = ImportSummary()
        summary.alreadyPresent = batch.records.count
        return ImportResult(
            receipt: EncodableReceipt(
                batchID: batch.batchID, payloadSHA256: batch.payloadSHA256,
                importedAt: receipt.importedAt,
                records: batch.records.map { ReceiptRecord(id: $0.id, revision: $0.revision) }),
            summary: summary)
    }

    private func writeReceipt(_ data: Data, directory: Int32, pending: Data, batch: BridgeBatch)
        throws
    {
        if let existing = try Self.readRegular(directory, name: "receipt.json") {
            _ = try Self.existingResult(existing, batch: batch)
            return
        }
        let temporary = ".receipt-\(UUID().uuidString.lowercased()).tmp"
        let descriptor = openat(
            directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw Self.failure("create receipt temporary file") }
        defer {
            close(descriptor)
            unlinkat(directory, temporary, 0)
        }
        try data.withUnsafeBytes { bytes in
            var written = 0
            while written < bytes.count {
                let count = Darwin.write(
                    descriptor, bytes.baseAddress!.advanced(by: written), bytes.count - written)
                if count < 0 {
                    if errno == EINTR { continue }
                    throw Self.failure("write receipt")
                }
                guard count > 0 else {
                    throw BridgeError.transport("receipt write made no progress")
                }
                written += count
            }
        }
        guard fsync(descriptor) == 0 else { throw Self.failure("flush receipt") }
        guard try Self.readRegular(directory, name: "pending.json") == pending else {
            throw BridgeError.transport("pending packet changed before receipt publication")
        }
        // Exclusive rename publishes the fully flushed file only if receipt.json is absent.
        // It cannot overwrite another device's outstanding receipt or follow a symlink.
        if renameatx_np(directory, temporary, directory, "receipt.json", UInt32(RENAME_EXCL)) < 0 {
            if errno == EEXIST, let existing = try Self.readRegular(directory, name: "receipt.json")
            {
                _ = try Self.existingResult(existing, batch: batch)
                return
            }
            throw Self.failure("publish receipt")
        }
    }

    private static func failure(_ operation: String) -> NSError {
        NSError(
            domain: NSPOSIXErrorDomain, code: Int(errno),
            userInfo: [NSLocalizedDescriptionKey: operation])
    }
}
