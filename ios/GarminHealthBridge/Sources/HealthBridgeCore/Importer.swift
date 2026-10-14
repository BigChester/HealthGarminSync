import Foundation

@MainActor public final class Importer {
    private let store: any HealthStorage
    private let ledger: any RevisionLedger
    private var importing = false

    public init(store: any HealthStorage, ledger: any RevisionLedger) {
        self.store = store
        self.ledger = ledger
    }

    public func validateRevisionFloor(_ batch: BridgeBatch) throws {
        try ledger.validate(batch.records)
    }

    func claimConfirmedReceiptFloor(_ batch: BridgeBatch) throws {
        try ledger.claim(batch.records)
    }

    public func importBatch(_ data: Data) async throws -> ImportResult {
        let batch = try decodeBatch(data)
        guard !importing else { throw BridgeError.conflict("import already running") }
        importing = true
        defer { importing = false }
        try validateRevisionFloor(batch)
        try await store.requestAuthorization()
        guard
            QuantityKind.allCases.allSatisfy({ store.authorizationStatus(for: $0) == .authorized })
        else {
            throw BridgeError.authorizationDenied
        }
        // Durable attempt floors also protect removals that leave no owned HealthKit sample.
        // Claim the entire packet before the first save/delete, including partial-save retries.
        try ledger.claim(batch.records)
        var summary = ImportSummary()
        for record in batch.records {
            let before = try await store.snapshot(for: record)
            if newestOwnedRevision(before, record) > record.revision {
                throw BridgeError.superseded(record.id)
            }
            let hasNativeBP = try matchingExternalBloodPressure(before, record)
            let hasNativePulse = try matchingExternalPulse(before, record)
            try validateCurrentOwnedValues(before, record)
            let writeBP = !hasNativeBP && !confirmedOwnedBloodPressure(before, record)
            let writePulse =
                record.pulse != nil && !hasNativePulse && !confirmedOwnedPulse(before, record)
            let clearPulse =
                record.pulse == nil && before.quantities.contains { isOwned($0, record, .pulse) }
            // Clearing an old pulse is a write even when the incoming packet has no pulse.
            if clearPulse && store.authorizationStatus(for: .pulse) != .authorized {
                throw BridgeError.authorizationDenied
            }
            try ledger.validate([record])
            if writeBP || writePulse {
                try await store.saveMeasurement(record, bloodPressure: writeBP, pulse: writePulse)
            }
            if clearPulse { try await store.deleteOwnedPulse(for: record) }
            let after = try await store.snapshot(for: record)
            // A competing importer may have saved a newer revision during suspension.
            // Never acknowledge our write unless its requested values can be read back.
            let bpConfirmed =
                try matchingExternalBloodPressure(after, record)
                || confirmedOwnedBloodPressure(after, record)
            let pulseConfirmed: Bool
            if record.pulse != nil {
                pulseConfirmed =
                    try matchingExternalPulse(after, record) || confirmedOwnedPulse(after, record)
            } else {
                pulseConfirmed = !after.quantities.contains { isOwned($0, record, .pulse) }
            }
            guard bpConfirmed && pulseConfirmed else { throw BridgeError.readbackFailed(record.id) }
            summary.bloodPressureWritten += writeBP ? 1 : 0
            summary.pulseWritten += writePulse ? 1 : 0
            if !writeBP && !writePulse && !clearPulse { summary.alreadyPresent += 1 }
        }
        try validateRevisionFloor(batch)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return ImportResult(
            receipt: EncodableReceipt(
                batchID: batch.batchID, payloadSHA256: batch.payloadSHA256,
                importedAt: formatter.string(from: Date()),
                records: batch.records.map { ReceiptRecord(id: $0.id, revision: $0.revision) }),
            summary: summary)
    }

    private func sameInstant(_ a: Date, _ b: Date) -> Bool {
        abs(a.timeIntervalSince(b)) < 0.000002
    }
    private func equalValue(_ a: Double, _ b: Double) -> Bool { abs(a - b) <= 0.001 }
    private func isOwned(_ sample: StoredQuantity, _ record: BridgeRecord, _ kind: QuantityKind)
        -> Bool
    {
        sample.ownedByBridge && sample.kind == kind
            && sample.syncIdentifier == record.syncIdentifier(for: kind)
    }
    private func isOwned(_ sample: StoredBloodPressure, _ record: BridgeRecord) -> Bool {
        sample.ownedByBridge && sample.syncIdentifier == record.bloodPressureSyncIdentifier
    }
    private func newestOwnedRevision(_ snapshot: HealthSnapshot, _ record: BridgeRecord) -> Int {
        let quantities = snapshot.quantities.filter { isOwned($0, record, $0.kind) }.compactMap(
            \.revision)
        let correlations = snapshot.bloodPressures.filter { isOwned($0, record) }.compactMap(
            \.revision)
        return (quantities + correlations).max() ?? 0
    }
    private func matchingExternalBloodPressure(_ snapshot: HealthSnapshot, _ record: BridgeRecord)
        throws -> Bool
    {
        let samples = snapshot.quantities.filter {
            $0.kind != .pulse && sameInstant($0.instant, record.instant)
                && !isOwned($0, record, $0.kind)
        }
        let correlations = snapshot.bloodPressures.filter {
            sameInstant($0.instant, record.instant) && !isOwned($0, record)
        }
        guard !samples.isEmpty || !correlations.isEmpty else { return false }
        let sys =
            samples.filter { $0.kind == .systolic }.map(\.value) + correlations.map(\.systolic)
        let dia =
            samples.filter { $0.kind == .diastolic }.map(\.value) + correlations.map(\.diastolic)
        guard !sys.isEmpty, !dia.isEmpty, sys.allSatisfy({ equalValue($0, record.systolic) }),
            dia.allSatisfy({ equalValue($0, record.diastolic) })
        else { throw BridgeError.conflict(record.id) }
        return true
    }
    private func matchingExternalPulse(_ snapshot: HealthSnapshot, _ record: BridgeRecord) throws
        -> Bool
    {
        guard let pulse = record.pulse else { return false }
        let samples = snapshot.quantities.filter {
            $0.kind == .pulse && sameInstant($0.instant, record.instant)
                && !isOwned($0, record, .pulse)
        }
        guard !samples.isEmpty else { return false }
        guard samples.allSatisfy({ equalValue($0.value, pulse) }) else {
            throw BridgeError.conflict(record.id)
        }
        return true
    }
    private func validateCurrentOwnedValues(_ snapshot: HealthSnapshot, _ record: BridgeRecord)
        throws
    {
        for sample in snapshot.quantities
        where isOwned(sample, record, sample.kind) && sample.revision == record.revision {
            let expected: Double? =
                sample.kind == .systolic
                ? record.systolic : sample.kind == .diastolic ? record.diastolic : record.pulse
            // Null pulse is a legitimate removal, including recovery after a partial update.
            if let expected,
                !sameInstant(sample.instant, record.instant) || !equalValue(sample.value, expected)
            {
                throw BridgeError.conflict(record.id)
            }
        }
        for sample in snapshot.bloodPressures
        where isOwned(sample, record) && sample.revision == record.revision {
            guard sameInstant(sample.instant, record.instant),
                equalValue(sample.systolic, record.systolic),
                equalValue(sample.diastolic, record.diastolic)
            else { throw BridgeError.conflict(record.id) }
        }
    }
    private func confirmedOwnedBloodPressure(_ snapshot: HealthSnapshot, _ record: BridgeRecord)
        -> Bool
    {
        let correlated = snapshot.bloodPressures.contains {
            isOwned($0, record) && $0.revision == record.revision
                && sameInstant($0.instant, record.instant)
                && equalValue($0.systolic, record.systolic)
                && equalValue($0.diastolic, record.diastolic)
        }
        return correlated
            && [QuantityKind.systolic, .diastolic].allSatisfy { kind in
                snapshot.quantities.contains {
                    isOwned($0, record, kind) && $0.revision == record.revision
                        && sameInstant($0.instant, record.instant)
                        && equalValue(
                            $0.value, kind == .systolic ? record.systolic : record.diastolic)
                }
            }
    }
    private func confirmedOwnedPulse(_ snapshot: HealthSnapshot, _ record: BridgeRecord) -> Bool {
        guard let pulse = record.pulse else { return false }
        return snapshot.quantities.contains {
            isOwned($0, record, .pulse) && $0.revision == record.revision
                && sameInstant($0.instant, record.instant) && equalValue($0.value, pulse)
        }
    }
}
