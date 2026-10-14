import CoreFoundation
import Darwin
import Foundation

@MainActor public protocol RevisionLedger {
    /// Fail closed for every record below its highest attempted revision.
    func validate(_ records: [BridgeRecord]) throws
    /// Atomically raise the entire packet's revision floors before any Health modification.
    /// A successful return must mean these floors are durable for a persistent ledger.
    func claim(_ records: [BridgeRecord]) throws
}

/// For offline mocks only. The app must inject a persistent FileRevisionLedger.
@MainActor public final class MemoryRevisionLedger: RevisionLedger {
    private var revisions: [String: Int] = [:]
    public init() {}
    public func validate(_ records: [BridgeRecord]) throws {
        try RevisionFloors.validate(records, against: revisions)
    }
    public func claim(_ records: [BridgeRecord]) throws {
        try validate(records)
        for record in records { revisions[record.id] = record.revision }
    }
}

/// Stores only opaque record IDs and revision integers in the caller's local app directory.
/// Initialization performs no I/O; corrupt or inaccessible state fails the first import.
@MainActor public final class FileRevisionLedger: RevisionLedger {
    private let directory: URL
    private static let maximumBytes = 64 * 1_048_576
    public init(directory: URL) { self.directory = directory }

    public func validate(_ records: [BridgeRecord]) throws {
        try withLockedDirectory { directory in
            try RevisionFloors.validate(records, against: Self.read(directory))
        }
    }
    public func claim(_ records: [BridgeRecord]) throws {
        try withLockedDirectory { directory in
            let previous = try Self.read(directory)
            try RevisionFloors.validate(records, against: previous)
            var updated = previous
            for record in records { updated[record.id] = record.revision }
            if updated != previous { try Self.write(updated, directory: directory) }
        }
    }

    private func withLockedDirectory<T>(_ operation: (Int32) throws -> T) throws -> T {
        guard directory.isFileURL else { throw BridgeError.ledger("local file URL required") }
        // Parents are the app's Application Support location; only the dedicated final
        // directory is used for ledger state. Opening it refuses a final symlink.
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        let descriptor = open(
            directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        guard descriptor >= 0 else { throw Self.failure("open revision directory") }
        defer { close(descriptor) }
        var attributes = stat()
        guard fstat(descriptor, &attributes) == 0, attributes.st_uid == getuid(),
            fchmod(descriptor, 0o700) == 0
        else {
            throw BridgeError.ledger("revision directory must be privately owned")
        }
        let lock = openat(
            descriptor, "revisions.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC,
            0o600)
        guard lock >= 0 else { throw Self.failure("open revision lock") }
        defer { close(lock) }
        guard fstat(lock, &attributes) == 0, attributes.st_mode & S_IFMT == S_IFREG,
            attributes.st_uid == getuid(), attributes.st_nlink == 1,
            fchmod(lock, 0o600) == 0
        else { throw BridgeError.ledger("revision lock must be a private regular file") }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else {
            throw Self.failure("lock revision ledger")
        }
        defer { flock(lock, LOCK_UN) }
        return try operation(descriptor)
    }

    private static func read(_ directory: Int32) throws -> [String: Int] {
        let descriptor = openat(
            directory, "revisions.json", O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if descriptor < 0 {
            if errno == ENOENT { return [:] }
            throw failure("open revision ledger")
        }
        defer { close(descriptor) }
        var attributes = stat()
        guard fstat(descriptor, &attributes) == 0, attributes.st_mode & S_IFMT == S_IFREG,
            attributes.st_uid == getuid(), attributes.st_nlink == 1,
            attributes.st_size > 0, attributes.st_size <= maximumBytes
        else {
            throw BridgeError.ledger("revision ledger must be a bounded private regular file")
        }
        guard fchmod(descriptor, 0o600) == 0 else { throw failure("protect revision ledger") }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count < 0 {
                if errno == EINTR { continue }
                throw failure("read revision ledger")
            }
            if count == 0 { break }
            guard data.count + count <= maximumBytes else {
                throw BridgeError.ledger("revision ledger exceeds size limit")
            }
            data.append(contentsOf: buffer.prefix(count))
        }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw BridgeError.ledger("corrupt revision ledger")
        }
        var revisions: [String: Int] = [:]
        for (id, value) in object {
            guard id.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                !["f", "d"].contains(String(cString: number.objCType)),
                let revision = Int(number.stringValue), revision > 0
            else { throw BridgeError.ledger("corrupt revision ledger") }
            revisions[id] = revision
        }
        // This private format is written canonically by this app. Reject noncanonical
        // bytes as corruption, including duplicate JSON keys and floating integer tokens.
        let canonical = try JSONSerialization.data(
            withJSONObject: revisions, options: [.sortedKeys])
        guard data == canonical else { throw BridgeError.ledger("corrupt revision ledger") }
        return revisions
    }

    private static func write(_ revisions: [String: Int], directory: Int32) throws {
        let data = try JSONSerialization.data(withJSONObject: revisions, options: [.sortedKeys])
        guard data.count <= maximumBytes else {
            throw BridgeError.ledger("revision ledger exceeds size limit")
        }
        let temporary = ".revisions-\(UUID().uuidString.lowercased()).tmp"
        let descriptor = openat(
            directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { throw failure("create revision ledger temporary file") }
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
                    throw failure("write revision ledger")
                }
                guard count > 0 else { throw BridgeError.ledger("revision write made no progress") }
                written += count
            }
        }
        guard fsync(descriptor) == 0 else { throw failure("flush revision ledger") }
        // Reject a symlink/nonregular replacement that appeared after the initial read.
        var attributes = stat()
        if fstatat(directory, "revisions.json", &attributes, AT_SYMLINK_NOFOLLOW) == 0 {
            guard attributes.st_mode & S_IFMT == S_IFREG, attributes.st_uid == getuid(),
                attributes.st_nlink == 1
            else {
                throw BridgeError.ledger("revision destination is not a private regular file")
            }
        } else if errno != ENOENT {
            throw failure("stat revision destination")
        }
        guard renameat(directory, temporary, directory, "revisions.json") == 0 else {
            throw failure("publish revision ledger")
        }
        guard fsync(directory) == 0 else { throw failure("flush revision directory") }
    }
    private static func failure(_ operation: String) -> NSError {
        NSError(
            domain: NSPOSIXErrorDomain, code: Int(errno),
            userInfo: [NSLocalizedDescriptionKey: operation])
    }
}

private enum RevisionFloors {
    static func validate(_ records: [BridgeRecord], against revisions: [String: Int]) throws {
        for record in records where (revisions[record.id] ?? 0) > record.revision {
            throw BridgeError.superseded(record.id)
        }
    }
}
