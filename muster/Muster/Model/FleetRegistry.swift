import Foundation

/// The canonical fleet inventory from `fleet-apps.json`,  paired with live activity from
/// `fleet-view` and tool versions from `fleet-tools-doctor`.
///
/// Nothing here reaches the network or reads a credential:  it reads `fleet-apps.json` from
/// the repository checkout and joins it with the already-collected `FleetViewEnvelope` and
/// `FleetToolsReading` snapshots.
struct FleetRegistryDocument: Decodable, Equatable {
    struct AppEntry: Decodable, Equatable, Identifiable {
        let repo: String
        let acronym: String
        let displayName: String?
        let codeDir: String?
        let worktreePrefix: String?
        let kind: String?
        let notes: String?

        var id: String { acronym }

        var title: String {
            if let displayName, !displayName.isEmpty { return displayName }
            return repo
        }

        var kindLabel: String {
            kind ?? "unknown"
        }
    }

    struct SeatEntry: Decodable, Equatable, Identifiable {
        let tag: String
        let notesName: String?
        let worktreeSuffix: String?
        let branchPrefixes: [String]?
        let zulipBotEmail: String?
        let zulipFileCode: String?
        let retired: Bool?
        let aliasOf: String?
        let notes: String?

        var id: String { tag }

        var isLive: Bool { retired != true }
    }

    struct SeatAppsSection: Decodable, Equatable {
        let bySeat: [String: String]?
        let extraSeats: [String: String]?
        struct Pattern: Decodable, Equatable {
            let match: String
            let app: String
        }
        let patterns: [Pattern]?
    }

    let owner: String?
    let codeRoot: String?
    let appsRoot: String?
    let zulipRealm: String?
    let zulipChannel: String?
    let apps: [AppEntry]
    let seats: [SeatEntry]
    let seatApps: SeatAppsSection?

    var liveSeats: [SeatEntry] {
        seats.filter { $0.isLive && !NotASeat.contains($0.tag) }
    }
}

/// Registry tags that name a group and are never a seat.
///
/// Grok Bots are a group of seats, each with its own name (`GB-<NAME>`), and no seat is ever
/// called `GROK-BOT` or carries it as a branch or pull request prefix.  The registry still
/// has a row for the group, so the app leaves it out of every list of seats:  the roster the
/// engine's grid and Seat Setup show, and the seats an app row names.  The group's own
/// members come in through the `GB-*` pattern.
enum NotASeat {
    static let tags: Set<String> = ["GROK-BOT"]

    static func contains(_ tag: String) -> Bool {
        tags.contains(tag)
    }
}

/// A count of work, and whether it is the whole count (FLEETVIEW-4).
///
/// A section that could not look has no count:  its rows say "not known", or "at least N"
/// when some were seen, and never a zero that reads as measured and idle.
struct WorkCount: Equatable {
    let observed: Int
    let complete: Bool

    /// The count for a column:  `3`, `at least 2` or `not known`.
    var text: String {
        if complete { return "\(observed)" }
        return observed == 0 ? "not known" : "at least \(observed)"
    }

    /// The count with its noun:  `3 lanes`, `at least 2 pull requests` or `pull requests not known`.
    func phrase(_ singular: String, _ plural: String) -> String {
        if complete { return FleetWords.count(observed, singular, plural) }
        return observed == 0 ? "\(plural) not known" : "at least \(FleetWords.count(observed, singular, plural))"
    }
}

/// What an app's open work asks of the owner (FLEETVIEW-9).
///
/// It describes lanes, pull requests and claims only.  It says nothing about whether the
/// app is up, and its words do not borrow the grid's PASS, which would read as if it did.
/// Uncommitted changes in a lane are work, not a problem.
enum AppWorkState: String, CaseIterable, Equatable {
    /// A pull request needs action, or a claim has expired.
    case needsAttention = "Needs Attention"
    /// Something this state depends on was not read.
    case notKnown = "Not Known"
    /// Lanes, pull requests or claims are open, and none of them needs anyone.
    case inProgress = "In Progress"
    /// Nothing is open.
    case quiet = "Quiet"

    var label: String { rawValue }

    /// The symbol that goes with the words, never instead of them.
    var systemImage: String {
        switch self {
        case .needsAttention: return "exclamationmark.triangle.fill"
        case .notKnown: return "questionmark.circle"
        case .inProgress: return "hammer"
        case .quiet: return "moon.zzz"
        }
    }
}

/// The open work for one production, library, infra, or marketing app in `fleet-apps.json`.
struct FleetAppStatus: Identifiable, Equatable {
    let acronym: String
    let repo: String
    let title: String
    let kind: String
    let codeDir: String?
    /// Whether the app's folder is in the code root on the Mac, the machine Muster runs on.
    let hasCodeCheckout: Bool
    /// Lanes on the machine that ran the collector (the Mac).
    let lanes: WorkCount
    let dirtyLanes: Int
    let unreadableLanes: Int
    let pullRequests: WorkCount
    /// Pull requests where someone has to act:  failing checks, conflicts, requested changes.
    let actionPullRequests: Int
    let claims: WorkCount
    let expiredClaims: Int
    let state: AppWorkState
    /// Why the state is what it is, in the owner's words.
    let reasons: [String]

    var id: String { acronym }
    var activeLanes: Int { lanes.observed }
    var openPullRequests: Int { pullRequests.observed }
    var activeClaims: Int { claims.observed }

    var hasActivity: Bool {
        activeLanes > 0 || openPullRequests > 0 || activeClaims > 0
    }

    /// The kind as a chip shows it:  `Product`, `Infra`.
    var kindLabel: String {
        kind.prefix(1).uppercased() + kind.dropFirst()
    }

    /// Said only when it is worth saying:  a missing folder on the Mac.
    var checkoutText: String? {
        hasCodeCheckout ? nil : "No checkout in the code folder on the Mac"
    }
}

/// How one machine's copy of a coding app's tool reads in the tools report.
enum CLIOutcome: Equatable {
    case current
    case behind
    /// A required tool that is not installed.
    case missing
    /// A tool whose check failed for another reason.
    case failing
    case notKnown
    /// An optional tool that is not installed, which is information and not a fault.
    case notInstalled
    /// The report has no row for this tool on this machine.
    case notChecked
    /// The doctor could not reach this machine.
    case notReached
}

/// One machine's reading of a coding app's tool (FLEETVIEW-8, FLEETVIEW-13, HOSTAUDIT-27).
struct CLIReading: Equatable, Identifiable {
    let host: FleetHost
    let version: String?
    let outcome: CLIOutcome

    var id: String { host.rawValue }

    /// The reading in words, naming its machine.
    var text: String {
        let machine = FleetWords.machine(host)
        let version = (self.version ?? "").isEmpty ? nil : self.version
        switch outcome {
        case .current: return "\(machine) \(version ?? "version not shown"), up to date"
        case .behind: return "\(machine) \(version ?? "version not shown"), behind"
        case .missing: return "\(machine):  required, not installed"
        case .failing: return "\(machine) \(version ?? "version not shown"), failing its check"
        case .notKnown: return "\(machine):  version not known"
        case .notInstalled: return "\(machine):  not installed (optional)"
        case .notChecked: return "\(machine):  not checked"
        case .notReached: return "\(machine):  not reached"
        }
    }
}

/// One coding app or harness, the seats that run in it, its tool on each machine, and its
/// seats' open work.
struct CodingAppStatus: Identifiable, Equatable {
    let appSlug: String
    let displayName: String
    /// The registry's seat tags.  A family pattern keeps its `*`.
    let seats: [String]
    let cliTool: String?
    /// Where the app runs, in words, when it has no tool the report tracks.
    let runsOn: String
    /// The Mac's version and state, when the tools report is current.
    let cliVersion: String?
    let cliState: FleetToolsRowState?
    let doctorReportCurrent: Bool
    /// One reading per machine, the Mac first.  Empty when the app has no tracked tool.
    let toolReadings: [CLIReading]
    let lanes: WorkCount
    let pullRequests: WorkCount
    let claims: WorkCount

    var id: String { appSlug }
    var activeLanes: Int { lanes.observed }
    var openPullRequests: Int { pullRequests.observed }
    var activeClaims: Int { claims.observed }

    /// The seats as the owner reads them:  a family is `BF-<role>`, not `BF-*`.
    var seatLabels: [String] {
        seats.map { $0.hasSuffix("*") ? String($0.dropLast()) + "<role>" : $0 }
    }

    /// Whether a machine's tool is failing, missing or behind.
    var hasToolProblem: Bool {
        let problems: [CLIOutcome] = [.missing, .failing, .behind]
        return toolReadings.contains { problems.contains($0.outcome) }
    }
}

/// Combined fleet inventory and status for production apps and coding apps.
struct FleetRegistrySummary: Equatable {
    var zulipChannel: String = "work-in-progress"
    var apps: [FleetAppStatus] = []
    var codingApps: [CodingAppStatus] = []
    var problem: String? = nil
    /// Why tool versions are not known, when they are not.
    var toolsNote: String? = nil
    /// When the tools report was written, when one was read.
    var toolsReportAt: Date? = nil

    /// The channel as the fleet writes it for people ("Work-In-Progress").  The registry keeps Zulip's lowercase slug.
    var zulipChannelDisplayName: String {
        zulipChannel
            .split(separator: "-", omittingEmptySubsequences: false)
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: "-")
    }

    /// The app a repo belongs to.  A repo may come as `Owner/Repo` or `Repo`.
    func app(forRepo repo: String) -> FleetAppStatus? {
        let name = repo.split(separator: "/").last.map(String.init) ?? repo
        return apps.first { $0.repo == name }
    }

    /// The app a Linear issue belongs to:  its identifier starts with the app's acronym.
    func app(forClaim identifier: String) -> FleetAppStatus? {
        apps.first { identifier.hasPrefix("\($0.acronym)-") }
    }

    /// Where the tool versions came from and how old they are.
    var toolsSourceText: String {
        if let toolsNote { return toolsNote }
        guard let toolsReportAt else { return "Tool versions from the tools report." }
        return "Tool versions from the tools report written at \(FleetTime.clock(toolsReportAt))."
    }
}

/// The page's filters, applied to every section alike (FLEETVIEW-33).
enum FleetAppKindFilter: String, CaseIterable, Identifiable {
    case all = "All Apps"
    case product = "Products"
    case infraAndLibrary = "Infra, Library & Marketing"
    case active = "Active Only"

    var id: String { rawValue }

    /// Whether an app of this kind passes.  A row that belongs to no app passes only All.
    func includes(kind: String?) -> Bool {
        switch self {
        case .all, .active: return true
        case .product: return kind == "product"
        case .infraAndLibrary: return kind == "infra" || kind == "library" || kind == "marketing"
        }
    }
}

struct FleetViewFilter: Equatable {
    var kind: FleetAppKindFilter = .all
    var query: String = ""
    var attentionOnly = false

    private var needle: String { query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }

    var isActive: Bool { kind != .all || !needle.isEmpty || attentionOnly }

    private func matches(_ fields: [String]) -> Bool {
        let text = needle
        guard !text.isEmpty else { return true }
        return fields.contains { $0.lowercased().contains(text) }
    }

    private func appFields(_ app: FleetAppStatus?) -> [String] {
        guard let app else { return [] }
        return [app.acronym, app.title, app.repo]
    }

    func includes(_ app: FleetAppStatus) -> Bool {
        guard kind.includes(kind: app.kind) else { return false }
        if kind == .active, !app.hasActivity, app.state != .needsAttention, app.state != .notKnown {
            return false
        }
        if attentionOnly, app.state != .needsAttention { return false }
        return matches(appFields(app) + [app.kind])
    }

    func includes(_ codingApp: CodingAppStatus) -> Bool {
        if attentionOnly, !codingApp.hasToolProblem { return false }
        return matches([codingApp.displayName, codingApp.appSlug, codingApp.cliTool ?? ""] + codingApp.seats)
    }

    func includes(_ lane: FleetViewEnvelope.Lane, app: FleetAppStatus?) -> Bool {
        guard kind.includes(kind: app?.kind) else { return false }
        if attentionOnly, lane.isReadable { return false }
        return matches([lane.seat, lane.repo, lane.lane, lane.branch] + appFields(app))
    }

    func includes(_ pr: FleetViewEnvelope.PullRequest, app: FleetAppStatus?) -> Bool {
        guard kind.includes(kind: app?.kind) else { return false }
        if attentionOnly, !pr.needsAction { return false }
        return matches([pr.seat, pr.repo, pr.title, pr.branch, "\(pr.number)"] + appFields(app))
    }

    func includes(_ claim: FleetViewEnvelope.Claim, app: FleetAppStatus?) -> Bool {
        guard kind.includes(kind: app?.kind) else { return false }
        if attentionOnly, !claim.expired { return false }
        return matches([claim.seat, claim.identifier, claim.title] + appFields(app))
    }
}

/// What a coding app is and where it runs.  `cli` is the `tool:<name>` row the tools report
/// keeps for it;  `runsOn` says where it runs when there is no such row, so nothing is
/// inferred from a missing entry (FLEETVIEW-12, FLEETVIEW-14, HOSTAUDIT-12).
struct CodingAppMetadata: Equatable, Sendable {
    let title: String
    let cli: String?
    let runsOn: String
}

enum FleetRegistryReader {
    /// What a slug with no row below shows.  The real registry is tested to have none.
    static let notTracked = "Not tracked here"

    /// Every `seatApps` slug:  its title, its tool, and where it runs.
    static let codingAppMetadata: [String: CodingAppMetadata] = [
        "claude-code": CodingAppMetadata(title: "Claude Code", cli: "claude", runsOn: ""),
        "codex": CodingAppMetadata(title: "Codex", cli: "codex", runsOn: ""),
        "cursor": CodingAppMetadata(title: "Cursor", cli: "cursor-agent", runsOn: ""),
        "antigravity": CodingAppMetadata(title: "Antigravity", cli: "agy", runsOn: ""),
        "grok-terminal": CodingAppMetadata(title: "Grok Terminal", cli: "grok", runsOn: ""),
        "grok-web": CodingAppMetadata(title: "Grok Web", cli: nil, runsOn: "Hosted, no local tool"),
        "grok-bot": CodingAppMetadata(title: "Grok Bots", cli: nil, runsOn: "Hosted, through Cursor cloud agents"),
        "muse-code": CodingAppMetadata(title: "Muse Code", cli: "muse", runsOn: ""),
        "muse-assist": CodingAppMetadata(title: "Muse Assist", cli: nil, runsOn: FleetRegistryReader.notTracked),
        "minimax-code": CodingAppMetadata(title: "MiniMax Code", cli: "mcode", runsOn: ""),
        "fx": CodingAppMetadata(title: "FX", cli: "fx", runsOn: ""),
        "opencode": CodingAppMetadata(title: "OpenCode", cli: "opencode", runsOn: ""),
        "clutch": CodingAppMetadata(title: "Clutch", cli: "clutch", runsOn: ""),
        "kimi-code": CodingAppMetadata(title: "Kimi Code", cli: "kimi", runsOn: ""),
        "zcode": CodingAppMetadata(title: "Z Code", cli: nil, runsOn: "Desktop app, version not tracked"),
        "instinct": CodingAppMetadata(
            title: "Instinct", cli: nil, runsOn: "Its own computer, through the hosted bridge"
        ),
        "chatgpt": CodingAppMetadata(title: "ChatGPT", cli: nil, runsOn: "Hosted, no local tool"),
        "amp": CodingAppMetadata(title: "Amp", cli: nil, runsOn: "Hosted or owner-run, not tracked here"),
        // The Operator is a listener service, not a command line tool:  the simple-chat CLI's
        // version says nothing about it, so no tool row stands in for it.
        "operator": CodingAppMetadata(title: "The Operator", cli: nil, runsOn: "A service, whose state is not read here"),
        "botfleet": CodingAppMetadata(title: "BotFleet Personas", cli: nil, runsOn: FleetRegistryReader.notTracked),
    ]

    /// Read `fleet-apps.json` at the repository root and build the combined summary.
    static func read(
        repoRoot: URL,
        viewOutput: FleetViewEnvelope? = nil,
        toolsReading: FleetToolsReading? = nil,
        pathExists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) -> FleetRegistrySummary {
        let url = repoRoot.appendingPathComponent("fleet-apps.json", isDirectory: false)
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            return FleetRegistrySummary(
                problem: "fleet-apps.json could not be read:  \(error.localizedDescription)"
            )
        }
        return build(
            from: data,
            viewOutput: viewOutput,
            toolsReading: toolsReading,
            pathExists: pathExists
        )
    }

    /// Why tool versions are not known, or nil when the report is current.
    static func toolsNote(_ reading: FleetToolsReading?) -> String? {
        guard let reading else {
            return "No tools report was read, so tool versions are not known."
        }
        if reading.isCurrent { return nil }
        if reading.refusal != nil {
            return "This Muster cannot read the tools report's format, so tool versions are not known."
        }
        if let problem = reading.problem {
            return "Tool versions are not known, because the tools report was not usable:  \(problem)"
        }
        if let age = reading.age {
            return "The tools report was written \(age) ago, which is too old to trust, so tool versions are not known."
        }
        return "The tools report is out of date, so tool versions are not known."
    }

    /// One machine's reading of a tool, from the tools report.
    static func cliReading(cli: String, host: FleetHost, reading: FleetToolsReading?) -> CLIReading {
        guard let reading, reading.isCurrent else {
            return CLIReading(host: host, version: nil, outcome: .notKnown)
        }
        guard let verdict = reading.hosts.first(where: { $0.host == host.rawValue }) else {
            return CLIReading(host: host, version: nil, outcome: .notChecked)
        }
        guard verdict.reachable else {
            return CLIReading(host: host, version: nil, outcome: .notReached)
        }
        guard let row = verdict.allRows.first(where: { $0.check == "tool:\(cli)" }) else {
            return CLIReading(host: host, version: nil, outcome: .notChecked)
        }
        let outcome: CLIOutcome
        switch row.outcome {
        case .passing: outcome = .current
        case .stale: outcome = .behind
        case .failing: outcome = row.rowState == .missing ? .missing : .failing
        case .unknown: outcome = .notKnown
        case .absent: outcome = .notInstalled
        }
        return CLIReading(host: host, version: row.installed, outcome: outcome)
    }

    /// Build the summary from raw `fleet-apps.json` bytes (used directly by unit tests).
    static func build(
        from data: Data,
        viewOutput: FleetViewEnvelope? = nil,
        toolsReading: FleetToolsReading? = nil,
        pathExists: (String) -> Bool = { _ in false }
    ) -> FleetRegistrySummary {
        let document: FleetRegistryDocument
        do {
            document = try JSONDecoder().decode(FleetRegistryDocument.self, from: data)
        } catch {
            return FleetRegistrySummary(
                problem: "fleet-apps.json could not be decoded:  \(error.localizedDescription)"
            )
        }

        let owner = document.owner ?? "Simple-With-Us"
        let codeRoot = document.codeRoot ?? "/Users/jay/Code"
        let lanes = viewOutput?.sections.lanes.items ?? []
        let prs = viewOutput?.sections.pullRequests.items ?? []
        let claims = viewOutput?.sections.claims.items ?? []
        let lanesKnown = viewOutput?.sections.lanes.isKnown ?? false
        let claimsKnown = viewOutput?.sections.claims.isKnown ?? false
        let prSection = viewOutput?.sections.pullRequests
        let lanePlace = viewOutput?.host?.inProse ?? "on the machine that ran the collector"

        /// Whether every pull request for these repo names was read.  A section that names
        /// what it could not read is complete for every repo it does not name.
        func prsComplete(for names: [String]) -> Bool {
            guard let prSection else { return false }
            if prSection.isKnown { return true }
            guard prSection.droppedItems == 0, let unread = prSection.unread, !unread.isEmpty else {
                return false
            }
            return !names.contains { unread.contains($0) }
        }

        let appStatuses: [FleetAppStatus] = document.apps.map { app in
            let repoLanes = lanes.filter { $0.repo == app.repo }
            let dirty = repoLanes.filter { $0.dirtyFiles > 0 }.count
            let unreadable = repoLanes.filter { !$0.isReadable }.count
            let fullRepo = "\(owner)/\(app.repo)"
            let repoPRs = prs.filter { $0.repo == fullRepo || $0.repo == app.repo }
            let actionPRs = repoPRs.filter(\.needsAction).count
            let appClaims = claims.filter { $0.identifier.hasPrefix("\(app.acronym)-") }
            let expired = appClaims.filter(\.expired).count

            let resolvedCodeDir: String? = {
                let dir = app.codeDir ?? app.repo
                return dir.isEmpty ? nil : dir
            }()
            let hasCode: Bool = {
                guard let dir = resolvedCodeDir else { return false }
                return pathExists("\(codeRoot)/\(dir)")
            }()

            let laneCount = WorkCount(observed: repoLanes.count, complete: lanesKnown)
            // A row with a field that did not decode is counted but not fully read, so the
            // count is "at least", never exact.
            let prCount = WorkCount(
                observed: repoPRs.count,
                complete: prsComplete(for: [fullRepo, app.repo]) && repoPRs.allSatisfy(\.isReadable)
            )
            let claimCount = WorkCount(
                observed: appClaims.count,
                complete: claimsKnown && appClaims.allSatisfy(\.isReadable)
            )

            var reasons: [String] = []
            if actionPRs > 0 {
                reasons.append("\(FleetWords.count(actionPRs, "pull request needs", "pull requests need")) action.")
            }
            if expired > 0 {
                reasons.append("\(FleetWords.count(expired, "claim has", "claims have")) expired.")
            }
            var notKnown = false
            if viewOutput == nil {
                reasons.append("Lanes, pull requests and claims were not read.")
                notKnown = true
            } else {
                if !laneCount.complete {
                    reasons.append("Lanes \(lanePlace) were not all read.")
                    notKnown = true
                }
                if unreadable > 0 {
                    reasons.append("\(FleetWords.count(unreadable, "lane")) could not be read by git.")
                    notKnown = true
                }
                if !prCount.complete {
                    reasons.append("Pull requests for \(app.repo) were not all read.")
                    notKnown = true
                }
                if !claimCount.complete {
                    // A claims read is one call for every app, so when it fails every app's
                    // claims are unknown, observed ones included (FLEETVIEW-3).
                    reasons.append("Claims were not all read.")
                    notKnown = true
                }
            }

            let state: AppWorkState
            if actionPRs > 0 || expired > 0 {
                state = .needsAttention
            } else if notKnown {
                state = .notKnown
            } else if !repoLanes.isEmpty || !repoPRs.isEmpty || !appClaims.isEmpty {
                state = .inProgress
            } else {
                state = .quiet
            }

            return FleetAppStatus(
                acronym: app.acronym,
                repo: app.repo,
                title: app.title,
                kind: app.kindLabel,
                codeDir: resolvedCodeDir,
                hasCodeCheckout: hasCode,
                lanes: laneCount,
                dirtyLanes: dirty,
                unreadableLanes: unreadable,
                pullRequests: prCount,
                actionPullRequests: actionPRs,
                claims: claimCount,
                expiredClaims: expired,
                state: state,
                reasons: reasons
            )
        }

        // Group live seats by their coding app slug in `seatApps.bySeat` and `patterns`.
        let bySeat = document.seatApps?.bySeat ?? [:]
        let patterns = document.seatApps?.patterns ?? []
        var orderedSlugs: [String] = []
        var seatsBySlug: [String: [String]] = [:]
        for seat in document.liveSeats {
            let slug: String = {
                if let direct = bySeat[seat.tag] { return direct }
                for pat in patterns {
                    if pat.match.hasSuffix("*") {
                        let prefix = String(pat.match.dropLast())
                        if seat.tag.hasPrefix(prefix) { return pat.app }
                    } else if seat.tag == pat.match {
                        return pat.app
                    }
                }
                return seat.tag.lowercased()
            }()
            if seatsBySlug[slug] == nil {
                orderedSlugs.append(slug)
                seatsBySlug[slug] = []
            }
            seatsBySlug[slug]?.append(seat.tag)
        }
        if let extra = document.seatApps?.extraSeats {
            for (seatTag, slug) in extra.sorted(by: { $0.key < $1.key }) {
                if seatsBySlug[slug] == nil {
                    orderedSlugs.append(slug)
                    seatsBySlug[slug] = []
                }
                if !(seatsBySlug[slug]?.contains(seatTag) ?? false) {
                    seatsBySlug[slug]?.append(seatTag)
                }
            }
        }
        for pat in patterns {
            if seatsBySlug[pat.app] == nil {
                orderedSlugs.append(pat.app)
                seatsBySlug[pat.app] = []
            }
            if !(seatsBySlug[pat.app]?.contains(pat.match) ?? false) {
                seatsBySlug[pat.app]?.append(pat.match)
            }
        }

        let toolsCurrent = toolsReading?.isCurrent == true
        let macRows = toolsCurrent
            ? (toolsReading?.hosts.first(where: { $0.host == FleetHost.mac.rawValue })?.allRows ?? [])
            : []
        let prsAllKnown = prSection?.isKnown ?? false
        // A lane git could not read may have no seat (a short folder name is resolved from its
        // branch, which could not be read).  It belongs to no app, so it could be any app's, and
        // no app's lane count is exact while one exists.
        let unreadableUnattributedLanes = lanes.filter { !$0.isReadable && $0.seat == "UNKNOWN" }.count
        let codingStatuses: [CodingAppStatus] = orderedSlugs.map { slug in
            let meta = codingAppMetadata[slug] ?? CodingAppMetadata(title: slug, cli: nil, runsOn: notTracked)
            let seatTags = seatsBySlug[slug] ?? []
            // A shared branch prefix names more than one seat ("ECHO or WISE"), and the row
            // belongs to each of them.
            let matchesSeat: (String) -> Bool = { candidate in
                for name in candidate.components(separatedBy: " or ") {
                    for tag in seatTags {
                        if tag.hasSuffix("*") {
                            if name.hasPrefix(String(tag.dropLast())) { return true }
                        } else if name == tag {
                            return true
                        }
                    }
                }
                return false
            }
            let cliRow = meta.cli.flatMap { cliName in
                macRows.first(where: { $0.check == "tool:\(cliName)" })
            }
            let readings = meta.cli.map { cli in
                FleetHost.allCases.map { cliReading(cli: cli, host: $0, reading: toolsReading) }
            } ?? []
            let seatLanes = lanes.filter { matchesSeat($0.seat) }
            let seatLanesReadable = !seatLanes.contains { !$0.isReadable }

            return CodingAppStatus(
                appSlug: slug,
                displayName: meta.title,
                seats: seatTags,
                cliTool: meta.cli,
                runsOn: meta.runsOn,
                cliVersion: cliRow?.installed,
                cliState: cliRow?.rowState,
                doctorReportCurrent: toolsCurrent,
                toolReadings: readings,
                lanes: WorkCount(
                    observed: seatLanes.count,
                    complete: lanesKnown && seatLanesReadable && unreadableUnattributedLanes == 0
                ),
                pullRequests: WorkCount(observed: prs.filter { matchesSeat($0.seat) }.count, complete: prsAllKnown),
                claims: WorkCount(observed: claims.filter { matchesSeat($0.seat) }.count, complete: claimsKnown)
            )
        }

        return FleetRegistrySummary(
            zulipChannel: document.zulipChannel ?? "work-in-progress",
            apps: appStatuses,
            codingApps: codingStatuses,
            problem: nil,
            toolsNote: toolsNote(toolsReading),
            toolsReportAt: toolsReading?.generatedAt
        )
    }
}
