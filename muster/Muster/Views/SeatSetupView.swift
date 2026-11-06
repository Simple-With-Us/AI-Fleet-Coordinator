import SwiftUI

/// The Seat Setup panel:  what one seat's setup looks like on the Mac, for any platform.
///
/// A seat is chosen at the top and the panel shows the same things for every one of them:
/// what needs a look, the engine's answer for each of the six config kinds it checks
/// (instructions, hooks, MCP, skills, sub-agents and permissions), and under each kind the
/// rules behind that answer with the file each rule is about, read as it is on this Mac.
/// Which file belongs to which seat comes from the engine's manifest, so a platform with a
/// different layout needs nothing here.
///
/// It covers the Mac only, it never edits, and a file that is absent or cannot be read says
/// so and never reads as empty.  MiniMax keeps the extra sections it already had (plugins,
/// config.yaml, channels and agents) under its own seat, because those are files only its
/// runtime has.
struct SeatSetupView: View {
    @EnvironmentObject private var store: FleetStore
    /// The seat chosen last, so the panel opens where it was left.
    @AppStorage("musterSeatSetupSeat") private var storedSeat = ""
    /// The artifacts each kind's rules are about, for the seat they were read for.
    @State private var artifacts: [ConfigKind: FleetStore.CellArtifacts] = [:]
    @State private var artifactsSeat: String?
    @State private var open: Set<ConfigKind> = []

    /// MiniMax's seat tag, the one seat with extra files of its own to show.
    private static let miniMaxSeat = "MM"

    private var seat: String? {
        SeatSetup.resolvedSeat(stored: storedSeat, roster: store.roster)
    }

    private var cells: [GridCell] {
        guard let seat else { return [] }
        return ConfigKind.columnOrder.compactMap { store.cell(seat: seat, kind: $0) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                chooser
                if let seat {
                    attention(seat)
                    answers(seat)
                    ForEach(ConfigKind.columnOrder, id: \.self) { kind in
                        kindPanel(kind, seat: seat)
                    }
                    if seat == Self.miniMaxSeat {
                        MiniMaxDetailView(embedded: true)
                    }
                } else {
                    EmptyNote(text: "The engine has no seats to show yet.  Run it from the Compliance Grid or the Overview.")
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        // The files are read again when the seat changes and when the engine finishes a run,
        // so a panel opened before the engine answered fills in when it does.
        .task(id: "\(seat ?? "")/\(String(describing: store.phase))") { await loadArtifacts() }
    }

    // MARK: - the seat

    private var chooser: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Picker("Seat", selection: seatBinding) {
                ForEach(store.roster, id: \.self) { tag in
                    Text(tag).tag(tag)
                }
            }
            .frame(maxWidth: 220)
            .disabled(store.roster.isEmpty)
            if let platform = SeatSetup.platform(of: cells) {
                Text("runs on \(platform)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            Button("Run The Engine Again") { store.refresh() }
                .disabled(store.isRunning)
                .help("Checks every seat's setup on the Mac again.")
        }
    }

    private var seatBinding: Binding<String> {
        Binding(
            get: { seat ?? "" },
            set: { storedSeat = $0 }
        )
    }

    private var engineLine: String {
        switch store.phase {
        case .idle:
            return "The engine has not run yet."
        case .running:
            return "The engine is running."
        case .loaded:
            guard let at = store.generatedAt else { return "The engine answered." }
            return "The engine last answered \(FleetTime.ageText(since: at)) ago."
        case .failed(let reason):
            return "The last engine run failed:  \(reason)"
        }
    }

    // MARK: - what needs a look

    private func attention(_ seat: String) -> some View {
        let items = SeatSetup.attention(for: cells)
        return SectionPanel(title: "What Needs A Look", subtitle: "Seat \(seat), on the Mac") {
            VStack(alignment: .leading, spacing: 8) {
                if store.phase != .loaded && cells.isEmpty {
                    Text("The engine has not answered for this seat yet.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if items.isEmpty {
                    Label {
                        Text("Nothing the engine checked on this seat is missing, drifting, unproven or unreadable.")
                            .font(.callout)
                    } icon: {
                        Image(systemName: "checkmark.circle")
                            .accessibilityHidden(true)
                    }
                } else {
                    ForEach(items, id: \.kind) { item in
                        HStack(alignment: .top, spacing: 8) {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                                .accessibilityHidden(true)
                            Text("\(item.kind.title), \(item.state):  \(item.text)")
                                .font(.callout)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                    }
                }
                Text(engineLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - the engine's answers

    private func answers(_ seat: String) -> some View {
        SectionPanel(title: "The Engine's Answers", subtitle: "Seat \(seat) on the Mac, one badge per config kind") {
            if cells.isEmpty {
                EmptyNote(text: "The engine has no answer for seat \(seat) yet.")
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

    // MARK: - one kind, with its rules and files

    private func kindPanel(_ kind: ConfigKind, seat: String) -> some View {
        let cell = store.cell(seat: seat, kind: kind)
        return FoldedPanel(
            title: kind.title,
            summary: SeatSetup.summary(for: cell),
            expanded: openBinding(kind)
        ) {
            kindDetail(kind, seat: seat, cell: cell)
        }
    }

    @ViewBuilder
    private func kindDetail(_ kind: ConfigKind, seat: String, cell: GridCell?) -> some View {
        if cell == nil {
            EmptyNote(text: SeatSetup.summary(for: nil))
        } else if let found = artifacts[kind], artifactsSeat == seat {
            if found.rules.isEmpty {
                EmptyNote(text: found.coverReason ?? "No rule covers this seat and kind.")
            } else {
                let sources = store.ruleSources(seat: seat, kind: kind)
                VStack(alignment: .leading, spacing: 20) {
                    ForEach(Array(found.rules.enumerated()), id: \.offset) { index, rule in
                        RuleArtifactView(
                            rule: rule,
                            view: found.views.indices.contains(index) ? found.views[index] : nil,
                            source: sources[rule.ruleId]
                        )
                        if index < found.rules.count - 1 { Divider() }
                    }
                }
            }
        } else {
            HStack(spacing: 8) {
                ProgressView()
                    .controlSize(.small)
                Text("Reading the files on the Mac.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func openBinding(_ kind: ConfigKind) -> Binding<Bool> {
        Binding(
            get: { open.contains(kind) },
            set: { shown in
                if shown { open.insert(kind) } else { open.remove(kind) }
            }
        )
    }

    // MARK: - reading

    private func loadArtifacts() async {
        guard let seat else {
            artifacts = [:]
            artifactsSeat = nil
            return
        }
        let wanted = ConfigKind.columnOrder.compactMap { store.cell(seat: seat, kind: $0) }
        let store = store
        let loaded = await Task.detached(priority: .userInitiated) {
            var byKind: [ConfigKind: FleetStore.CellArtifacts] = [:]
            for cell in wanted { byKind[cell.kind] = store.artifacts(for: cell) }
            return byKind
        }.value
        guard !Task.isCancelled else { return }
        artifacts = loaded
        artifactsSeat = seat
    }
}
