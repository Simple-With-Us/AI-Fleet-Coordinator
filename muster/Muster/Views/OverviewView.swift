import SwiftUI

/// Both machines at a glance,  then the seat setup the Mac checks,  each one a way into
/// the panel that holds its rows.
///
/// The two place cards are built from the menu's own summary of each place,  so the
/// Overview and the menu say the same thing in the same words.  The counts come off the
/// store's cells,  which are the same cells the grid paints,  so the Overview cannot
/// disagree with the grid it points at.  A run that could not be trusted leaves every
/// engine count unknown:  the store has cleared its results,  and a count of zero would
/// read as a fleet with nothing wrong.
struct OverviewView: View {
    @EnvironmentObject private var store: FleetStore
    @EnvironmentObject private var monitor: DriftMonitor
    @EnvironmentObject private var admin: FleetAdmin
    @EnvironmentObject private var watch: PlaceWatch
    @EnvironmentObject private var router: ServiceActionRouter
    @Binding var selection: MusterDestination

    init(selection: Binding<MusterDestination>) {
        _selection = selection
    }

    private var counts: OverviewCounts {
        OverviewCounts.make(
            cells: store.cells,
            trusted: store.phase == .loaded,
            commonCheckedAt: store.commonCheckedAt
        )
    }

    var body: some View {
        let menu = MachineMenu(MachineMenuInputs(admin: admin, monitor: monitor, watch: watch, router: router))
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 280), spacing: 12)], spacing: 12) {
                    placeCard(menu.macCard)
                    placeCard(menu.serverCard)
                }
                Divider()
                Label("Seat Setup \(FleetHost.mac.onTitle)", systemImage: "square.grid.3x3")
                    .font(.title3.weight(.semibold))
                verdictRow
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 170), spacing: 12)], spacing: 12) {
                    countCard(
                        title: "Red Cells",
                        systemImage: "square.grid.3x3",
                        count: counts.red,
                        tint: counts.red.map { $0 > 0 ? .red : .secondary } ?? .secondary,
                        lines: counts.redBreakdown ?? [],
                        filter: .red
                    )
                    countCard(
                        title: "Drifted",
                        systemImage: "arrow.left.arrow.right",
                        count: counts.drift,
                        tint: counts.drift.map { $0 > 0 ? .orange : .secondary } ?? .secondary,
                        lines: ["Differs from what the fleet expects"],
                        filter: .drift
                    )
                    countCard(
                        title: "Unproven",
                        systemImage: "questionmark.diamond",
                        count: counts.unproven,
                        tint: counts.unproven.map { $0 > 0 ? .orange : .secondary } ?? .secondary,
                        lines: ["Checked, but the check could not prove it"],
                        filter: .unproven
                    )
                    // A result checked at another time is not a fault,  so this card has no
                    // warning colour:  the engine's own rules say whether a result is too old.
                    countCard(
                        title: "Checked Earlier",
                        systemImage: "clock",
                        count: counts.stale,
                        tint: .secondary,
                        lines: ["Results from another time than the rest of this check"],
                        filter: .all
                    )
                    OverviewCardButton(hint: "Opens \(MusterDestination.driftHistory.title)") {
                        selection = .driftHistory
                    } label: {
                        cardBody(
                            title: "Drift History",
                            systemImage: "exclamationmark.triangle",
                            count: monitor.transitions.count,
                            tint: .secondary,
                            lines: ["Times something went red"]
                        )
                    }
                }
                watchLine
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: The two places

    /// One machine,  in the menu's words,  opening the panel for that machine.
    private func placeCard(_ card: PlaceCard) -> some View {
        OverviewCardButton(hint: "Opens \(card.destination.title)") {
            selection = card.destination
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Label(card.host.heading, systemImage: card.host.systemImage)
                        .font(.headline)
                    Spacer()
                    Image(systemName: "chevron.right")
                        .foregroundStyle(.tertiary)
                        .accessibilityHidden(true)
                }
                PlaceHealthLabel(health: card.health, font: .title3.weight(.semibold))
                ForEach(Array(card.lines.enumerated()), id: \.offset) { _, line in
                    Text(line)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    // MARK: The seat setup

    /// The verdict,  its failing count and when the Mac last checked.
    private var verdictRow: some View {
        HStack(spacing: 10) {
            RunVerdictLabel(verdict: RunVerdict.make(phase: store.phase, verdict: store.summary?.overallVerdict))
            if let failing = RunVerdict.failingText(store.summary) {
                Text(failing)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if store.phase == .running {
                ProgressView().controlSize(.small)
            }
            Spacer()
            if let lastRunAt = monitor.lastRunAt {
                Text("Last checked at \(FleetTime.clock(lastRunAt))")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// What the watcher holds,  when it holds anything.
    @ViewBuilder
    private var watchLine: some View {
        if monitor.isWatching {
            Label(
                "Watching \(monitor.watchedPathCount) paths on the Mac for \(monitor.watchedSeats.count) seats",
                systemImage: "eye"
            )
            .font(.callout)
            .foregroundStyle(.secondary)
        }
    }

    /// One count,  opening the grid on the seats it counts.  A nil count reads unknown,
    /// never zero.
    private func countCard(
        title: String,
        systemImage: String,
        count: Int?,
        tint: Color,
        lines: [String],
        filter: GridStateFilter
    ) -> some View {
        OverviewCardButton(hint: "Opens \(MusterDestination.complianceGrid.title), showing \(filter.rawValue)") {
            GridStateFilter.request(filter)
            selection = .complianceGrid
        } label: {
            cardBody(title: title, systemImage: systemImage, count: count, tint: tint, lines: lines)
        }
    }

    private func cardBody(title: String, systemImage: String, count: Int?, tint: Color, lines: [String]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Label(title, systemImage: systemImage)
                    .font(.headline)
                Spacer()
                Image(systemName: "chevron.right")
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
            Text(count.map { "\($0)" } ?? "\u{2014}")
                .font(.largeTitle.weight(.semibold).monospaced())
                .foregroundStyle(tint)
            if count == nil {
                Text("Not known")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(lines, id: \.self) { line in
                    Text(line)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// A card that is a button,  and looks like one:  a chevron,  a border that answers the
/// pointer,  and a hint that says where it goes.
private struct OverviewCardButton<CardLabel: View>: View {
    let hint: String
    let action: () -> Void
    let label: CardLabel
    @State private var hovering = false

    init(hint: String, action: @escaping () -> Void, @ViewBuilder label: () -> CardLabel) {
        self.hint = hint
        self.action = action
        self.label = label()
    }

    var body: some View {
        Button(action: action) {
            label
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(14)
                .background(.background, in: RoundedRectangle(cornerRadius: 12))
                .overlay {
                    RoundedRectangle(cornerRadius: 12)
                        .strokeBorder(hovering ? Color.accentColor : Color.secondary.opacity(0.3), lineWidth: 1)
                }
                .contentShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityHint(hint)
    }
}
