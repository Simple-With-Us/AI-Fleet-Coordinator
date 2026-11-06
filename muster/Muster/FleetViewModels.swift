import Foundation

/// Decodable mirror of the `fleet-view status --json` envelope (AFC-394).
///
/// The contract is `scripts/fleet_view/contract.py`:  `schemaVersion` is
/// pinned at 1, and a failed section reports `UNKNOWN`, never an empty
/// healthy list.  The app decodes this envelope and nothing else;  it never
/// parses a seat's config files itself.
///
/// Fields are only ever added at version 1, so every field newer than the first
/// envelope is optional here:  a checkout older than this app leaves them out, and
/// each one's absence reads as "not said", never as a healthy value.  One row that
/// will not decode costs that row, not the page:  the row reads UNKNOWN, or is
/// dropped and its section stops reading as known (CONTRACT-SKEW-15).
struct FleetViewEnvelope: Decodable {
    let schemaVersion: Int
    /// The command that answered.  Older collectors do not say;  any other tool is refused.
    let tool: String?
    let command: String?
    let generatedAt: String
    /// The machine that ran the collector, which is the machine every lane is on.  An
    /// older collector does not say, and Muster only ever runs it as a child process
    /// on the Mac, so a missing value means the Mac.  A value this app does not know is
    /// nil, and the lanes then name no machine.
    let host: FleetHost?
    /// The ceiling that flagged a claim expired, in hours.  Nil from an older collector.
    let claimExpiryHours: Int?
    /// How long the run took.  Nil from an older collector or when not measured.
    let durationSeconds: Double?
    let summary: Summary
    let requestCounts: RequestCounts
    let sections: Sections
    let notes: [String]

    private enum CodingKeys: String, CodingKey {
        case schemaVersion, tool, command, generatedAt, host, claimExpiryHours, durationSeconds
        case summary, requestCounts, sections, notes
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        let tool = try? container.decodeIfPresent(String.self, forKey: .tool)
        let command = try? container.decodeIfPresent(String.self, forKey: .command)
        if let tool, tool != "fleet-view" {
            throw DecodingError.dataCorruptedError(
                forKey: .tool, in: container,
                debugDescription: "This answer came from \(tool), not from fleet-view."
            )
        }
        if let command, command != "status" {
            throw DecodingError.dataCorruptedError(
                forKey: .command, in: container,
                debugDescription: "This answer is from fleet-view \(command), not fleet-view status."
            )
        }
        self.tool = tool
        self.command = command
        generatedAt = try container.decode(String.self, forKey: .generatedAt)
        if container.contains(.host) {
            host = FleetHost(reportValue: try? container.decode(String.self, forKey: .host))
        } else {
            host = .mac
        }
        claimExpiryHours = try? container.decodeIfPresent(Int.self, forKey: .claimExpiryHours)
        let duration = try? container.decodeIfPresent(Double.self, forKey: .durationSeconds)
        durationSeconds = (duration ?? -1) >= 0 ? duration : nil
        summary = try container.decode(Summary.self, forKey: .summary)
        requestCounts = try container.decode(RequestCounts.self, forKey: .requestCounts)
        sections = try container.decode(Sections.self, forKey: .sections)
        notes = (try? container.decodeIfPresent([String].self, forKey: .notes)) ?? []
    }

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
        /// What the section names as not read:  for pull requests, the repos whose list
        /// is missing or may be cut short.  Nil from an older collector, which does not say.
        let unread: [String]?
        /// Rows that were not objects at all, so they are not on the page.
        let droppedItems: Int

        /// Known only when the collector says so and every row it sent was read.
        var isKnown: Bool { state == "OK" && droppedItems == 0 }

        private enum CodingKeys: String, CodingKey {
            case state, reason, items, unread
        }

        /// Advances past a row that is not an object, whatever it is.
        private struct Skipped: Decodable {
            init(from decoder: Decoder) throws {}
        }

        init(state: String, reason: String, items: [Item], unread: [String]? = nil, droppedItems: Int = 0) {
            self.state = state
            self.reason = reason
            self.items = items
            self.unread = unread
            self.droppedItems = droppedItems
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            state = (try? container.decode(String.self, forKey: .state)) ?? "UNKNOWN"
            reason = (try? container.decode(String.self, forKey: .reason)) ?? "The collector gave no reason."
            unread = try? container.decodeIfPresent([String].self, forKey: .unread)
            var kept: [Item] = []
            var dropped = 0
            if var list = try? container.nestedUnkeyedContainer(forKey: .items) {
                while !list.isAtEnd {
                    if let item = try? list.decode(Item.self) {
                        kept.append(item)
                    } else {
                        dropped += 1
                        // A failed decode does not move past the row, so this does.
                        if (try? list.decode(Skipped.self)) == nil { break }
                    }
                }
            } else {
                dropped = 1
            }
            items = kept
            droppedItems = dropped
        }
    }

    struct Lane: Decodable, Identifiable, Equatable {
        let repo: String
        let lane: String
        let seat: String
        let branch: String
        let dirtyFiles: Int
        /// The folder's own modified time, which says little about work.  Kept for decoding only.
        let ageSeconds: Int
        let state: String
        /// The machine the lane is on, when the row names it.
        let host: FleetHost?
        let detached: Bool
        /// The short commit HEAD is on, "" when not known.
        let head: String
        /// Seconds since the last commit, -1 when not known.
        let lastCommitSeconds: Int
        /// The lane's folder on its own machine, when the collector sent it.
        let path: String?
        let layout: String

        var id: String { "\(repo)/\(lane)" }
        var isReadable: Bool { state == "OK" }

        private enum CodingKeys: String, CodingKey {
            case repo, lane, seat, branch, dirtyFiles, ageSeconds, state
            case host, detached, head, lastCommitSeconds, path, layout
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            let repo = try? container.decode(String.self, forKey: .repo)
            let lane = try? container.decode(String.self, forKey: .lane)
            let seat = try? container.decode(String.self, forKey: .seat)
            let branch = try? container.decode(String.self, forKey: .branch)
            let dirty = try? container.decode(Int.self, forKey: .dirtyFiles)
            let age = try? container.decode(Int.self, forKey: .ageSeconds)
            let state = try? container.decode(String.self, forKey: .state)
            self.repo = repo ?? ""
            self.lane = lane ?? ""
            self.seat = seat ?? "UNKNOWN"
            self.branch = branch ?? ""
            dirtyFiles = dirty ?? -1
            ageSeconds = age ?? -1
            let texts: [String?] = [repo, lane, seat, branch]
            let whole = texts.allSatisfy { $0 != nil } && dirty != nil && age != nil
            self.state = whole ? (state ?? "UNKNOWN") : "UNKNOWN"
            let hostValue = try? container.decodeIfPresent(String.self, forKey: .host)
            host = FleetHost(reportValue: hostValue)
            detached = (try? container.decodeIfPresent(Bool.self, forKey: .detached)) ?? false
            head = (try? container.decodeIfPresent(String.self, forKey: .head)) ?? ""
            lastCommitSeconds = (try? container.decodeIfPresent(Int.self, forKey: .lastCommitSeconds)) ?? -1
            let path = try? container.decodeIfPresent(String.self, forKey: .path)
            self.path = (path ?? "").isEmpty ? nil : path
            layout = (try? container.decodeIfPresent(String.self, forKey: .layout)) ?? "seat"
        }

        /// The branch, in words:  a detached lane names its commit instead.
        var branchText: String {
            if detached || branch == "HEAD" {
                return head.isEmpty ? "Detached, commit not known" : "Detached at \(head)"
            }
            return branch.isEmpty ? "Branch not read" : branch
        }

        /// The time since the last commit, which is what says whether a lane is being worked.
        var activityText: String {
            lastCommitSeconds < 0 ? "Last commit not known" : "Last commit \(FleetTime.age(lastCommitSeconds)) ago"
        }

        /// Uncommitted changes, in words.
        var changesText: String {
            if dirtyFiles < 0 { return "Changes not known" }
            if dirtyFiles == 0 { return "No uncommitted changes" }
            return FleetWords.count(dirtyFiles, "uncommitted change")
        }
    }

    struct PullRequest: Decodable, Identifiable, Equatable {
        let repo: String
        let number: Int
        let title: String
        let branch: String
        let seat: String
        let blocker: String
        let state: String
        /// What the blocker asks of people.  An older collector does not say, so it is
        /// worked out from the blocker with the collector's own table.
        let severity: PullRequestSeverity
        let draft: Bool
        /// The pull request on GitHub.  Only an https link is kept.
        let url: URL?

        var id: String { "\(repo)#\(number)" }
        /// Whether someone has to act before this can merge.  Waiting is not a problem.
        var needsAction: Bool { severity == .action }

        private enum CodingKeys: String, CodingKey {
            case repo, number, title, branch, seat, blocker, state, severity, draft, url
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            let repo = try? container.decode(String.self, forKey: .repo)
            let number = try? container.decode(Int.self, forKey: .number)
            let title = try? container.decode(String.self, forKey: .title)
            let branch = try? container.decode(String.self, forKey: .branch)
            let seat = try? container.decode(String.self, forKey: .seat)
            let blocker = try? container.decode(String.self, forKey: .blocker)
            let state = try? container.decode(String.self, forKey: .state)
            self.repo = repo ?? ""
            self.number = number ?? 0
            self.title = title ?? ""
            self.branch = branch ?? ""
            self.seat = seat ?? "UNKNOWN"
            self.blocker = blocker ?? "unknown"
            let texts: [String?] = [repo, title, branch, seat, blocker]
            let whole = texts.allSatisfy { $0 != nil } && number != nil
            self.state = whole ? (state ?? "UNKNOWN") : "UNKNOWN"
            let said = try? container.decodeIfPresent(String.self, forKey: .severity)
            if !whole {
                severity = .unknown
            } else if let said {
                severity = PullRequestSeverity(rawValue: said) ?? .unknown
            } else {
                severity = PullRequestSeverity(blocker: self.blocker)
            }
            draft = (try? container.decodeIfPresent(Bool.self, forKey: .draft)) ?? false
            url = FleetLinks.https(try? container.decodeIfPresent(String.self, forKey: .url))
        }

        /// The blocker, in the owner's words.
        var statusText: String {
            switch blocker {
            case "ready": return "Ready to merge"
            case "draft": return "Draft"
            case "changes requested": return "Changes requested"
            case "failing checks": return "Checks failing"
            case "checks need a rerun": return "Checks need a rerun"
            case "checks pending": return "Checks running"
            case "conflicting with main": return "Conflicts with main"
            case "behind main": return "Behind main"
            case "unknown": return "Merge state not known"
            case "waiting for review": return "Waiting for review"
            case "blocked (see PR)": return "Blocked by a branch rule"
            case "auto-merge not armed": return "Auto-merge not on"
            default: return blocker
            }
        }
    }

    struct Claim: Decodable, Identifiable, Equatable {
        let identifier: String
        let title: String
        let seat: String
        /// Seconds since the issue was last updated, -1 when not known.
        let ageSeconds: Int
        let expired: Bool
        /// The issue in Linear.  Only an https link is kept.
        let url: URL?

        var id: String { identifier }

        private enum CodingKeys: String, CodingKey {
            case identifier, title, seat, ageSeconds, expired, url
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            let identifier = try? container.decode(String.self, forKey: .identifier)
            let age = try? container.decode(Int.self, forKey: .ageSeconds)
            let expired = try? container.decode(Bool.self, forKey: .expired)
            self.identifier = identifier ?? ""
            title = (try? container.decode(String.self, forKey: .title)) ?? ""
            seat = (try? container.decode(String.self, forKey: .seat)) ?? "UNKNOWN"
            ageSeconds = age ?? -1
            // A claim whose age or flag did not decode needs a look, as the collector's own
            // unreadable stamp does:  it reads expired, never fresh.
            self.expired = expired ?? true || age == nil
            url = FleetLinks.https(try? container.decodeIfPresent(String.self, forKey: .url))
        }

        /// The time since the issue last changed, which is what the expiry ceiling measures.
        var ageText: String {
            ageSeconds < 0 ? "Last update not known" : "No update for \(FleetTime.age(ageSeconds))"
        }
    }
}

extension FleetViewEnvelope.PullRequest {
    /// The list's order:  what needs action first, then not known, then waiting, then ready.
    static func listOrder(_ left: Self, _ right: Self) -> Bool {
        if left.severity.rank != right.severity.rank { return left.severity.rank < right.severity.rank }
        if left.repo != right.repo { return left.repo < right.repo }
        return left.number < right.number
    }
}

extension FleetViewEnvelope.Claim {
    /// The list's order:  expired first, then the longest without an update.
    static func listOrder(_ left: Self, _ right: Self) -> Bool {
        if left.expired != right.expired { return left.expired }
        if left.ageSeconds != right.ageSeconds { return left.ageSeconds > right.ageSeconds }
        return left.identifier < right.identifier
    }
}

/// What a pull request's blocker asks of people.
///
/// The collector's table (`SEVERITY` in `scripts/fleet_view/collectors.py`) is the source;
/// `init(blocker:)` repeats it for an older collector that sends no severity.
enum PullRequestSeverity: String, Equatable {
    /// Someone has to do something:  failing checks, conflicts, requested changes.
    case action
    /// A normal step on the way to a merge:  a draft, running checks, a review to come.
    case waiting
    /// It can merge.
    case ready
    /// Not proved either way.
    case unknown

    init(blocker: String) {
        switch blocker {
        case "ready": self = .ready
        case "changes requested", "failing checks", "checks need a rerun", "conflicting with main":
            self = .action
        case "draft", "checks pending", "behind main", "waiting for review", "blocked (see PR)",
             "auto-merge not armed":
            self = .waiting
        default: self = .unknown
        }
    }

    /// The order the list shows them in:  what needs someone first.
    var rank: Int {
        switch self {
        case .action: return 0
        case .unknown: return 1
        case .waiting: return 2
        case .ready: return 3
        }
    }

    var label: String {
        switch self {
        case .action: return "Needs action"
        case .waiting: return "Waiting"
        case .ready: return "Ready"
        case .unknown: return "Not known"
        }
    }

    /// The symbol that goes with the words, never instead of them.
    var systemImage: String {
        switch self {
        case .action: return "exclamationmark.triangle.fill"
        case .waiting: return "clock"
        case .ready: return "checkmark.circle"
        case .unknown: return "questionmark.circle"
        }
    }
}

/// Links from the collector's data.  Only https opens, so a row can never open a file or an app.
enum FleetLinks {
    static func https(_ text: String?) -> URL? {
        guard let text, let url = URL(string: text), url.scheme == "https", url.host != nil else {
            return nil
        }
        return url
    }
}

/// Counts in words, with the noun agreeing:  `1 lane`, `2 lanes`.
enum FleetWords {
    static func count(_ number: Int, _ singular: String, _ plural: String? = nil) -> String {
        "\(number) \(number == 1 ? singular : (plural ?? singular + "s"))"
    }

    /// A machine's short name for a column or a chip:  `Mac` or `Server`.
    static func machine(_ host: FleetHost) -> String {
        switch host {
        case .mac: return "Mac"
        case .server: return "Server"
        }
    }
}

/// What a section shows when it has no rows to show (FLEETVIEW-11, COPY-25, A11Y-29).
///
/// Three different things, never one:  a filter hides every row, there truly are none,
/// or the section could not look, which is never shown as none.
enum FleetSectionEmpty: Equatable {
    case hiddenByFilter
    case noRows(String)
    case notKnown(String)

    /// Nil when there are rows to show.
    static func state(total: Int, shown: Int, isKnown: Bool, reason: String, noneText: String) -> FleetSectionEmpty? {
        if shown > 0 { return nil }
        if total > 0 { return .hiddenByFilter }
        if !isKnown { return .notKnown(reason) }
        return .noRows(noneText)
    }
}

/// One line of the Needs Attention list at the top of Apps & Work (FLEETVIEW-18).
struct FleetAttentionItem: Identifiable, Equatable {
    enum Kind: Equatable {
        case pullRequest
        case claim
        case notRead
    }

    let id: String
    let kind: Kind
    let text: String
    let url: URL?
}

extension FleetViewEnvelope {
    /// The machine the lanes are on, as a heading names it.
    var lanesHeading: String {
        guard let host else { return "Lanes, Machine Not Named" }
        return "Lanes \(host.onTitle)"
    }

    /// Lanes are read on one machine only, and the page says which one it is not showing.
    var lanesCaption: String {
        guard let host else {
            return "The collector did not name the machine these lanes are on."
        }
        let other: FleetHost = host == .mac ? .server : .mac
        return "Read from the lanes folder \(host.inProse).\u{00A0} Lanes \(other.inProse) are not shown here."
    }

    /// What needs someone, worst first:  pull requests that need action, expired claims, then
    /// whatever could not be read.  Each source that could not be read is one line, so the
    /// list never reads as clear while a source is missing.
    var attentionItems: [FleetAttentionItem] {
        var items: [FleetAttentionItem] = []
        let prs = sections.pullRequests.items
            .filter(\.needsAction)
            .sorted { ($0.repo, $0.number) < ($1.repo, $1.number) }
        for pr in prs {
            let repo = pr.repo.split(separator: "/").last.map(String.init) ?? pr.repo
            items.append(FleetAttentionItem(
                id: "pr:\(pr.id)",
                kind: .pullRequest,
                text: "\(repo) #\(pr.number):  \(pr.statusText.lowercased()).\u{00A0} \(pr.title)",
                url: pr.url
            ))
        }
        let claims = sections.claims.items.filter(\.expired).sorted { $0.ageSeconds > $1.ageSeconds }
        for claim in claims {
            items.append(FleetAttentionItem(
                id: "claim:\(claim.id)",
                kind: .claim,
                text: "\(claim.identifier) claim expired, \(claim.ageText.lowercased()).\u{00A0} \(claim.title)",
                url: claim.url
            ))
        }
        if !sections.pullRequests.isKnown {
            items.append(FleetAttentionItem(
                id: "unread:pullRequests", kind: .notRead,
                text: "Pull requests were not all read:  \(sections.pullRequests.reason)", url: nil
            ))
        }
        if !sections.claims.isKnown {
            items.append(FleetAttentionItem(
                id: "unread:claims", kind: .notRead,
                text: "Claims were not all read:  \(sections.claims.reason)", url: nil
            ))
        }
        let unreadableLanes = unreadableLaneCount
        if !sections.lanes.isKnown {
            items.append(FleetAttentionItem(
                id: "unread:lanes", kind: .notRead,
                text: "Lanes were not all read:  \(sections.lanes.reason)", url: nil
            ))
        } else if unreadableLanes > 0 {
            let place = host?.inProse ?? "on the machine that ran this"
            items.append(FleetAttentionItem(
                id: "unread:lanes", kind: .notRead,
                text: "\(FleetWords.count(unreadableLanes, "lane")) \(place) could not be read by git.", url: nil
            ))
        }
        return items
    }

    /// Where this page's data came from, in the owner's words (every panel says so).
    var sourceText: String {
        let place = host?.inProse ?? "on a machine the collector did not name"
        var parts = [
            "Lanes from the lanes folder \(place).",
            "Pull requests from GitHub (\(FleetWords.count(requestCounts.github, "request")) this run).",
            "Claims from Linear (\(FleetWords.count(requestCounts.linear, "request")) this run).",
        ]
        if let durationSeconds {
            parts.append("The run took \(FleetWords.count(Int(durationSeconds.rounded()), "second")).")
        }
        return parts.joined(separator: "\u{00A0} ")
    }

    /// Lanes git could not read, on the machine that ran the collector.
    var unreadableLaneCount: Int {
        sections.lanes.items.filter { !$0.isReadable }.count
    }

    /// Whether every section read its source and every lane in it could be read.  A lane git
    /// could not read leaves its section's count exact, but the page must not say all is read.
    var everythingRead: Bool {
        sourcesRead && unreadableLaneCount == 0
    }

    private var sourcesRead: Bool {
        overallVerdict == "PASS" && sections.lanes.isKnown && sections.pullRequests.isKnown && sections.claims.isKnown
    }

    /// The run's own verdict, in words.
    var verdictText: String {
        if everythingRead { return "Every source was read." }
        if sourcesRead {
            let place = host?.inProse ?? "on the machine that ran this"
            return "Every source was read, but \(FleetWords.count(unreadableLaneCount, "lane")) \(place) could not be read by git."
        }
        return "Some sources could not be read, so some counts below are not known."
    }

    private var overallVerdict: String { summary.overallVerdict }

    /// The claims header's rule, when the collector said which ceiling it used.
    var expiryText: String? {
        guard let hours = claimExpiryHours, hours > 0 else { return nil }
        return "Expired means no update for more than \(FleetWords.count(hours, "hour"))."
    }
}

/// Lanes in one repo, for the grouped list (FLEETVIEW-17).
struct FleetLaneGroup: Identifiable, Equatable {
    let repo: String
    let lanes: [FleetViewEnvelope.Lane]

    var id: String { repo }
    var unreadable: Int { lanes.filter { !$0.isReadable }.count }
    var changed: Int { lanes.filter { $0.dirtyFiles > 0 }.count }

    /// One line for the group's row.
    var summaryText: String {
        var parts = [FleetWords.count(lanes.count, "lane")]
        if changed > 0 { parts.append("\(changed) with uncommitted changes") }
        if unreadable > 0 { parts.append("\(unreadable) could not be read") }
        return parts.joined(separator: " · ")
    }

    /// Repos with lanes git could not read come first, then repos with uncommitted changes,
    /// then the busiest.  Inside a repo the same order holds, then the most recent commit.
    static func groups(_ lanes: [FleetViewEnvelope.Lane]) -> [FleetLaneGroup] {
        let byRepo = Dictionary(grouping: lanes, by: \.repo)
        let groups = byRepo.map { repo, lanes in
            FleetLaneGroup(repo: repo, lanes: lanes.sorted(by: laneOrder))
        }
        return groups.sorted { left, right in
            if (left.unreadable > 0) != (right.unreadable > 0) { return left.unreadable > 0 }
            if (left.changed > 0) != (right.changed > 0) { return left.changed > 0 }
            if left.lanes.count != right.lanes.count { return left.lanes.count > right.lanes.count }
            return left.repo < right.repo
        }
    }

    private static func laneOrder(_ left: FleetViewEnvelope.Lane, _ right: FleetViewEnvelope.Lane) -> Bool {
        if left.isReadable != right.isReadable { return !left.isReadable }
        if (left.dirtyFiles > 0) != (right.dirtyFiles > 0) { return left.dirtyFiles > 0 }
        let leftAge = left.lastCommitSeconds < 0 ? Int.max : left.lastCommitSeconds
        let rightAge = right.lastCommitSeconds < 0 ? Int.max : right.lastCommitSeconds
        if leftAge != rightAge { return leftAge < rightAge }
        return left.lane < right.lane
    }
}

// `FleetTime` lives in `Model/FleetConfigContract.swift`:  the grid, the fleet view and
// the apply flow all read the clock, and one type keeps the two views agreeing on it.
