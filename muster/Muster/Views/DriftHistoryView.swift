import SwiftUI

/// The drift history:  every cell that turned red,  newest first.
///
/// The list is kept on disk,  so it is there after a restart,  and it is cleared
/// by hand when the owner has read it.
struct DriftHistoryView: View {
    @EnvironmentObject private var monitor: DriftMonitor
    @State private var confirmingClear = false
    @State private var searchText = ""
    @State private var stateFilter: DriftHistoryStateFilter = .all
    @State private var grouping: DriftHistoryGrouping = .newestFirst
    @State private var collapsedSeats: Set<String> = []

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                status
                if monitor.transitions.isEmpty {
                    empty
                } else {
                    table
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .confirmationDialog(
            "Clear the drift history?",
            isPresented: $confirmingClear,
            titleVisibility: .visible
        ) {
            Button("Clear History", role: .destructive) {
                monitor.clearHistory()
            }
            Button("Keep It", role: .cancel) {}
        } message: {
            Text("The record of every cell that turned red is removed from this Mac.")
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 6) {
                Text(MusterDestination.driftHistory.title)
                    .font(.largeTitle.weight(.semibold))
                Text("Every cell that turned red,  newest first.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 12)
            Button {
                confirmingClear = true
            } label: {
                Label("Clear History", systemImage: "trash")
            }
            .disabled(monitor.transitions.isEmpty)
            .help("Remove every recorded turn from this Mac.")
        }
    }

    /// What the watcher is doing and what it last found.
    private var status: some View {
        let line = watchLine
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                // Green only when every watch asked for started:  a watcher that is off,
                // watching nothing or missing a stream is a warning, whatever its armed flag says.
                Circle()
                    .fill(line.tone == .watching ? Color.green : Color.orange)
                    .frame(width: 8, height: 8)
                    .accessibilityHidden(true)
                Text(line.text)
                    .font(.caption)
                    .foregroundStyle(line.tone == .watching ? Color.secondary : Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
                if let count = monitor.redCount {
                    Text(count == 1 ? "1 red cell" : "\(count) red cells")
                        .font(.caption)
                        .foregroundStyle(count > 0 ? Color.red : Color.secondary)
                } else if monitor.lastRunWasTrusted == false {
                    Text("The last run could not be trusted.")
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                } else {
                    // No run has landed yet,  which is not a run that failed.
                    Text("Checking\u{2026}")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            // The paths no started watch covers (GRID-37),  folded under the line.
            UnwatchedPathsDisclosure(coverage: monitor.watchCoverage)
            if let problem = monitor.historyProblem {
                Text(problem)
                    .font(.caption)
                    .foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The fleet-wide rules failing in the last trusted run (GRID-18):  they reach no
            // cell,  so the red count above leaves them out.
            if !monitor.complianceFailures.isEmpty {
                complianceFailures(monitor.complianceFailures)
                    .padding(.top, 6)
            }
        }
    }

    /// What the watcher actually holds,  in the same words the Overview uses (GRID-37):  a
    /// stream that failed to start makes the line a warning.
    private var watchLine: GridPresentation.WatchLine {
        GridPresentation.WatchLine.make(
            isWatching: monitor.isWatching,
            coverage: monitor.watchCoverage,
            seatCount: monitor.watchedSeats.count,
            recheckSeconds: DriftMonitor.safetyNetInterval
        )
    }

    /// The compliance results failing now,  each with its rule,  its place in the app's words,
    /// the engine's state and what the engine said.
    private func complianceFailures(_ failures: [DriftComplianceFailure]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(GridPresentation.complianceFailuresTitle(count: failures.count))
                .font(.headline)
            Text(GridPresentation.complianceFailuresExplanation)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(failures) { failure in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        Circle()
                            .fill(GridPresentation.colourState(of: failure).tint)
                            .frame(width: 7, height: 7)
                            .accessibilityHidden(true)
                        Text(failure.ruleID)
                            .font(.system(size: 11, weight: .semibold, design: .monospaced))
                            .textSelection(.enabled)
                        Text(failure.placeTitle)
                            .font(.system(size: 9, weight: .semibold))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(Color.accentColor.opacity(0.12), in: Capsule())
                        Text(failure.state)
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        Spacer()
                    }
                    if !failure.reason.isEmpty {
                        Text(failure.reason)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    /// The list of turns.
    private var table: some View {
        let filtered = filteredTransitions
        let groups = groupedBySeat(filtered)
        return VStack(alignment: .leading, spacing: 8) {
            filterToolbar(filteredCount: filtered.count, groupSeats: groups.map(\.seat))
            if filtered.isEmpty {
                HStack(spacing: 8) {
                    Text("No drift transitions match the current filter.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Reset Filters") {
                        searchText = ""
                        stateFilter = .all
                    }
                    .buttonStyle(.link)
                }
                .padding(.vertical, 6)
            } else if grouping == .newestFirst {
                VStack(alignment: .leading, spacing: 0) {
                    GridRowHeader(labels: ["Time", "Seat", "Kind", "Check", "From", "To"])
                    ForEach(filtered) { transition in
                        DriftTransitionRow(transition: transition)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(groups) { group in
                        let isCollapsed = collapsedSeats.contains(group.seat)
                        VStack(alignment: .leading, spacing: 6) {
                            MusterGroupHeaderButton(
                                title: group.seat,
                                isExpanded: !isCollapsed,
                                summary: group.summary,
                                badges: [MusterBadgeSpec(text: "\(group.rows.count)", color: .orange)]
                            ) {
                                if isCollapsed {
                                    collapsedSeats.remove(group.seat)
                                } else {
                                    collapsedSeats.insert(group.seat)
                                }
                            }
                            if !isCollapsed {
                                VStack(alignment: .leading, spacing: 0) {
                                    GridRowHeader(labels: ["Time", "Seat", "Kind", "Check", "From", "To"])
                                    ForEach(group.rows) { transition in
                                        DriftTransitionRow(transition: transition)
                                    }
                                }
                                .padding(.leading, 14)
                            }
                        }
                    }
                }
            }
        }
    }

    private var filteredTransitions: [DriftTransition] {
        let needle = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        return monitor.transitions.filter { transition in
            guard stateFilter.matches(transition) else { return false }
            guard !needle.isEmpty else { return true }
            return [transition.seat, transition.kindTitle, transition.checkText, transition.fromText, transition.toState, transition.timeText]
                .contains { $0.localizedCaseInsensitiveContains(needle) }
        }
    }

    private func groupedBySeat(_ transitions: [DriftTransition]) -> [DriftSeatGroup] {
        var order: [String] = []
        var buckets: [String: [DriftTransition]] = [:]
        for transition in transitions {
            if buckets[transition.seat] == nil { order.append(transition.seat) }
            buckets[transition.seat, default: []].append(transition)
        }
        return order.compactMap { seat in
            guard let rows = buckets[seat] else { return nil }
            let kinds = rows.reduce(into: [String]()) { acc, row in
                if !acc.contains(row.kindTitle) { acc.append(row.kindTitle) }
            }
            return DriftSeatGroup(
                seat: seat,
                summary: kinds.prefix(4).joined(separator: ", ") + (kinds.count > 4 ? " +\(kinds.count - 4)" : ""),
                rows: rows
            )
        }
    }

    private func filterToolbar(filteredCount: Int, groupSeats: [String]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                MusterSearchField(prompt: "Filter seat, kind, rule, or state\u{2026}", text: $searchText)
                Picker("State", selection: $stateFilter) {
                    ForEach(DriftHistoryStateFilter.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
            HStack(spacing: 8) {
                Picker("Group", selection: $grouping) {
                    ForEach(DriftHistoryGrouping.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                Spacer()
                Text("Showing \(filteredCount) of \(monitor.transitions.count) transitions")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if grouping == .bySeat && !groupSeats.isEmpty {
                    let allCollapsed = groupSeats.allSatisfy { collapsedSeats.contains($0) }
                    Button(allCollapsed ? "Expand All" : "Collapse All") {
                        if allCollapsed {
                            collapsedSeats.removeAll()
                        } else {
                            collapsedSeats = Set(groupSeats)
                        }
                    }
                    .font(.caption)
                    .buttonStyle(.link)
                }
            }
        }
    }

    /// The empty state,  which says what the list would hold.
    private var empty: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Nothing has turned red.")
                .font(.callout.weight(.medium))
            Text("A cell is added here when it turns red,  and it is kept until it is cleared.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 8)
    }
}

/// The paths no started watch covers,  folded under the watch line (GRID-37).  It shows
/// nothing when every path has a watch on it.  The Overview uses it too.
struct UnwatchedPathsDisclosure: View {
    let coverage: DriftWatchCoverage

    var body: some View {
        if let title = GridPresentation.unwatchedPathsTitle(coverage) {
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 4) {
                    Text(GridPresentation.unwatchedPathsExplanation)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    ForEach(Array(GridPresentation.unwatchedPathLines(coverage).enumerated()), id: \.offset) { _, path in
                        Text(path)
                            .font(.system(size: 10, design: .monospaced))
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.top, 4)
            } label: {
                Text(title)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.orange)
            }
        }
    }
}

/// One turn,  as a row of the history.
private struct DriftTransitionRow: View {
    let transition: DriftTransition

    var body: some View {
        HStack(spacing: 12) {
            GridCellText(value: transition.timeText)
                .frame(width: 62, alignment: .leading)
            GridCellText(value: transition.seat, emphasised: true)
                .frame(width: 92, alignment: .leading)
            GridCellText(value: transition.kindTitle)
                .frame(width: 92, alignment: .leading)
            GridCellText(value: transition.checkText)
                .frame(maxWidth: .infinity, alignment: .leading)
            GridCellText(value: transition.fromText)
                .frame(width: 92, alignment: .leading)
            HStack(spacing: 4) {
                Circle()
                    .fill(CellState(checkState: CheckState(envelopeValue: transition.toState)).tint)
                    .frame(width: 7, height: 7)
                GridCellText(value: transition.toState)
            }
            .frame(width: 92, alignment: .leading)
        }
        .padding(.vertical, 3)
        .contextMenu {
            Button("Copy Transition Summary") {
                MusterClipboard.copy(
                    "\(transition.timeText) \(transition.seat) \(transition.kindTitle) (\(transition.checkText)): \(transition.fromText) -> \(transition.toState)"
                )
            }
            Button("Copy Seat Name") {
                MusterClipboard.copy(transition.seat)
            }
            if let rule = transition.ruleID {
                Button("Copy Rule ID") {
                    MusterClipboard.copy(rule)
                }
            }
        }
    }
}

private enum DriftHistoryGrouping: String, CaseIterable, Identifiable {
    case newestFirst
    case bySeat

    var id: String { rawValue }

    var label: String {
        switch self {
        case .newestFirst: return "Newest first"
        case .bySeat: return "By seat"
        }
    }
}

private enum DriftHistoryStateFilter: String, CaseIterable, Identifiable {
    case all
    case missing
    case drift
    case unknown
    case unproven

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "All"
        case .missing: return "Missing"
        case .drift: return "Drifted"
        case .unknown: return "Unknown"
        case .unproven: return "Unproven"
        }
    }

    func matches(_ transition: DriftTransition) -> Bool {
        if self == .all { return true }
        let state = CellState(checkState: CheckState(envelopeValue: transition.toState))
        switch self {
        case .all: return true
        case .missing: return state == .missing
        case .drift: return state == .drift
        case .unknown: return state == .unknown
        case .unproven: return state == .unproven
        }
    }
}

private struct DriftSeatGroup: Identifiable {
    let seat: String
    let summary: String
    let rows: [DriftTransition]
    var id: String { seat }
}

#Preview {
    DriftHistoryView()
        .environmentObject(DriftMonitor(grid: PreviewGrid.shared))
}

/// The empty grid the previews use,  so a preview never runs an engine.
final class PreviewGrid: DriftGridReading {
    static let shared = PreviewGrid()

    var cells: [GridCell] = []
    var manifest: SeatManifest? = nil
    var isRunning: Bool = false
    var onRunFinished: ((Bool) -> Void)?

    func refresh() {}
}
