import CoreFoundation
import CryptoKit
import Foundation

public func decodeBatch(_ data: Data) throws -> BridgeBatch { try BatchDecoder.decode(data) }

public enum BatchDecoder {
    public static let maximumBytes = 1_048_576

    public static func decode(_ data: Data) throws -> BridgeBatch {
        guard !data.isEmpty, data.count <= maximumBytes else { throw invalid("packet size") }
        let json: Any
        do { json = try JSONSerialization.jsonObject(with: data) } catch { throw invalid("JSON") }
        guard let object = json as? [String: Any] else { throw invalid("object required") }
        guard try integer(object, "schema_version") == 1 else { throw invalid("schema_version") }
        guard try string(object, "kind") == "garmin_blood_pressure" else { throw invalid("kind") }
        let batchID = try string(object, "batch_id")
        let accountID = try string(object, "account_id")
        guard matches(batchID, "^[0-9a-f]{32}$"), matches(accountID, "^[0-9a-f]{64}$") else {
            throw invalid("batch/account identifier")
        }
        let created = try string(object, "created_at")
        guard
            matches(
                created,
                "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.([0-9]{3}|[0-9]{6})Z$"),
            parseTimestamp(created) != nil
        else { throw invalid("created_at") }
        guard let units = object["units"] as? [String: Any],
            units["blood_pressure"] as? String == "mmHg",
            units["pulse"] as? String == "count/min", units.count == 2
        else { throw invalid("units") }
        guard let rows = object["records"] as? [[String: Any]], (1...100).contains(rows.count)
        else {
            throw invalid("records count")
        }
        var seen = Set<String>()
        let records = try rows.map { row -> BridgeRecord in
            let id = try string(row, "id")
            guard matches(id, "^[0-9a-f]{64}$"), seen.insert(id).inserted else {
                throw invalid("duplicate/invalid record id")
            }
            let revision = try integer(row, "revision")
            guard revision > 0 else { throw invalid("revision") }
            let sourceID = try string(row, "source_id")
            guard matches(sourceID, "^[1-9][0-9]*$") else { throw invalid("source_id") }
            let at = try string(row, "at")
            guard
                matches(
                    at,
                    "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.([0-9]{3}|[0-9]{6})Z$"
                ),
                let instant = parseTimestamp(at)
            else { throw invalid("at") }
            let local = try string(row, "local_timestamp")
            // Garmin's local timestamp can be naive. Validate its calendar syntax, preserve it
            // exactly for identity, and never infer a timezone from it. `at` is authoritative UTC.
            let naivePattern =
                "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]{1,6})?$"
            if matches(local, naivePattern) {
                guard parseTimestamp(local + "Z") != nil else { throw invalid("local_timestamp") }
            } else {
                guard parseTimestamp(local) != nil else { throw invalid("local_timestamp") }
            }
            guard sha256(Data("cn|\(accountID)|\(local.prefix(10))|\(sourceID)".utf8)) == id else {
                throw invalid("record identity")
            }
            let sourceType = try string(row, "source_type")
            guard !sourceType.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw invalid("source_type")
            }
            let systolic = try positiveNumber(row, "systolic")
            let diastolic = try positiveNumber(row, "diastolic")
            guard let pulseValue = row["pulse"] else { throw invalid("pulse required") }
            let pulse = pulseValue is NSNull ? nil : try positiveNumber(row, "pulse")
            guard let flag = row["multi_measurement"] as? NSNumber,
                CFGetTypeID(flag) == CFBooleanGetTypeID()
            else {
                throw invalid("multi_measurement")
            }
            return BridgeRecord(
                id: id, revision: revision, sourceID: sourceID, at: at, instant: instant,
                localTimestamp: local, sourceType: sourceType, systolic: systolic,
                diastolic: diastolic, pulse: pulse, multiMeasurement: flag.boolValue)
        }
        return BridgeBatch(
            schemaVersion: 1, kind: "garmin_blood_pressure", batchID: batchID,
            accountID: accountID, createdAt: created, records: records, payloadSHA256: sha256(data))
    }

    public static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private static func invalid(_ field: String) -> BridgeError { .invalidPacket(field) }
    private static func matches(_ value: String, _ pattern: String) -> Bool {
        value.range(of: pattern, options: .regularExpression) != nil
    }
    private static func string(_ row: [String: Any], _ key: String) throws -> String {
        guard let value = row[key] as? String else { throw invalid(key) }
        return value
    }
    private static func integer(_ row: [String: Any], _ key: String) throws -> Int {
        guard let value = row[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
            !["f", "d"].contains(String(cString: value.objCType)),
            let integer = Int(value.stringValue)
        else { throw invalid(key) }
        return integer
    }
    private static func positiveNumber(_ row: [String: Any], _ key: String) throws -> Double {
        guard let value = row[key] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
            value.doubleValue.isFinite, value.doubleValue > 0
        else { throw invalid(key) }
        return value.doubleValue
    }

    /// Parse independently of ISO8601DateFormatter's millisecond rounding; preserve microseconds.
    static func parseTimestamp(_ value: String) -> Date? {
        let pattern =
            "^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\\.([0-9]{1,6}))?(Z|[+-][0-9]{2}:[0-9]{2})$"
        guard let regex = try? NSRegularExpression(pattern: pattern),
            let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value))
        else { return nil }
        func group(_ index: Int) -> String? {
            guard let range = Range(match.range(at: index), in: value) else { return nil }
            return String(value[range])
        }
        guard let year = Int(group(1)!), let month = Int(group(2)!), let day = Int(group(3)!),
            let hour = Int(group(4)!), let minute = Int(group(5)!), let second = Int(group(6)!),
            (1...9999).contains(year), (1...12).contains(month), (1...31).contains(day),
            hour < 24, minute < 60, second < 60, let zone = group(8)
        else { return nil }
        var offset = 0
        if zone != "Z" {
            let hours = Int(zone.dropFirst().prefix(2))!
            let minutes = Int(zone.suffix(2))!
            guard hours < 24, minutes < 60 else { return nil }
            offset = (hours * 3600 + minutes * 60) * (zone.first == "-" ? -1 : 1)
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let parts = DateComponents(
            year: year, month: month, day: day, hour: hour, minute: minute, second: second)
        guard let base = calendar.date(from: parts) else { return nil }
        let verified = calendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second], from: base)
        guard verified.year == year, verified.month == month, verified.day == day,
            verified.hour == hour, verified.minute == minute, verified.second == second
        else { return nil }
        let fraction = group(7).flatMap { Double("0." + $0) } ?? 0
        return base.addingTimeInterval(fraction - Double(offset))
    }
}
