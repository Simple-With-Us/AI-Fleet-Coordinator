import Foundation

// The `fleet-config status --json` envelope (schemaVersion 1), decoded tolerantly:  unknown
// fields are ignored, an unknown state decodes as `.unrecognized` and never reads as healthy.
// Names carry the FleetConfig prefix so they cannot collide with other views' own types.

enum FleetConfigState: Equatable, Hashable, Decodable {
    case pass, drift, missing, unproven, unsupported, unknown
    case unrecognized(String)

    init(rawValue: String) {
        switch rawValue {
        case "PASS": self = .pass
        case "DRIFT": self = .drift
        case "MISSING": self = .missing
        case "UNPROVEN": self = .unproven
        case "UNSUPPORTED": self = .unsupported
        case "UNKNOWN": self = .unknown
        default: self = .unrecognized(rawValue)
        }
    }

    init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }

    var label: String {
        switch self {
        case .pass: return "Passing"
        case .drift: return "Drifted"
        case .missing: return "Missing"
        case .unproven: return "Unproven"
        case .unsupported: return "Unsupported"
        case .unknown: return "Unknown"
        case .unrecognized(let raw): return raw.capitalized
        }
    }
}

struct FleetConfigResult: Equatable, Decodable {
    var ruleId: String
    var seat: String
    var platform: String
    var kind: String
    var state: FleetConfigState
    var reason: String
    var reasonCode: String?
    var failing: Bool?
    var stale: Bool?
    var checkedAt: String?

    var isProven: Bool { state == .pass && stale != true && failing != true }
}

struct FleetConfigSummary: Equatable, Decodable {
    var total: Int
    var overallVerdict: String
    var failing: Int?
}

struct FleetConfigEnvelope: Equatable, Decodable {
    static let supportedSchemaVersion = 1

    var schemaVersion: Int
    var generatedAt: String?
    var summary: FleetConfigSummary
    var results: [FleetConfigResult]

    static func decode(_ text: String) throws -> FleetConfigEnvelope {
        guard let data = text.data(using: .utf8), !data.isEmpty else {
            throw FleetConfigCLIError.undecodable("the output was empty")
        }
        let envelope: FleetConfigEnvelope
        do {
            envelope = try JSONDecoder().decode(FleetConfigEnvelope.self, from: data)
        } catch {
            throw FleetConfigCLIError.undecodable(String(describing: error).prefix(300).description)
        }
        guard envelope.schemaVersion == supportedSchemaVersion else {
            throw FleetConfigCLIError.unsupportedSchema(envelope.schemaVersion)
        }
        return envelope
    }

    func result(seat: String, ruleID: String) -> FleetConfigResult? {
        results.first { $0.seat == seat && $0.ruleId == ruleID }
    }
}

extension FleetConfigRunning {
    /// Runs `status --json` and decodes it.  Exit 0, 1 and 3 are the PASS, FAIL and UNKNOWN
    /// verdicts and all carry a full envelope.  Any other exit is a refusal or a usage error.
    func status(
        seat: String? = nil,
        kind: String? = nil,
        platform: String? = nil,
        includeDoctors: Bool = true
    ) async throws -> FleetConfigEnvelope {
        let invocation = try FleetConfigInvocation.status(
            seat: seat, kind: kind, platform: platform, includeDoctors: includeDoctors
        )
        let result = try await run(invocation)
        if result.timedOut { throw FleetConfigCLIError.timedOut(Int(invocation.timeout)) }
        guard [0, 1, 3].contains(result.exitCode) else {
            throw FleetConfigCLIError.engineRefused(
                exitCode: result.exitCode, message: result.stderrTail
            )
        }
        return try FleetConfigEnvelope.decode(result.stdout)
    }
}
