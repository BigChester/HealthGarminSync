import Foundation
import HealthBridgeCore
import Testing

final class BatchDecoderTests {
    @Test func testPacketGeneratedByPythonOutbox() throws {
        // Generated offline through export_blood_pressure(FakeAPI), not handwritten Swift JSON.
        let url = try #require(
            Bundle.module.url(
                forResource: "python-pending", withExtension: "json", subdirectory: "Resources"))
        let data = try Data(contentsOf: url)
        let batch = try decodeBatch(data)
        #expect(batch.createdAt.hasSuffix("Z"))
        #expect(batch.records[0].sourceID == "1001")
        #expect(batch.records[0].at == "2026-10-14T16:30:00.123456Z")
        #expect(batch.records[0].localTimestamp == "2026-10-15T00:30:00.123456")
        #expect(batch.records[0].systolic == 120)
        #expect(batch.records[0].pulse == 65)
        #expect(batch.payloadSHA256 == BatchDecoder.sha256(data))
    }
    @Test func testRealWireShapeAndCrossDayMicroseconds() throws {
        let data = try fixture()
        let batch = try decodeBatch(data)
        #expect(batch.schemaVersion == 1)
        #expect(batch.records[0].localTimestamp.prefix(10) == "2026-10-15")
        #expect(batch.records[0].at.prefix(10) == "2026-10-14")
        let milliseconds = try decodeBatch(
            fixture(at: "2026-10-14T16:15:30.123Z", local: "2026-10-15T00:15:30.123+08:00"))
        #expect(
            abs(
                batch.records[0].instant.timeIntervalSince(milliseconds.records[0].instant)
                    - 0.000456) <= 0.000001)
        #expect(batch.payloadSHA256 == BatchDecoder.sha256(data))
        #expect(batch.payloadSHA256 != BatchDecoder.sha256(data + Data("\n".utf8)))
    }
    @Test func testRejectsEnvelopeErrors() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["schema_version"] = 2 }, { $0["schema_version"] = true },
            { $0["kind"] = "weight" },
            { $0["units"] = ["blood_pressure": "kPa", "pulse": "count/min"] },
            { $0["batch_id"] = "ABC" }, { $0["created_at"] = "2026-10-15T01:00:00" },
            { $0["records"] = [] },
            {
                var rows = $0["records"] as! [[String: Any]]
                rows.append(rows[0])
                $0["records"] = rows
            },
        ]
        for mutation in mutations {
            #expect(throws: (any Error).self) { try decodeBatch(fixture(mutate: mutation)) }
        }
        #expect(throws: (any Error).self) {
            try decodeBatch(Data(repeating: 32, count: BatchDecoder.maximumBytes + 1))
        }
        #expect(throws: (any Error).self) { try decodeBatch(fixture(count: 101)) }
    }
    @Test func testPythonNaiveLocalTimestampAndMicrosecondCreatedAt() throws {
        let batch = try decodeBatch(
            fixture(
                local: "2026-10-15T00:15:30.123456",
                mutate: {
                    $0["created_at"] = "2026-10-15T01:00:00.123456Z"
                }))
        #expect(batch.createdAt == "2026-10-15T01:00:00.123456Z")
        #expect(batch.records[0].localTimestamp == "2026-10-15T00:15:30.123456")
        #expect(batch.records[0].at == "2026-10-14T16:15:30.123456Z")
    }
    @Test func testRejectsRecordErrors() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["revision"] = true }, { $0["revision"] = 1.5 }, { $0["revision"] = 0 },
            { $0["systolic"] = true }, { $0["diastolic"] = -1 }, { $0["pulse"] = false },
            { $0["multi_measurement"] = 1 }, { $0["source_id"] = "01" }, { $0["source_type"] = "" },
            { $0["at"] = "2026-10-14T16:15:30.123456" },
            { $0["at"] = "2026-02-30T16:15:30.123Z" },
            { $0["local_timestamp"] = "2026-10-15T24:15:31.123456+08:00" },
            { $0["id"] = String(repeating: "f", count: 64) }, { $0.removeValue(forKey: "pulse") },
        ]
        for mutation in mutations {
            #expect(throws: (any Error).self) {
                try decodeBatch(fixture(mutate: { mutateRecord(&$0, mutation) }))
            }
        }
        let valid = String(data: try fixture(), encoding: .utf8)!
        #expect(throws: (any Error).self) {
            try decodeBatch(
                Data(
                    valid.replacingOccurrences(of: "\"systolic\":121", with: "\"systolic\":NaN")
                        .utf8))
        }
    }
}
