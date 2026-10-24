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
                Text("Drift History")
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
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle()
                    .fill(monitor.isWatching ? Color.green : Color.secondary)
                    .frame(width: 8, height: 8)
                Text(watchingText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let count = monitor.redCount {
                    Text("\(count) red cell(s)")
                        .font(.caption)
                        .foregroundStyle(count > 0 ? Color.red : Color.secondary)
                } else {
                    Text("The last run could not be trusted.")
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                }
            }
            if let problem = monitor.historyProblem {
                Text(problem)
                    .font(.caption)
                    .foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// What the watcher is watching,  in words.
    private var watchingText: String {
        let paths = monitor.watchedPathCount
        let seats = monitor.watchedSeats.count
        guard paths > 0 else {
            return "No paths are watched,  so nothing can be seen change."
        }
        return "Watching \(paths) path(s) the manifest names,  for \(seats) seat(s)."
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
