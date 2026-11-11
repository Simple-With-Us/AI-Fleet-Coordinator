import SwiftUI

/// MiniMax's own files:  what the MiniMax install on the Mac looks like, for seat MM.
///
/// This is the part of Seat Setup that only MiniMax has (its plugins, config.yaml, channels
/// and agents), shown under its seat there.  It covers the Mac only;  the server's MiniMax
/// install is not read here, and the panel says so.  The top answers "does anything need a look?"
/// in plain words, with where the reading came from and how old it is.  The engine's
/// own answers come next, then the hooks with their proof, because that is where the
/// problems were (AFC-354).  Plugins, config.yaml, channels and agents are folded
/// below, each with a one-line summary.
struct MiniMaxDetailView: View {
    @EnvironmentObject private var store: FleetStore
    /// The last reading of the disk, or nil before the first one finishes.
    ///
    /// Nil is "not read yet", never "nothing installed":  the sections that would say
    /// a list is empty are not drawn until a reading exists.
    @State private var surface: MiniMaxSurface?
    @State private var reading = false
    /// The number of the latest reading, so an older one cannot overwrite a newer one.
    @State private var generation = 0
    @State private var showPlugins = false
    @State private var showSettings = false
    @State private var showChannels = false
    @State private var showAgents = false

    /// True when the view sits inside the Seat Setup panel for MiniMax's seat.  That panel
    /// already shows the engine's answers for any seat and has its own scrolling, so an
    /// embedded view leaves out its own copy of the answers and its own scroll view.
    var embedded = false

    private let seat = "MM"

    var body: some View {
        Group {
            if embedded {
                content
            } else {
                ScrollView {
                    content
                        .padding(.horizontal, 22)
                        .padding(.vertical, 18)
                }
            }
        }
        // The panel reads the disk and the engine, and the engine run finishes after
        // the window appears.  Reading once at appear time would leave the proof column
        // empty on every launch, which is the one thing that column exists to prevent.
        .task(id: store.phase) { await reload() }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 24) {
            attention
            if !embedded { verdicts }
            if let surface, surface.installFound {
                hooks(surface)
                plugins(surface)
                settings(surface)
                channels(surface)
                agents(surface)
            }
        }
    }

    private func reload() async {
        // Each reading takes a number.  A reading that was cancelled or overtaken by a
        // later one is not allowed to clear `reading` or replace the newer surface: its
        // detached disk read still finishes, so a `defer` would unlock Read Again while
        // the newer reading is in flight.
        generation += 1
        let mine = generation
        reading = true
        let roster = Set(store.roster)
        let home = FileManager.default.homeDirectoryForCurrentUser
        // While a run checks again the panel shows the last answer, so it takes that
        // answer's proofs too:  a hook must not read "the engine has no rule" beside counts
        // that say the engine answered.
        let proofs = store.hookProofs(seat: seat, includingLastAnswer: store.isShowingLastAnswer)
        let seat = seat
        let loaded = await Task.detached(priority: .userInitiated) {
            // The MiniMax runtime names its own seat agent `mavis`, while the fleet calls
            // that seat `MM`, so a route that names `mavis` names the seat.
            var aliases: [String: String] = [:]
            if let runtimeAgent = MiniMaxReader.seatRuntimeAgentName(home: home) {
                aliases[runtimeAgent] = seat
            }
            return MiniMaxReader.read(
                home: home,
                rosterSeats: roster,
                seatAliases: aliases,
                proofs: proofs
            )
        }.value
        guard mine == generation else { return }
        reading = false
        guard !Task.isCancelled else { return }
        surface = loaded
    }

    /// The button that reads the disk again, which says when a reading is under way.
    private var readButtonTitle: String {
        reading ? "Reading" : "Read Again"
    }

    /// Whether the engine's answer is on screen:  a finished run, or the last answer the
    /// store keeps while a run checks again.  The attention list, the hook rows, the hook
    /// proofs and the engine line all take the same flag, so they never disagree.
    private var engineAnswerShown: Bool {
        GridPresentation.engineAnswerShown(phase: store.phase, showingLastAnswer: store.isShowingLastAnswer)
    }

    /// The engine's state for each config kind of this seat, in column order.
    private var engineEntries: [(kind: ConfigKind, state: CellState)] {
        ConfigKind.columnOrder.compactMap { kind -> (kind: ConfigKind, state: CellState)? in
            guard let cell = store.cell(seat: seat, kind: kind) else { return nil }
            return (kind: kind, state: cell.state)
        }
    }

    // MARK: - what needs a look

    private var attention: some View {
        SectionPanel(
            title: embedded ? "MiniMax's Own Files" : "What Needs A Look",
            subtitle: "MiniMax, seat \(seat), on the Mac"
        ) {
            VStack(alignment: .leading, spacing: 8) {
                if let surface {
                    let items = MiniMaxAttention.items(
                        surface: surface,
                        seat: seat,
                        engine: engineEntries,
                        engineAnswered: engineAnswerShown
                    )
                    if items.isEmpty {
                        Label {
                            Text(MiniMaxCopy.allClearText(seat: seat, engine: engineEntries))
                                .font(.callout)
                        } icon: {
                            Image(systemName: "checkmark.circle")
                                .accessibilityHidden(true)
                        }
                    } else {
                        Text(MiniMaxCopy.headline(count: items.count))
                            .font(.callout.weight(.semibold))
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            HStack(alignment: .top, spacing: 8) {
                                Image(systemName: "exclamationmark.triangle.fill")
                                    .foregroundStyle(.orange)
                                    .accessibilityHidden(true)
                                Text(item)
                                    .font(.callout)
                                    .textSelection(.enabled)
                                    .fixedSize(horizontal: false, vertical: true)
                                Spacer(minLength: 0)
                            }
                        }
                    }
                    Divider().padding(.vertical, 2)
                    Text(MiniMaxCopy.readLine(surface))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    HStack(spacing: 8) {
                        ProgressView()
                            .controlSize(.small)
                        Text("Reading the MiniMax files on the Mac.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                Text(MiniMaxCopy.engineLine(
                    phase: store.phase,
                    generatedAt: store.generatedAt,
                    showingLastAnswer: store.isShowingLastAnswer
                ))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                Button(readButtonTitle) {
                    Task { await reload() }
                }
                .disabled(reading)
                .help(
                    ApplyCopy.join(
                        "Read the MiniMax files on the Mac again.",
                        "The toolbar's Refresh also runs the engine again."
                    )
                )
            }
        }
    }

    // MARK: - the engine's own answers for this seat

    private var verdicts: some View {
        SectionPanel(title: "The Engine's Answers", subtitle: "Seat \(seat) on the Mac, one badge per config kind") {
            let cells = ConfigKind.columnOrder.compactMap { store.cell(seat: seat, kind: $0) }
            if cells.isEmpty {
                EmptyNote(text: MiniMaxCopy.verdictsEmptyText(phase: store.phase, seat: seat))
            } else {
                HStack(spacing: 10) {
                    ForEach(cells) { cell in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(cell.kind.title)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            CellBadge(cell: cell, width: 124)
                        }
                    }
                }
                .padding(.top, 4)
            }
        }
    }

    // MARK: - hooks, first

    private func hooks(_ surface: MiniMaxSurface) -> some View {
        SectionPanel(
            title: "Hooks",
            subtitle: "Every hook a plugin manifest or ~/.minimax/hooks.json registers, with the engine's proof"
        ) {
            if surface.hooks.isEmpty {
                EmptyNote(text: MiniMaxCopy.hooksEmptyText(surface))
            } else {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(surface.hooks) { hook in
                        HookRow(hook: hook, engineAnswered: engineAnswerShown)
                        if hook.id != surface.hooks.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
    }

    // MARK: - plugins

    private func plugins(_ surface: MiniMaxSurface) -> some View {
        let withManifest = surface.plugins.filter { $0.hasHookManifest }.count
        let summary = surface.plugins.isEmpty
            ? MiniMaxCopy.pluginsEmptyText(surface.pluginsFolder)
            : "\(surface.plugins.count) installed, \(withManifest) with a hook manifest"
        return FoldedPanel(title: "Plugins", summary: summary, expanded: $showPlugins) {
            if surface.plugins.isEmpty {
                EmptyNote(text: MiniMaxCopy.pluginsEmptyText(surface.pluginsFolder))
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    GridRowHeader(labels: ["Plugin", "Hook manifest", "Events", "Commands"])
                    ForEach(surface.plugins) { plugin in
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

    private func settings(_ surface: MiniMaxSurface) -> some View {
        let summary = surface.settings.isEmpty
            ? MiniMaxCopy.settingsEmptyText(surface.settingsFile)
            : "\(surface.settings.count) allowed keys shown; the credential in the file is never read onto the screen"
        return FoldedPanel(title: "config.yaml", summary: summary, expanded: $showSettings) {
            if surface.settings.isEmpty {
                EmptyNote(text: MiniMaxCopy.settingsEmptyText(surface.settingsFile))
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(surface.settings) { setting in
                        ProofLine(label: setting.key, value: setting.value)
                    }
                }
            }
        }
    }

    // MARK: - channels

    private func channels(_ surface: MiniMaxSurface) -> some View {
        let present = surface.channels.filter { $0.state == .read }.count
        let routes = surface.channels.flatMap { $0.routes }
        let summary = "\(present) of \(surface.channels.count) channel files read, \(routes.count) routes"
        return FoldedPanel(title: "Channels", summary: summary, expanded: $showChannels) {
            VStack(alignment: .leading, spacing: 8) {
                GridRowHeader(labels: ["File", "Schema", "Detail"])
                ForEach(surface.channels) { channel in
                    GridRow(cells: [
                        GridCellText(value: channel.file),
                        GridCellText(value: channel.schemaVersion ?? "—"),
                        GridCellText(value: channel.detail, emphasised: channel.state == .unreadable),
                    ])
                }
                if !routes.isEmpty {
                    Divider().padding(.vertical, 4)
                    Text("Routes")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    ForEach(routes) { route in
                        ProofLine(
                            label: route.channel,
                            value: route.agentId + "  (" + MiniMaxCopy.routeTargetText(route.target) + ")"
                        )
                    }
                }
            }
        }
    }

    // MARK: - agents

    private func agents(_ surface: MiniMaxSurface) -> some View {
        let summary = surface.agents.isEmpty
            ? MiniMaxCopy.agentsEmptyText(surface.agentsFolder)
            : "\(surface.agents.count) agents, with how recently each definition changed"
        return FoldedPanel(title: "Agents", summary: summary, expanded: $showAgents) {
            if surface.agents.isEmpty {
                EmptyNote(text: MiniMaxCopy.agentsEmptyText(surface.agentsFolder))
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    GridRowHeader(labels: ["Agent", "Prompt", "Files", "Newest change"])
                    ForEach(surface.agents) { agent in
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

/// One hook, with its proof first and every other fact on its own line.
///
/// A hook used to be one row of seven narrow cells, which clipped the command and the
/// proof.  Each fact now has its own line, wraps instead of clipping, and can be
/// selected and copied.
private struct HookRow: View {
    let hook: MiniMaxSurface.Hook
    let engineAnswered: Bool

    var body: some View {
        let needsLook = MiniMaxCopy.needsLook(hook.proof)
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                // The icon carries the state in its shape as well as its colour.
                Image(systemName: needsLook ? "exclamationmark.shield" : "checkmark.shield")
                    .foregroundStyle(needsLook ? Color.orange : Color.green)
                    .accessibilityHidden(true)
                Text("\(hook.event), matcher \(hook.matcher)")
                    .font(.callout.weight(.semibold))
                Text(hook.origin)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(MiniMaxCopy.proofText(hook.proof, engineAnswered: engineAnswered))
                .font(.callout.weight(needsLook ? .semibold : .regular))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if !hook.enabled {
                ProofLine(label: "Turned on", value: "no: the file sets enabled to false")
            }
            ProofLine(label: "Hook id", value: hook.hookId.isEmpty ? "none" : hook.shownHookId)
            ProofLine(label: "Script", value: MiniMaxCopy.scriptText(hook))
            ProofLine(label: "Timeout", value: hook.timeout.map { "\($0) seconds" } ?? "none set")
            ProofLine(label: "Command", value: hook.shownCommand.isEmpty ? "none" : hook.shownCommand)
        }
    }
}

/// A titled panel that folds, with a one-line summary that shows while it is folded.
struct FoldedPanel<Content: View>: View {
    let title: String
    let summary: String
    @Binding var expanded: Bool
    let content: Content

    init(title: String, summary: String, expanded: Binding<Bool>, @ViewBuilder content: () -> Content) {
        self.title = title
        self.summary = summary
        self._expanded = expanded
        self.content = content()
    }

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            content
                .padding(.top, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.headline)
                Text(summary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
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
///
/// A cell is clipped to two lines, so the whole value is on hover and every cell can
/// be selected and copied:  nothing is shortened without a way to read the rest.
struct GridRow: View {
    let cells: [GridCellText]

    var body: some View {
        HStack(spacing: 12) {
            ForEach(cells.indices, id: \.self) { index in
                cells[index]
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                    .help(cells[index].value)
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
            .fixedSize(horizontal: false, vertical: true)
    }
}
