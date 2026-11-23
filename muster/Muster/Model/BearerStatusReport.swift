import Foundation

/// The bearer status the Mac's poll writes, and what Muster can say about it.
///
/// `~/.local/state/fleet-tools/bearer-status.json` is written every five minutes by
/// `com.jay.fleet-bearer-status` (`scripts/fleet-bearer-status.py`), which reads the Worker's
/// owner status API with the owner's own Cloudflare Access login.  It holds names, kinds,
/// times and counts, never a bearer.  A file that is missing, unreadable, of another version
/// or older than fifteen minutes reads as not known, never as a pass.
struct BearerStatusRow: Identifiable, Equatable {
    var id: String { identity }
    let identity: String
    let kind: String?
    let problem: String?
    let active: Bool
    let expired: Bool
    let expiresAt: String?
    let mcpAccept: Bool?
    let paused: Bool?
    let missingRecords: [String]
    let mismatchCount: Int?

    /// The row's word:  a bearer in good standing says so, anything else says what the Worker said.
    var verdict: String {
        if let problem, problem != "ok" { return problem.replacingOccurrences(of: "_", with: " ") }
        if expired { return "expired" }
        if !active { return "no live bearer" }
        return "live"
    }

    var isHealthy: Bool { (problem == nil || problem == "ok") && active && !expired }
}

struct BearerStatusAlarm: Identifiable, Equatable {
    var id: String { key }
    let key: String
    let summary: String
    let since: String?
}

struct BearerStatusReading: Equatable {
    var rows: [BearerStatusRow] = []
    var alarms: [BearerStatusAlarm] = []
    var generatedAt: Date?
    /// Whether the poll reached the Worker on its last run.
    var pollOk = false
    /// What the poll said went wrong, or why this file could not be read.
    var problem: String?
    var isCurrent = false
}

enum BearerStatusContract {
    static let schemaVersion = 1
    static let maxAgeSeconds = 15 * 60
    static let fileName = "bearer-status.json"

    static func fileURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home.appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent(fileName)
    }
}

enum BearerStatusReader {
    static func read(at url: URL = BearerStatusContract.fileURL(), now: Date = Date()) -> BearerStatusReading {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return BearerStatusReading(problem: "No bearer status file at \(url.path).  The poll com.jay.fleet-bearer-status has not written one.")
        }
        guard let data = try? Data(contentsOf: url) else {
            return BearerStatusReading(problem: "The bearer status file could not be read.")
        }
        return parse(data, now: now)
    }

    static func parse(_ data: Data, now: Date) -> BearerStatusReading {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return BearerStatusReading(problem: "The bearer status file is not valid JSON.")
        }
        guard (object["schemaVersion"] as? Int) == BearerStatusContract.schemaVersion else {
            return BearerStatusReading(problem: "The bearer status file is of another version, so it is not read.")
        }
        var reading = BearerStatusReading()
        reading.pollOk = (object["ok"] as? Bool) ?? false
        if let text = object["generatedAt"] as? String { reading.generatedAt = FleetTime.parse(text) }
        if let error = object["error"] as? String { reading.problem = error.replacingOccurrences(of: "_", with: " ") }
        reading.rows = ((object["rows"] as? [[String: Any]]) ?? []).compactMap { raw in
            guard let identity = raw["identity"] as? String else { return nil }
            return BearerStatusRow(
                identity: identity,
                kind: raw["kind"] as? String,
                problem: raw["problem"] as? String,
                active: (raw["active"] as? Bool) ?? false,
                expired: (raw["expired"] as? Bool) ?? false,
                expiresAt: raw["expiresAt"] as? String,
                mcpAccept: raw["mcpAccept"] as? Bool,
                paused: raw["paused"] as? Bool,
                missingRecords: (raw["missingRecords"] as? [String]) ?? [],
                mismatchCount: raw["mismatchCount"] as? Int)
        }
        reading.alarms = ((object["alarms"] as? [[String: Any]]) ?? []).compactMap { raw in
            guard let key = raw["key"] as? String else { return nil }
            return BearerStatusAlarm(key: key, summary: (raw["summary"] as? String) ?? key, since: raw["since"] as? String)
        }
        if let generatedAt = reading.generatedAt {
            let age = now.timeIntervalSince(generatedAt)
            reading.isCurrent = age >= -60 && age <= TimeInterval(BearerStatusContract.maxAgeSeconds)
        } else {
            reading.problem = reading.problem ?? "The bearer status file has no time, so it is not current."
        }
        return reading
    }
}
