import Foundation

/// The canonical fleet inventory from `fleet-apps.json`,  paired with live activity from
/// `fleet-view` and CLI currency from `fleet-tools-doctor`.
///
/// Nothing here reaches the network or reads a credential:  it reads `fleet-apps.json` from
/// the repository checkout and joins it with the already-collected `FleetViewOutput` and
/// `FleetToolsReading` snapshots.
struct FleetRegistryDocument: Decodable, Equatable {
    struct AppEntry: Decodable, Equatable, Identifiable {
        let repo: String
        let acronym: String
        let displayName: String?
        let codeDir: String?
        let worktreePrefix: String?
        let integrationDir: String?
        let publishScript: String?
        let kind: String?
        let visibility: String?
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
        seats.filter(\.isLive)
    }
}

/// Live status of one production, library, infra, or marketing app in `fleet-apps.json`.
struct FleetAppStatus: Identifiable, Equatable {
    let acronym: String
    let repo: String
    let title: String
    let kind: String
    let visibility: String
    let codeDir: String?
    let publishScript: String?
    let hasCodeCheckout: Bool
    let hasPublishScript: Bool
    let activeLanes: Int
    let dirtyLanes: Int
    let openPullRequests: Int
    let blockedPullRequests: Int
    let activeClaims: Int
    let expiredClaims: Int
    let state: CellState

    var id: String { acronym }

    var hasIntegrationCheckout: Bool { hasCodeCheckout }

    var hasActivity: Bool {
        activeLanes > 0 || openPullRequests > 0 || activeClaims > 0
    }
}

/// Live status of one coding app / harness across its fleet seats.
struct CodingAppStatus: Identifiable, Equatable {
    let appSlug: String
    let displayName: String
    let seats: [String]
    let cliTool: String?
    let cliVersion: String?
    let cliState: FleetToolsRowState?
    let activeLanes: Int
    let openPullRequests: Int
    let activeClaims: Int

    var id: String { appSlug }
}

/// Combined fleet inventory and status for production apps and coding apps.
struct FleetRegistrySummary: Equatable {
    var zulipChannel: String = "work-in-progress"
    var apps: [FleetAppStatus] = []
    var codingApps: [CodingAppStatus] = []
    var problem: String? = nil
}

enum FleetRegistryReader {
    /// Mapping from `seatApps.bySeat` app slug to human display title and `fleet-tools-doctor` CLI name.
    private static let codingAppMetadata: [String: (title: String, cli: String?)] = [
        "claude-code": ("Claude Code", "claude"),
        "codex": ("Codex", "codex"),
        "cursor": ("Cursor", "cursor-agent"),
        "antigravity": ("Antigravity", "agy"),
        "grok-terminal": ("Grok Terminal", "grok"),
        "grok-web": ("Grok Web", nil),
        "grok-bot": ("Grok Bot", nil),
        "grok": ("Grok", "grok"),
        "muse-code": ("Muse Code", "muse"),
        "muse-assist": ("Muse Assist", nil),
        "muse": ("Muse", "muse"),
        "minimax-code": ("MiniMax Code", "mcode"),
        "fx": ("FX", "fx"),
        "opencode": ("OpenCode", "opencode"),
        "clutch": ("Clutch", "clutch"),
        "instinct": ("Instinct (Hosted ST-MCP)", nil),
        "chatgpt": ("ChatGPT", nil),
        "openai": ("OpenAI Dot", nil),
        "operator": ("The Operator", "simple-chat"),
        "botfleet": ("BotFleet Personas", nil),
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
        let appsRoot = document.appsRoot ?? "/Users/jay/apps"
        let codeRoot = document.codeRoot ?? "/Users/jay/Code"
        let lanes = viewOutput?.sections.lanes.items ?? []
        let prs = viewOutput?.sections.pullRequests.items ?? []
        let claims = viewOutput?.sections.claims.items ?? []

        let appStatuses: [FleetAppStatus] = document.apps.map { app in
            let repoLanes = lanes.filter { $0.repo == app.repo }
            let dirty = repoLanes.filter { $0.dirtyFiles > 0 }.count
            let fullRepo = "\(owner)/\(app.repo)"
            let repoPRs = prs.filter { $0.repo == fullRepo || $0.repo == app.repo }
            let blockedPRs = repoPRs.filter { $0.blocker != "ready" }
            let failingOrConflictingPRs = repoPRs.filter {
                $0.blocker == "failing checks"
                    || $0.blocker == "conflicting with main"
                    || $0.blocker == "changes requested"
            }
            let prefix = "\(app.acronym)-"
            let appClaims = claims.filter { $0.identifier.hasPrefix(prefix) }
            let expired = appClaims.filter(\.expired).count

            let resolvedCodeDir: String? = {
                let dir = app.codeDir ?? app.integrationDir ?? app.repo
                return dir.isEmpty ? nil : dir
            }()
            let hasCode: Bool = {
                guard let dir = resolvedCodeDir else { return false }
                return pathExists("\(codeRoot)/\(dir)")
            }()
            let resolvedPublish: String? = {
                guard let explicit = app.publishScript, !explicit.isEmpty else { return nil }
                return explicit
            }()
            let hasPublish: Bool = {
                guard let script = resolvedPublish else { return false }
                return pathExists("\(appsRoot)/\(script)")
            }()

            let state: CellState
            if viewOutput == nil {
                state = .unproven
            } else if !failingOrConflictingPRs.isEmpty || expired > 0 {
                state = .missing
            } else if !blockedPRs.isEmpty || dirty > 0 {
                state = .drift
            } else {
                state = .passed
            }

            return FleetAppStatus(
                acronym: app.acronym,
                repo: app.repo,
                title: app.title,
                kind: app.kindLabel,
                visibility: app.visibility ?? "unknown",
                codeDir: resolvedCodeDir,
                publishScript: resolvedPublish,
                hasCodeCheckout: hasCode,
                hasPublishScript: hasPublish,
                activeLanes: repoLanes.count,
                dirtyLanes: dirty,
                openPullRequests: repoPRs.count,
                blockedPullRequests: blockedPRs.count,
                activeClaims: appClaims.count,
                expiredClaims: expired,
                state: state
            )
        }

        // Group live seats by their coding app slug in `seatApps.bySeat`.
        let bySeat = document.seatApps?.bySeat ?? [:]
        var orderedSlugs: [String] = []
        var seatsBySlug: [String: [String]] = [:]
        for seat in document.liveSeats {
            let slug = bySeat[seat.tag] ?? seat.tag.lowercased()
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

        let macRows = toolsReading?.hosts.first(where: { $0.host == "mac" })?.allRows ?? []
        let codingStatuses: [CodingAppStatus] = orderedSlugs.map { slug in
            let meta = codingAppMetadata[slug] ?? (title: slug, cli: nil)
            let seatTags = seatsBySlug[slug] ?? []
            let seatSet = Set(seatTags)
            let cliRow = meta.cli.flatMap { cliName in
                macRows.first(where: { $0.check == "tool:\(cliName)" })
            }
            let appLanes = lanes.filter { seatSet.contains($0.seat) }.count
            let appPRs = prs.filter { seatSet.contains($0.seat) }.count
            let appClaims = claims.filter { seatSet.contains($0.seat) }.count

            return CodingAppStatus(
                appSlug: slug,
                displayName: meta.title,
                seats: seatTags,
                cliTool: meta.cli,
                cliVersion: cliRow?.installed,
                cliState: cliRow?.rowState,
                activeLanes: appLanes,
                openPullRequests: appPRs,
                activeClaims: appClaims
            )
        }

        return FleetRegistrySummary(
            zulipChannel: document.zulipChannel ?? "work-in-progress",
            apps: appStatuses,
            codingApps: codingStatuses,
            problem: nil
        )
    }
}
