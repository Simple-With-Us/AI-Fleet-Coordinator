import Foundation

/// Decodable mirror of the `fleet-view status --json` envelope (AFC-394).
///
/// The contract is `scripts/fleet_view/contract.py`:  `schemaVersion` is
/// pinned at 1, and a failed section reports `UNKNOWN`, never an empty
/// healthy list.  The app decodes this envelope and nothing else;  it never
/// parses a seat's config files itself.
struct FleetViewEnvelope: Decodable {
    let schemaVersion: Int
    let generatedAt: String
    let summary: Summary
    let requestCounts: RequestCounts
    let sections: Sections
    let notes: [String]

    struct Summary: Decodable {
        let lanes: Int
        let pullRequests: Int
        let claims: Int
        let overallVerdict: String
    }

    struct RequestCounts: Decodable {
        let github: Int
        let linear: Int
        let githubMax: Int
        let linearMax: Int
    }

    struct Sections: Decodable {
        let lanes: Section<Lane>
        let pullRequests: Section<PullRequest>
        let claims: Section<Claim>
    }

    struct Section<Item: Decodable>: Decodable {
        let state: String
        let reason: String
        let items: [Item]

        var isKnown: Bool { state == "OK" }
    }

    struct Lane: Decodable, Identifiable {
        let repo: String
        let lane: String
        let seat: String
        let branch: String
        let dirtyFiles: Int
        let ageSeconds: Int
        let state: String

        var id: String { "\(repo)/\(lane)" }
    }

    struct PullRequest: Decodable, Identifiable {
        let repo: String
        let number: Int
        let title: String
        let branch: String
        let seat: String
        let blocker: String
        let state: String

        var id: String { "\(repo)#\(number)" }
        var isBlocked: Bool { blocker != "ready" }
    }

    struct Claim: Decodable, Identifiable {
        let identifier: String
        let title: String
        let seat: String
        let ageSeconds: Int
        let expired: Bool

        var id: String { identifier }
    }
}

/// Formats seconds as a short age, and instants on Jay's clock (12-hour,
/// lowercase am or pm, no zone label).
enum FleetTime {
    static func age(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds)s" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86400 { return "\(seconds / 3600)h" }
        return "\(seconds / 86400)d"
    }

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
