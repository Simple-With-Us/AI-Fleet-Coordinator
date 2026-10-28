import Foundation

// The operations audit log,  as Muster reads it and adds to it.
//
// The file is `~/.local/state/fleet-tools/ops-audit.jsonl`,  mode 0600,  one JSON object a
// line:  `fleet-ops-control.py` appends one row for every `--yes` run on the host that runs
// it,  with the time,  the host,  the verb,  the target and the result,  and nothing the
// commands themselves printed.
//
// A server verb runs on the server,  so its own row lands in the server's log.  Muster adds
// one row of its own to the Mac's log for each server action it starts,  marked
// `"via": "muster"`,  so the Operations tab can show what the owner did from this Mac.

/// One row of the log.
struct OpsAuditEntry: Decodable, Identifiable, Equatable {
    let time: String?
    let host: String?
    let verb: String?
    let target: String?
    let result: String?
    let via: String?
    /// The row's place in the file,  for a stable identity.
    var line: Int = 0

    enum CodingKeys: String, CodingKey {
        case time, host, verb, target, result, via
    }

    init(
        time: String?, host: String?, verb: String?, target: String?, result: String?,
        via: String? = nil, line: Int = 0
    ) {
        self.time = time
        self.host = host
        self.verb = verb
        self.target = target
        self.result = result
        self.via = via
        self.line = line
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        time = try? container.decodeIfPresent(String.self, forKey: .time)
        host = try? container.decodeIfPresent(String.self, forKey: .host)
        verb = try? container.decodeIfPresent(String.self, forKey: .verb)
        target = try? container.decodeIfPresent(String.self, forKey: .target)
        result = try? container.decodeIfPresent(String.self, forKey: .result)
        via = try? container.decodeIfPresent(String.self, forKey: .via)
    }

    var id: Int { line }

    /// When the row was written,  when it carries a time this app can read.
    var date: Date? { time.flatMap(FleetTime.parse) }

    /// The time on the owner's clock,  in 12-hour form.
    var clockText: String {
        guard let date = date else { return "—" }
        return FleetTime.clock(date)
    }

    /// The state the row reads as:  done is a pass,  refused is drift,  failed is a failure.
    var state: CellState {
        switch result ?? "" {
        case "done": return .passed
        case "planned": return .passed
        case "refused": return .drift
        case "failed": return .missing
        default: return .unproven
        }
    }
}

/// The last rows of the log,  and what could not be read.
struct OpsAuditReading: Equatable {
    var path: String = ""
    /// The rows,  newest first.
    var entries: [OpsAuditEntry] = []
    /// Lines that were there and were not a row this app can read.
    var unreadable: Int = 0
    /// Why the log could not be read at all,  when it could not.
    var problem: String?
}

enum OpsAuditLog {
    /// How many rows the Operations tab shows.
    static let defaultLimit = 50
    /// The most of the file's tail this app reads,  in bytes.  Fifty rows are a few
    /// kilobytes,  so a log that has grown large costs only its tail.
    static let maximumTailBytes = 256 * 1024

    /// Where the log is.
    static func logURL(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("state", isDirectory: true)
            .appendingPathComponent("fleet-tools", isDirectory: true)
            .appendingPathComponent("ops-audit.jsonl", isDirectory: false)
    }

    /// Read the last rows of the log,  newest first.
    ///
    /// A log that is not there yet is an empty log and not a problem:  nothing has been
    /// run.  A line that is not a JSON object is counted and skipped.
    ///
    /// When the log is larger than `maximumTailBytes`,  the tail almost always starts in the
    /// middle of a row.  That cut-off row is dropped without being counted:  it is the
    /// read's own edge,  not a line of the log that could not be read.
    static func read(at url: URL, limit: Int = OpsAuditLog.defaultLimit) -> OpsAuditReading {
        var reading = OpsAuditReading()
        reading.path = url.path
        guard FileManager.default.fileExists(atPath: url.path) else { return reading }
        var data: Data
        do {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            let size = try handle.seekToEnd()
            let tail = UInt64(maximumTailBytes)
            if size > tail {
                // Start one byte early,  so a tail that begins exactly at a row keeps that
                // row:  the byte before it is the newline that ends the row before.
                let start = size - tail - 1
                try handle.seek(toOffset: start)
                data = try handle.readToEnd() ?? Data()
                if start == 0 {
                    // The read began at the start of the file,  so its first row is whole.
                } else if let newline = data.firstIndex(of: 0x0A) {
                    data = Data(data[data.index(after: newline)...])
                } else {
                    // One row longer than the whole tail:  there is no whole row to show.
                    data = Data()
                }
            } else {
                try handle.seek(toOffset: 0)
                data = try handle.readToEnd() ?? Data()
            }
        } catch {
            reading.problem = "The audit log could not be read:\(ServerStatusCopy.sentenceGap)\(error.localizedDescription)"
            return reading
        }
        return parse(String(decoding: data, as: UTF8.self), path: url.path, limit: limit)
    }

    /// Read the log this user's tools write.
    static func readDefault(limit: Int = OpsAuditLog.defaultLimit) -> OpsAuditReading {
        read(at: logURL(), limit: limit)
    }

    /// Parse the text of the log,  newest first.
    static func parse(_ text: String, path: String = "", limit: Int = OpsAuditLog.defaultLimit) -> OpsAuditReading {
        var reading = OpsAuditReading()
        reading.path = path
        let lines = text.split(whereSeparator: \.isNewline)
        let decoder = JSONDecoder()
        var entries: [OpsAuditEntry] = []
        for (index, raw) in lines.enumerated() {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { continue }
            guard line.hasPrefix("{"),
                  var entry = try? decoder.decode(OpsAuditEntry.self, from: Data(line.utf8))
            else {
                reading.unreadable += 1
                continue
            }
            entry.line = index
            entries.append(entry)
        }
        reading.entries = Array(entries.suffix(max(limit, 0)).reversed())
        return reading
    }

    /// The row Muster writes for a server action it started.
    static func entry(
        for action: OpsAction, result: String, now: Date = Date()
    ) -> [String: String] {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(identifier: "UTC")
        return [
            "time": formatter.string(from: now),
            "host": action.runsOnServer ? "server" : "mac",
            "verb": action.auditVerb,
            "target": action.auditTarget,
            "result": result,
            "via": "muster",
        ]
    }

    enum AppendError: Error, Equatable {
        case unsafePath(String)
        case couldNotWrite(String)
    }

    /// Append one row,  creating the log at mode 0600 when it is not there.
    ///
    /// The log is never followed through a symlink,  and its directory is created private.
    static func append(_ row: [String: String], to url: URL) throws {
        let fileManager = FileManager.default
        let directory = url.deletingLastPathComponent()
        if let values = try? directory.resourceValues(forKeys: [.isSymbolicLinkKey]),
           values.isSymbolicLink == true {
            throw AppendError.unsafePath(directory.path)
        }
        if !fileManager.fileExists(atPath: directory.path) {
            do {
                try fileManager.createDirectory(
                    at: directory, withIntermediateDirectories: true,
                    attributes: [.posixPermissions: 0o700]
                )
            } catch {
                throw AppendError.couldNotWrite(directory.path)
            }
        }
        guard let body = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) else {
            throw AppendError.couldNotWrite(url.path)
        }
        var line = body
        line.append(0x0A)
        let descriptor = open(url.path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else { throw AppendError.couldNotWrite(url.path) }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer { try? handle.close() }
        do {
            try handle.write(contentsOf: line)
        } catch {
            throw AppendError.couldNotWrite(url.path)
        }
    }
}
