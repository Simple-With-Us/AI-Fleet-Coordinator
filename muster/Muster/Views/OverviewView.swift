import SwiftUI

/// The fleet at a glance:  the verdict and the counts the last run gives, each one a
/// way into the panel that holds its rows.
///
/// The counts come off the store's cells, which are the same cells the grid paints,
/// so the overview cannot disagree with the grid it points at.  A run that could not
/// be trusted leaves every engine count unknown:  the store has cleared its results, and a
/// count of zero would read as a fleet with nothing wrong.
struct OverviewView: View {
    @EnvironmentObject private var store: FleetStore
    @EnvironmentObject private var monitor: DriftMonitor
    @Binding var selection: MusterDestination

    private var counts: OverviewCounts {
        OverviewCounts.make(
            cells: store.cells,
            trusted: store.phase == .loaded,
            commonCheckedAt: store.commonCheckedAt
        )
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 4) {
                    verdictRow
                    if let meaning = verdictMeaning {
                        Text(meaning)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 170), spacing: 12)], spacing: 12) {
                    countCard(
                        title: "Red Cells",
                        systemImage: "square.grid.3x3",
                        count: counts.red,
                        tint: counts.red.map { $0 > 0 ? .red : .secondary } ?? .secondary,
                        subtitle: "Cells the grid paints red",
                        destination: .complianceGrid
                    )
                    countCard(
                        title: "Unknown",
                        systemImage: "questionmark.square",
                        count: counts.unknown,
                        tint: counts.unknown.map { $0 > 0 ? .red : .secondary } ?? .secondary,
                        subtitle: "Cells the app could not read",
                        destination: .complianceGrid
                    )
                    countCard(
                        title: "Missing",
                        systemImage: "square.slash",
                        count: counts.missing,
                        tint: counts.missing.map { $0 > 0 ? .red : .secondary } ?? .secondary,
                        subtitle: "Cells naming an absent artifact",
                        destination: .complianceGrid
                    )
                    countCard(
                        title: "Drift",
                        systemImage: "arrow.left.arrow.right",
                        count: counts.drift,
                        tint: counts.drift.map { $0 > 0 ? .orange : .secondary } ?? .secondary,
                        subtitle: "Cells off their wanted state",
                        destination: .complianceGrid
                    )
                    countCard(
                        title: "Stale",
                        systemImage: "clock.badge.exclamationmark",
                        count: counts.stale,
                        tint: counts.stale.map { $0 > 0 ? .orange : .secondary } ?? .secondary,
                        subtitle: "Cells checked apart from the rest",
                        destination: .complianceGrid
                    )
                    countCard(
                        title: "Drift History",
                        systemImage: "exclamationmark.triangle",
                        count: monitor.transitions.count,
                        tint: .secondary,
                        subtitle: "Turns to red on record",
                        destination: .driftHistory
                    )
                }
                watchLine
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// The verdict, its failing count and the watch state, which is what the owner
    /// opens the window to learn.
    private var verdictRow: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(verdictColor)
                .frame(width: 12, height: 12)
            Text(verdictText)
                .font(.title2.weight(.semibold).monospaced())
            if let summary = store.summary {
                Text("\(summary.failing) failing of \(summary.total)")
                    .font(.callout.monospaced())
                    .foregroundStyle(.secondary)
            } else if store.phase == .running {
                ProgressView().controlSize(.small)
            }
            Spacer()
            if let lastRunAt = monitor.lastRunAt {
                Text("Last run \(lastRunAt.formatted(date: .omitted, time: .standard))")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .foregroundStyle(verdictColor)
    }

    private var verdictText: String {
        switch store.phase {
        case .idle: return "IDLE"
        case .running: return "RUNNING"
        case .loaded: return store.summary?.overallVerdict ?? "UNKNOWN"
        case .failed: return "UNKNOWN"
        }
    }

    /// What the verdict above means,  said once so a PASS here is the grid's PASS.
    ///
    /// Only a verdict the engine returned has a meaning to state:  an idle, running or
    /// failed run has none.
    private var verdictMeaning: String? {
        guard store.phase == .loaded, let verdict = store.summary?.overallVerdict else { return nil }
        return "\(PassMeaning.verdict(verdict))\u{00A0} \(PassMeaning.everywhere)"
    }

    private var verdictColor: Color {
        switch store.phase {
        case .idle: return .secondary
        case .running: return .secondary
        case .loaded:
            switch store.summary?.overallVerdict {
            case "PASS": return .green
            case "FAIL": return .red
            default: return .orange
            }
        case .failed: return .red
        }
    }

    /// What the watcher holds, when it holds anything.
    @ViewBuilder
    private var watchLine: some View {
        if monitor.isWatching {
            Label(
                "Watching \(monitor.watchedPathCount) paths for \(monitor.watchedSeats.count) seats",
                systemImage: "eye"
            )
            .font(.callout)
            .foregroundStyle(.secondary)
        }
    }

    /// One count and the place it came from.  A nil count reads unknown, never zero.
    private func countCard(
        title: String,
        systemImage: String,
        count: Int?,
        tint: Color,
        subtitle: String,
        destination: MusterDestination
    ) -> some View {
        Button {
            selection = destination
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                Label(title, systemImage: systemImage)
                    .font(.headline)
                Text(count.map { "\($0)" } ?? "—")
                    .font(.largeTitle.weight(.semibold).monospaced())
                    .foregroundStyle(tint)
                Text(count != nil ? subtitle : "Unknown")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(.background, in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12).strokeBorder(.quaternary, lineWidth: 1)
            }
        }
        .buttonStyle(.plain)
    }
}

#Preview {
    OverviewView(selection: .constant(.overview))
        .environmentObject(FleetStore())
        .environmentObject(DriftMonitor(grid: PreviewGrid.shared))
}
