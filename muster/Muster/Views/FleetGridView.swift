import AppKit
import Combine
import SwiftUI

extension CellState {
    /// The tint the badge uses.
    ///
    /// Unknown is red on purpose:  a cell the app could not read is not a cell that is
    /// merely quiet, and a quiet-looking unknown is how an "installed but not proved"
    /// seat passes unnoticed.
    var tint: Color {
        switch self {
        case .passed: return .green
        case .drift: return .orange
        case .missing: return .red
        case .unproven: return .indigo
        case .unsupported: return .gray
        case .unknown: return .red
        case .covered: return .secondary
        }
    }

    /// The background the badge uses.
    var background: Color { tint.opacity(0.13) }

    /// The border the badge uses.
    var border: Color { tint.opacity(0.35) }
}

/// One cell's badge:  the state, the proof level and the age of the observation.
struct CellBadge: View {
    let cell: GridCell
    /// The age the cell carries,  when it is the exception and not the run's own time.
    ///
    /// Every rule in a run is checked at the same moment,  so ninety cells showing the
    /// same age is noise and hides the one that matters.  A cell shows its age only
    /// when it was checked apart from the rest,  which is a rule served from the cache
    /// and not looked at again.
    var age: String?
    var width: CGFloat = 108
    /// True when the cell's results are past the time limit the engine set,  so the badge
    /// is dimmed and never reads as current.
    var pastLimit: Bool = false

    var body: some View {
        HStack(spacing: 5) {
            // A symbol as well as a colour,  so a state never rests on its colour alone.
            Image(systemName: cell.gridSymbol)
                .font(.system(size: 8, weight: .semibold))
                .foregroundStyle(cell.state.tint)
                .accessibilityHidden(true)
            // The grid's one spelling of the state (GridPresentation),  in full.
            Text(cell.gridWord)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Spacer(minLength: 0)
            // The proof the cell REACHED, never the proof its rules required.
            Text(cell.proofBadgeText)
                .font(.system(size: 9, weight: .medium, design: .monospaced))
                .foregroundStyle(.secondary)
            if let age {
                Text(age)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(Color.orange)
                    .help("This rule was checked apart from the rest of the run.")
            }
        }
        .padding(.horizontal, 7)
        .frame(width: width, height: 30)
        .background(cell.state.background, in: RoundedRectangle(cornerRadius: 7))
        .overlay {
            RoundedRectangle(cornerRadius: 7)
                .strokeBorder(cell.borderColor(age != nil), lineWidth: 1)
        }
        .opacity(pastLimit ? 0.45 : 1)
        .contentShape(Rectangle())
    }
}

extension GridCell {
    /// The border the badge uses,  which is stronger on an exception.
    func borderColor(_ isException: Bool) -> Color {
        isException ? Color.orange.opacity(0.7) : state.border
    }
}

/// The seat by config grid:  rows are seats, columns are the six config kinds.
///
/// The detail lives in one place,  the panel under the grid.  There was a popup as
/// well once,  and two surfaces for one cell is a trap:  closing the popup put the
/// panel back to the previous cell,  so the panel and the popup could disagree about
/// what was selected and neither was the place the operator was reading.
struct FleetGridView: View {
    @EnvironmentObject private var store: FleetStore
    /// The cell the panel is open on, by seat and kind and never by copy:  the panel looks
    /// the cell up in the store on every render, so a run that lands while it is open shows
    /// the new cell and not the one that was clicked.
    @State private var selection: GridSelection?
    /// Artifacts for the selected cell, read off the main thread by `.task`.
    @State private var loadedArtifacts: FleetStore.CellArtifacts?
    /// The cell and run the loaded artifacts belong to (`GridSelection.readKey`).
    @State private var loadedArtifactsKey: String?
    /// When the Mac last woke, so the check time and the time limits are measured again
    /// at once rather than at the timeline's next tick.
    @State private var wokeAt: Date?
    @State private var gridFilter: GridStateFilter = .all
    /// The filter an Overview card asked for,  held by the window that asked.  The grid
    /// takes it once when it appears and clears it,  so it never outlives that one visit.
    @Binding private var request: GridStateFilter?
    @State private var searchText: String = ""
    /// The height of the detail panel,  which the operator drags and which survives
    /// the next launch.
    @AppStorage("muster.detailPanelHeight") private var panelHeight: Double = 420

    private let seatWidth: CGFloat = 132
    private let columnWidth: CGFloat = 108
    private let minPanelHeight: CGFloat = 170
    private let maxPanelHeight: CGFloat = 900

    init(request: Binding<GridStateFilter?> = .constant(nil)) {
        _request = request
    }

    private var visibleRoster: [String] {
        let base = store.roster.filter { seat in
            gridFilter.includes(ConfigKind.columnOrder.compactMap { store.cell(seat: seat, kind: $0) },
                                commonCheckedAt: store.commonCheckedAt)
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return base }
        return base.filter { $0.lowercased().contains(query) }
    }

    /// The cell the panel shows, looked up in the store now.
    private var selectedCell: GridCell? {
        guard let selection else { return nil }
        return store.cell(seat: selection.seat, kind: selection.kind)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                // The check time and the time limits are measured against the clock, so the
                // grid is drawn inside a timeline:  an open grid ages, and a grid left open
                // through a sleep reads its results as past their limit when it wakes.
                TimelineView(.everyMinute) { context in
                    let now = GridPresentation.effectiveNow(timelineDate: context.date, wokeAt: wokeAt)
                    VStack(alignment: .leading, spacing: 0) {
                        scopeBlock
                            .padding(.bottom, 12)
                        filterBar
                            .padding(.bottom, 12)
                        if let banner = GridPresentation.pastLimitBanner(cells: store.cells, now: now) {
                            pastLimitBanner(banner)
                                .padding(.bottom, 10)
                        }
                        header(now: now)
                        if visibleRoster.isEmpty {
                            emptyState
                        } else {
                            ForEach(visibleRoster, id: \.self) { seat in
                                row(for: seat, now: now)
                            }
                        }
                        legend
                            .padding(.top, 18)
                        if let manifest = store.manifest {
                            complianceSection(manifest)
                                .padding(.top, 22)
                        }
                    }
                    .padding(.horizontal, 22)
                    .padding(.vertical, 18)
                }
            }
            if let selection, let cell = selectedCell {
                let readKey = selection.readKey(generatedAt: store.generatedAt)
                PanelResizeHandle(height: panelHeightBinding)
                Group {
                    if let artifacts = loadedArtifacts, loadedArtifactsKey == readKey {
                        ConfigFilePanel(
                            cell: cell,
                            artifacts: artifacts,
                            sources: store.ruleSources(seat: cell.seat, kind: cell.kind),
                            asOf: GridPresentation.asOfLine(generatedAt: store.generatedAt),
                            onClose: { self.selection = nil }
                        )
                    } else {
                        ProgressView("Reading the artifact\u{2026}")
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
                .frame(height: CGFloat(panelHeight))
                // Keyed on the cell AND the run, so the read runs again when a check lands
                // and the panel never shows the files of a run that is gone.
                .task(id: readKey) {
                    loadedArtifacts = nil
                    loadedArtifactsKey = nil
                    let loaded = await Task.detached(priority: .userInitiated) {
                        store.artifacts(for: cell)
                    }.value
                    guard !Task.isCancelled else { return }
                    loadedArtifacts = loaded
                    loadedArtifactsKey = readKey
                }
            }
        }
        .onAppear {
            if let requested = request {
                gridFilter = requested
                request = nil
            }
        }
        // A filter that hides the open cell's row closes the panel, so the panel never
        // explains a cell the grid is not showing.  A run in flight rebuilds every cell as
        // unknown for a moment, so the panel waits for the run to land before it decides.
        .onChange(of: visibleRoster) { _, roster in
            closePanelIfGone(roster: roster)
        }
        .onChange(of: store.isRunning) { _, running in
            if !running { closePanelIfGone(roster: visibleRoster) }
        }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)) { _ in
            wokeAt = Date()
        }
        // The wake time is for the first tick after a wake only:  once the timeline has
        // ticked past it, it is cleared, so it never holds the grid's clock.
        .task(id: wokeAt) {
            guard let woke = wokeAt else { return }
            try? await Task.sleep(nanoseconds: 61_000_000_000)
            guard !Task.isCancelled, wokeAt == woke else { return }
            wokeAt = nil
        }
    }

    /// Close the panel when its cell is gone or its row is hidden, but never in the middle
    /// of a run (`GridSelection.shouldClose`).
    private func closePanelIfGone(roster: [String]) {
        guard let selection else { return }
        if selection.shouldClose(visibleRoster: roster, isRunning: store.isRunning, cellExists: selectedCell != nil) {
            self.selection = nil
        }
    }

    /// What the grid checks and what it does not, above everything else.
    private var scopeBlock: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(GridPresentation.scopeHeading, systemImage: FleetHost.mac.systemImage)
                .font(.headline)
            Text(GridPresentation.scopeExplanation)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The banner over the grid when results are past their time limit.
    private func pastLimitBanner(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "clock.badge.exclamationmark")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(text)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
    }

    /// What the grid shows when the filter leaves no seat, with the way back.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(GridPresentation.emptyFilterText(
                filter: gridFilter,
                search: searchText,
                rosterCount: store.roster.count,
                checkedAt: store.commonCheckedAt
            ))
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            if gridFilter != .all || !searchText.isEmpty {
                Button("Clear Filters") {
                    gridFilter = .all
                    searchText = ""
                }
                .help("Shows every seat again.")
            }
        }
        .padding(.vertical, 14)
    }

    private var filterBar: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                Picker("Seats", selection: $gridFilter) {
                    ForEach(GridStateFilter.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(maxWidth: 560)
                TextField("Filter seats by tag…", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: 200)
                Spacer()
                Text("\(visibleRoster.count) of \(store.roster.count) seat(s)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let note = gridFilter.cannotNarrowNote(commonCheckedAt: store.commonCheckedAt) {
                Text(note)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// The manifest's fleet-wide rules, grouped by the place each is about, each rule once.
    ///
    /// The app does not read these rules' results, so no rule here wears a green:  a rule
    /// says only whether its check exists, in neutral words, and a rule with no check yet
    /// says that instead.
    private func complianceSection(_ manifest: SeatManifest) -> some View {
        let rules = manifest.allComplianceRules
        let byID = Dictionary(rules.map { ($0.id, $0) }, uniquingKeysWith: { first, _ in first })
        return VStack(alignment: .leading, spacing: 12) {
            Divider()
            Text(GridPresentation.complianceHeading)
                .font(.headline)
            Text(GridPresentation.complianceExplanation)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(GridPresentation.complianceGroups(rules)) { group in
                VStack(alignment: .leading, spacing: 6) {
                    Text(group.heading)
                        .font(.subheadline.weight(.semibold))
                    ForEach(group.ruleIDs, id: \.self) { ruleID in
                        if let rule = byID[ruleID] {
                            complianceRow(rule)
                        }
                    }
                }
            }
        }
    }

    /// One fleet-wide rule:  its id, its owner, where it applies and whether its check exists.
    private func complianceRow(_ rule: ComplianceRule) -> some View {
        let status = GridPresentation.complianceStatus(rule)
        return VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text(rule.id)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                if let owner = rule.ownerSeat, !owner.isEmpty {
                    Text("owner: \(owner)")
                        .font(.system(size: 9, weight: .semibold, design: .monospaced))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.14), in: Capsule())
                }
                ForEach(rule.scope ?? [], id: \.self) { scope in
                    Text(GridPresentation.scopeWords(scope))
                        .font(.system(size: 9, weight: .semibold))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Color.accentColor.opacity(0.12), in: Capsule())
                }
                Text(status.text)
                    .font(.system(size: 9, weight: .semibold, design: .monospaced))
                    .foregroundStyle(status.isBuilt ? Color.secondary : Color.orange)
                Spacer()
            }
            if let reason = rule.implementationReason, !reason.isEmpty {
                Text(reason)
                    .font(.system(size: 10))
                    .foregroundStyle(status.isBuilt ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let target = rule.check?.target, !target.isEmpty {
                Text("Target:  \(target)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let checkType = rule.check?.type, !checkType.isEmpty {
                Text("Check type:  \(checkType)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 2)
    }

    /// The panel's height,  kept inside its bounds.
    private var panelHeightBinding: Binding<CGFloat> {
        Binding(
            get: { CGFloat(panelHeight) },
            set: { newValue in
                panelHeight = Double(min(max(newValue, minPanelHeight), maxPanelHeight))
            }
        )
    }

    private func header(now: Date) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("Seat")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: seatWidth, alignment: .leading)
                ForEach(ConfigKind.columnOrder, id: \.self) { kind in
                    Text(kind.title)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: columnWidth, alignment: .leading)
                }
                Spacer()
            }
            if let checked = GridPresentation.checkedLine(commonCheckedAt: store.commonCheckedAt, now: now) {
                // The run checks every rule at one moment,  so the time goes here once
                // instead of in ninety cells.  The age is measured against the timeline's
                // clock,  so it moves while the grid is open.
                HStack(spacing: 8) {
                    Spacer().frame(width: seatWidth)
                    Text(checked)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .help("When the engine checked the rules this run.\u{00A0} A cell shows its own age only when it was checked apart from the rest.")
                    Spacer()
                }
            }
        }
        .padding(.bottom, 8)
    }

    /// The age a cell carries,  when it was checked apart from the rest of the run.
    private func exceptionAge(for cell: GridCell) -> String? {
        guard
            let common = store.commonCheckedAt,
            let checkedAt = cell.checkedAt,
            abs(checkedAt.timeIntervalSince(common)) > 60
        else { return nil }
        return cell.ageText
    }

    /// The legend,  so a badge is never a puzzle.
    ///
    /// The engine's own levels are the fleet's vocabulary,  and an operator who has
    /// to ask what `L0 2m` means will not trust the grid on anything else either.
    private var legend: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("What a state says")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            // Every word a badge can show, with its colour, its symbol, what it means and
            // whether it needs anything done.
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 230), alignment: .topLeading)], alignment: .leading, spacing: 10) {
                ForEach(GridPresentation.stateLegend) { entry in
                    let tint = entry.state?.tint ?? Color.secondary
                    VStack(alignment: .leading, spacing: 2) {
                        Label {
                            Text(entry.word)
                                .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        } icon: {
                            Image(systemName: entry.symbol)
                                .foregroundStyle(tint)
                        }
                        Text(entry.meaning)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(entry.action)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityElement(children: .combine)
                }
            }
            Text("What the proof beside a state says")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.top, 6)
            Text("\(PassMeaning.everywhere)\u{00A0} The proof beside a state is what was reached, and a dash means no proof was reached.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .top, spacing: 22) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("L0").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("Reached:  the config was read and holds what the rule expects.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("L1").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("Reached:  the hook was sent a write and it denied it.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("L2").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("Reached:  a headless canary showed the platform loads the hook.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("1/2").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("Partly proven:  one of the cell's two rules was proven, and the tooltip names the one that was not.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("2m").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("The age of a result checked apart from the rest of the run.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One seat:  its tag, where its config lives, and its six cells.
    ///
    /// A seat that does not run on the Mac says so on its row, in the manifest's own
    /// sentence, rather than only in a tooltip.
    private func row(for seat: String, now: Date) -> some View {
        let location = GridPresentation.SeatLocation.of(seat: seat, manifest: store.manifest)
        return VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(seat)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    Text(location.chip)
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(location == .onTheMac ? Color.secondary : Color.orange)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.12), in: Capsule())
                        .help(location.help)
                }
                .frame(width: seatWidth, alignment: .leading)
                ForEach(ConfigKind.columnOrder, id: \.self) { kind in
                    if let cell = store.cell(seat: seat, kind: kind) {
                        let pastLimit = GridPresentation.isPastLimit(cell, now: now)
                        Button {
                            selection = GridSelection(cell)
                        } label: {
                            CellBadge(cell: cell, age: exceptionAge(for: cell), width: columnWidth, pastLimit: pastLimit)
                        }
                        .buttonStyle(.plain)
                        .help(GridPresentation.tooltip(for: cell, pastLimit: pastLimit))
                        .accessibilityLabel("\(seat), \(cell.kind.title):  \(cell.gridWord)")
                    } else {
                        Color.clear.frame(width: columnWidth, height: 30)
                    }
                }
            }
            if let note = location.note {
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .padding(.leading, seatWidth + 8)
                    .help(note)
            }
        }
        .padding(.vertical, 3)
    }
}

/// The drag bar between the grid and the detail panel.
///
/// Dragging it up makes the panel taller and dragging it down makes it shorter,  so
/// the operator can give the detail as much room as the cell in front of them needs.
struct PanelResizeHandle: View {
    @Binding var height: CGFloat
    @State private var startHeight: CGFloat = 0
    @State private var hovering = false

    var body: some View {
        ZStack {
            Rectangle()
                .fill(Color.clear)
                .frame(height: 9)
            Capsule()
                .fill(Color.secondary.opacity(hovering ? 0.65 : 0.3))
                .frame(width: 44, height: 3)
        }
        .contentShape(Rectangle())
        .onHover { inside in
            if startHeight == 0 { hovering = inside }
        }
        .gesture(
            DragGesture(minimumDistance: 1)
                .onChanged { value in
                    if startHeight == 0 { startHeight = height }
                    // Dragging up grows the panel,  so the change is the opposite sign.
                    height = startHeight - value.translation.height
                }
                .onEnded { _ in
                    startHeight = 0
                    hovering = false
                }
        )
        .accessibilityLabel("Resize the detail panel")
        .accessibilityHint("Drag up to make the panel taller")
    }
}

/// One label and value line.
struct ProofLine: View {
    let label: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 130, alignment: .leading)
            Text(value)
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The `expected` or `observed` object as a table of names and values.
struct ValueTable: View {
    let title: String
    let values: [String: JSONValue]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            if values.isEmpty {
                Text("—")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.tertiary)
            } else {
                ForEach(values.keys.sorted(), id: \.self) { key in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(key)
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .frame(width: 110, alignment: .leading)
                        Text(values[key]?.displayText ?? "—")
                            .font(.system(size: 10, design: .monospaced))
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
