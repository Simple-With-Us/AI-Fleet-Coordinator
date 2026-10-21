import Foundation

/// The pinned contract for the `fleet-config status --json` envelope.
///
/// The envelope version is an exact integer, never a range:  a breaking change raises
/// it deliberately, and this app refuses an envelope it does not know instead of
/// guessing at the fields it cannot read.  That refusal is what turns a schema change
/// into "unknown" rather than a wrong green.
enum FleetConfigContract {
    static let supportedSchemaVersion = 1
    static let expectedTool = "fleet-config"
    static let expectedCommand = "status"
}

/// One state from the fixed six-state vocabulary the engine uses.
///
/// A state string the app does not know is never guessed:  it becomes `unknown`,
/// because an unreadable state is not a healthy one.
enum CheckState: String, CaseIterable {
    case passed = "PASS"
    case drift = "DRIFT"
    case missing = "MISSING"
    case unproven = "UNPROVEN"
    case unsupported = "UNSUPPORTED"
    case unknown = "UNKNOWN"

    init(envelopeValue: String) {
        self = CheckState(rawValue: envelopeValue) ?? .unknown
    }
}

/// One config kind:  a column of the grid.
enum ConfigKind: String, CaseIterable {
    case instructions
    case hooks
    case mcp
    case skills
    case subagents
    case permissions

    /// The column order the grid uses, from the AFC-389 acceptance criteria.
    static let columnOrder: [ConfigKind] = [
        .instructions, .hooks, .mcp, .skills, .subagents, .permissions,
    ]

    var title: String {
        switch self {
        case .instructions: return "Instructions"
        case .hooks: return "Hooks"
        case .mcp: return "MCP"
        case .skills: return "Skills"
        case .subagents: return "Sub-Agents"
        case .permissions: return "Permissions"
        }
    }
}

/// A value from the envelope's open `expected` and `observed` objects.
///
/// The engine adds a field per check type, so the app reads them as JSON rather than as
/// a fixed Swift model.  The envelope never carries a secret value (the design document,
/// section 8), so nothing here needs to be redacted.
enum JSONValue: Decodable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            self = .null
        }
    }

    /// The value as the panel shows it.
    var displayText: String {
        switch self {
        case .string(let text):
            return text
        case .number(let number):
            if number == number.rounded(), abs(number) < 1e15 {
                return String(Int(number))
            }
            return String(number)
        case .bool(let flag):
            return flag ? "true" : "false"
        case .array(let items):
            return items.map { $0.displayText }.joined(separator: ", ")
        case .object(let fields):
            return fields.keys.sorted()
                .map { "\($0): \(fields[$0]?.displayText ?? "")" }
                .joined(separator: ", ")
        case .null:
            return "—"
        }
    }

    /// The value as a list of strings, when it is a list of strings.
    ///
    /// The engine's `names` field carries the hook and MCP server names a rule
    /// registered, and the app reads them as names and never as anything else.
    var stringArray: [String]? {
        guard case .array(let items) = self else { return nil }
        var names: [String] = []
        for item in items {
            guard case .string(let text) = item else { return nil }
            names.append(text)
        }
        return names
    }
}

/// One row of the envelope's flat `results` list.
///
/// The envelope carries one list and no grouped copy, so the grid groups these rows by
/// seat and kind itself (the design document, decision D3).
struct CheckResult: Decodable, Identifiable, Equatable {
    let ruleId: String
    let seat: String
    let platform: String
    let kind: String
    let state: String
    let reason: String
    let reasonCode: String
    let expected: [String: JSONValue]
    let observed: [String: JSONValue]
    let proofLevelRequired: String?
    let proofLevelAchieved: String?
    let collector: String
    let checkType: String
    let checkedAt: String
    let expiresAt: String?
    let ttlSeconds: Int
    let stale: Bool
    let failing: Bool

    var id: String { ruleId }

    var checkState: CheckState { CheckState(envelopeValue: state) }

    var configKind: ConfigKind? { ConfigKind(rawValue: kind) }

    /// The proof this rule required, and the one it reached.
    var proofText: String {
        let required = proofLevelRequired ?? "L0"
        guard let achieved = proofLevelAchieved, !achieved.isEmpty else {
            return "\(required), not proved"
        }
        return "\(required), reached \(achieved)"
    }
}

/// The summary the envelope carries:  counts per state, the verdict and the failing count.
struct EnvelopeSummary: Decodable, Equatable {
    let total: Int
    let byState: [String: Int]
    let overallVerdict: String
    let failing: Int
}

/// The pinned `fleet-config status --json` envelope.
struct StatusEnvelope: Decodable, Equatable {
    let schemaVersion: Int
    let tool: String
    let command: String
    let generatedAt: String
    let summary: EnvelopeSummary
    let results: [CheckResult]
}

/// Decodes the envelope, and refuses one this app cannot read.
enum EnvelopeDecoder {
    /// Why an envelope was refused.
    enum Failure: Error, LocalizedError {
        case unreadable(String)
        case schemaVersion(Int)
        case wrongTool(String)
        case wrongCommand(String)

        var errorDescription: String? {
            switch self {
            case .unreadable(let detail):
                return "the engine did not return a readable envelope:  \(detail)"
            case .schemaVersion(let found):
                return "the engine returned envelope version \(found);  this app reads version \(FleetConfigContract.supportedSchemaVersion)"
            case .wrongTool(let tool):
                return "the engine returned the tool \"\(tool)\";  this app reads \"\(FleetConfigContract.expectedTool)\""
            case .wrongCommand(let command):
                return "the engine returned the command \"\(command)\";  this app reads \"\(FleetConfigContract.expectedCommand)\""
            }
        }
    }

    /// The envelope, when it is one this app reads.
    ///
    /// The version gate is the point of this function:  an envelope from a newer engine
    /// is refused whole, so the grid shows unknown rather than a partly-read report
    /// whose missing fields would look like a pass.
    static func decode(_ data: Data) throws -> StatusEnvelope {
        let envelope: StatusEnvelope
        do {
            envelope = try JSONDecoder().decode(StatusEnvelope.self, from: data)
        } catch {
            throw Failure.unreadable(error.localizedDescription)
        }
        guard envelope.schemaVersion == FleetConfigContract.supportedSchemaVersion else {
            throw Failure.schemaVersion(envelope.schemaVersion)
        }
        guard envelope.tool == FleetConfigContract.expectedTool else {
            throw Failure.wrongTool(envelope.tool)
        }
        guard envelope.command == FleetConfigContract.expectedCommand else {
            throw Failure.wrongCommand(envelope.command)
        }
        return envelope
    }
}

/// The engine's timestamps, which are ISO 8601 UTC with a `Z` suffix.
enum FleetTime {
    private static let plain: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    static func parse(_ text: String) -> Date? {
        plain.date(from: text) ?? fractional.date(from: text)
    }

    /// The age of an observation, as a cell shows it.
    static func ageText(since date: Date?, now: Date = Date()) -> String {
        guard let date = date else { return "—" }
        let seconds = Int(now.timeIntervalSince(date))
        if seconds < 0 { return "0s" }
        return age(seconds)
    }

    /// The age of a span of seconds, as a list shows it.
    static func age(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86400 { return "\(seconds / 3600)h" }
        return "\(seconds / 86400)d"
    }

    /// The time of day, on Jay's clock, in 12-hour form with am or pm.
    static func clock(_ date: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        let hour24 = parts.hour ?? 0
        let minute = parts.minute ?? 0
        let suffix = hour24 < 12 ? "am" : "pm"
        var hour12 = hour24 % 12
        if hour12 == 0 { hour12 = 12 }
        return String(format: "%d:%02d%@", hour12, minute, suffix)
    }
}
