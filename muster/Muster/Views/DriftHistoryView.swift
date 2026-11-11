import SwiftUI

/// The drift history:  every cell that turned red,  newest first.
///
/// The list is kept on disk,  so it is there after a restart,  and it is cleared
/// by hand when the owner has read it.
struct DriftHistoryView: View {
    @EnvironmentObject private var monitor: DriftMonitor
    @State private var confirmingClear = false

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
        VStack(alignment: .leading, spacing: 0) {
            GridRowHeader(labels: ["Time", "Seat", "Kind", "Check", "From", "To"])
            ForEach(monitor.transitions) { transition in
                DriftTransitionRow(transition: transition)
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
    }
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
