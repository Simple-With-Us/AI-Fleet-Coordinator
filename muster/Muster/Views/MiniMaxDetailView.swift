import SwiftUI

/// The MiniMax panel:  what the MiniMax surface looks like on this Mac.
///
/// The panels are ordered the way MM asked for them in the AFC-354 topic:  hooks first,
/// because that is where the problems were.  Everything here is observed surface;  the
/// verdicts come from the engine, and the six badges at the top are the engine's own
/// answers for this seat.
struct MiniMaxDetailView: View {
    @EnvironmentObject private var store: FleetStore
    @State private var surface: MiniMaxSurface?

    private let seat = "MM"

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                verdicts
                hooks
                plugins
                settings
                channels
                agents
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        // The panel reads the disk and the engine, and the engine run finishes after
        // the window appears.  Reading once at appear time would leave the proof column
        // empty on every launch, which is the one thing that column exists to prevent.
        .task(id: store.phase) { reload() }
    }

    private func reload() {
        let live = Set(store.roster)
        let home = FileManager.default.homeDirectoryForCurrentUser
        // The MiniMax runtime names its own seat agent `mavis`, while the fleet calls
        // that seat `MM`, so a route that names `mavis` names a live seat.
        var aliases: [String: String] = [:]
        if let runtimeAgent = MiniMaxReader.seatRuntimeAgentName(home: home) {
            aliases[runtimeAgent] = seat
        }
        surface = MiniMaxReader.read(
            home: home,
            liveSeats: live,
            seatAliases: aliases,
            proofs: store.hookProofs(seat: seat)
        )
    }

    // MARK: - the engine's own answers for this seat

    private var verdicts: some View {
        SectionPanel(title: "Verdicts from the engine", subtitle: "Seat \(seat), every config kind") {
            HStack(spacing: 10) {
                ForEach(ConfigKind.columnOrder, id: \.self) { kind in
                    if let cell = store.cell(seat: seat, kind: kind) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(kind.title)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            CellBadge(cell: cell, width: 124)
                        }
                    }
                }
            }
            .padding(.top, 4)
        }
    }

    // MARK: - hooks, first

    private var hooks: some View {
        SectionPanel(
            title: "Hooks",
            subtitle: "Per plugin, event and matcher:  registered, command resolves, script exists, last proof"
        ) {
            let hooks = surface?.hooks ?? []
            if hooks.isEmpty {
                EmptyNote(text: "No plugin on this Mac registers a hook.")
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    GridRowHeader(labels: ["Plugin", "Event", "Matcher", "Command", "Script", "Timeout", "Proof"])
                    ForEach(hooks) { hook in
                        GridRow(cells: [
                            GridCellText(value: hook.plugin),
                            GridCellText(value: hook.event),
                            GridCellText(value: hook.matcher),
                            GridCellText(value: hook.command),
                            GridCellText(
                                value: hook.scriptPath == nil
                                    ? "—"
                                    : (hook.scriptExists ? "present" : "missing"),
                                emphasised: hook.scriptPath != nil && !hook.scriptExists
                            ),
                            GridCellText(value: hook.timeout.map { "\($0)s" } ?? "—"),
                            GridCellText(
                                value: proofText(hook.proof),
                                emphasised: hook.proof == nil || hook.proof?.achieved == nil
                            ),
                        ])
                    }
                }
            }
        }
    }

    /// What the engine proved about one hook.
    ///
    /// A hook with no proof at all reads as unproved rather than as fine:  "installed"
    /// and "protecting" are different states, and a panel that cannot tell them apart
    /// is the thing MM said this project must not build.
    private func proofText(_ proof: MiniMaxSurface.HookProof?) -> String {
        guard let proof else { return "not proved" }
        let achieved = proof.achieved ?? "none"
        let age = FleetTime.ageText(since: proof.checkedAt)
        return "\(proof.state) \(proof.required)→\(achieved), \(age) ago"
    }

    // MARK: - plugins

    private var plugins: some View {
        SectionPanel(title: "Plugins", subtitle: "Manifest validity, events registered, commands declared") {
            let plugins = surface?.plugins ?? []
            if plugins.isEmpty {
                EmptyNote(text: "No plugin is installed under ~/.minimax/plugins.")
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    GridRowHeader(labels: ["Plugin", "Hook manifest", "Events", "Commands"])
                    ForEach(plugins) { plugin in
                        GridRow(cells: [
                            GridCellText(value: plugin.name),
                            GridCellText(
                                value: plugin.hasHookManifest
                                    ? (plugin.hookManifestValid ? "valid" : "unreadable")
                                    : "none",
                                emphasised: plugin.hasHookManifest && !plugin.hookManifestValid
                            ),
                            GridCellText(value: plugin.events.isEmpty ? "—" : plugin.events.joined(separator: ", ")),
                            GridCellText(value: "\(plugin.commandCount)"),
                        ])
                    }
                }
            }
        }
    }

    // MARK: - config.yaml

    private var settings: some View {
        SectionPanel(title: "config.yaml", subtitle: "The allowed keys only;  the file also holds a credential") {
            let settings = surface?.settings ?? []
            if settings.isEmpty {
                EmptyNote(text: "config.yaml could not be read.")
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(settings) { setting in
                        ProofLine(label: setting.key, value: setting.value)
                    }
                }
            }
        }
    }

    // MARK: - channels

    private var channels: some View {
        SectionPanel(title: "Channels", subtitle: "Bindings, routes and the owner each file declares") {
            let channels = surface?.channels ?? []
            VStack(alignment: .leading, spacing: 8) {
                GridRowHeader(labels: ["File", "Schema", "Detail"])
                ForEach(channels) { channel in
                    GridRow(cells: [
                        GridCellText(value: channel.file),
                        GridCellText(value: channel.schemaVersion ?? "—"),
                        GridCellText(value: channel.detail),
                    ])
                }
                let routes = channels.flatMap { $0.routes }
                if !routes.isEmpty {
                    Divider().padding(.vertical, 4)
                    Text("Routes")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(routes) { route in
                        ProofLine(
                            label: route.channel,
                            value: route.agentId + "  (" + routeTargetText(route.target) + ")"
                        )
                    }
                }
            }
        }
    }

    /// What a route's agent id turned out to name.
    private func routeTargetText(_ target: MiniMaxSurface.Route.Target) -> String {
        switch target {
        case .fleetSeat(let seat):
            return "seat \(seat), live"
        case .runtimeAgent:
            return "a runtime agent, live"
        case .unknown:
            return "not a live seat"
        }
    }

    // MARK: - agents

    private var agents: some View {
        SectionPanel(title: "Agents", subtitle: "The roster and how stale each definition is") {
            let agents = surface?.agents ?? []
            if agents.isEmpty {
                EmptyNote(text: "No sub-agent is installed under ~/.minimax/agents.")
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    GridRowHeader(labels: ["Agent", "Prompt", "Files", "Newest change"])
                    ForEach(agents) { agent in
                        GridRow(cells: [
                            GridCellText(
                                // The runtime calls this seat's own agent `mavis` and the fleet
                                // calls that seat `MM`, so the row names both of them.
                                value: agent.ownsSeatMemory ? "\(agent.name) (\(seat), this seat)" : agent.name
                            ),
                            GridCellText(
                                value: agent.hasPrompt ? "agent.md" : "none",
                                emphasised: !agent.hasPrompt
                            ),
                            GridCellText(value: "\(agent.files.count)"),
                            GridCellText(
                                value: agent.newestChange.map { FleetTime.ageText(since: $0) + " ago" } ?? "—"
                            ),
                        ])
                    }
                }
            }
        }
    }
}

/// One titled panel.
struct SectionPanel<Content: View>: View {
    let title: String
    var subtitle: String = ""
    let content: Content

    init(title: String, subtitle: String = "", @ViewBuilder content: () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.headline)
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(.quaternary, lineWidth: 1)
        }
    }
}

/// One cell of a simple table.
struct GridCellText: View {
    let value: String
    var emphasised: Bool = false

    var body: some View {
        Text(value)
            .font(.system(size: 11, design: .monospaced).weight(emphasised ? .semibold : .regular))
            .foregroundStyle(emphasised ? Color.primary : Color.secondary)
    }
}

/// A header row of a simple table.
struct GridRowHeader: View {
    let labels: [String]

    var body: some View {
        HStack(spacing: 12) {
            ForEach(labels, id: \.self) { label in
                Text(label)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

/// One row of a simple table.
struct GridRow: View {
    let cells: [GridCellText]

    var body: some View {
        HStack(spacing: 12) {
            ForEach(cells.indices, id: \.self) { index in
                cells[index]
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }
        }
        .padding(.vertical, 2)
    }
}

/// A note where a table would be.
struct EmptyNote: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.callout)
            .foregroundStyle(.secondary)
    }
}
