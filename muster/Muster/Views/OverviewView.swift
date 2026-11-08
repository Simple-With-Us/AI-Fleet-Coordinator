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
    /// The filter a count card asks the grid to open on.  The window holds it.
    @Binding var gridRequest: GridStateFilter?

    init(selection: Binding<MusterDestination>, gridRequest: Binding<GridStateFilter?>) {
        _selection = selection
        _gridRequest = gridRequest
    }

    /// True unless the last seat check is older than its window,  as after a long sleep.  An
    /// old check's counts are not current,  so they read as not known until a new one lands.
    private var seatCheckIsCurrent: Bool {
        monitor.lastRunAt.map { SeatCheckFreshness.isCurrent(checkedAt: $0, now: watch.now) } ?? true
    }

    private var counts: OverviewCounts {
        OverviewCounts.make(
            cells: store.cells,
            trusted: store.phase == .loaded && seatCheckIsCurrent,
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
                VStack(alignment: .leading, spacing: 2) {
                    Label("Seat Setup \(FleetHost.mac.onTitle)", systemImage: "square.grid.3x3")
                        .font(.title3.weight(.semibold))
                    // The counts below prove the Mac's config and nothing else.
                    Text(GridPresentation.overviewScope)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    verdictRow
                    if seatCheckIsCurrent,
                       let meaning = RunVerdict.meaning(phase: store.phase, verdict: store.summary?.overallVerdict) {
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
                        title: GridStateFilter.checkedAtAnotherTime.rawValue,
                        systemImage: "clock",
                        count: counts.stale,
                        tint: .secondary,
                        lines: ["Results from another time than the rest of this check"],
                        filter: .checkedAtAnotherTime
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
            RunVerdictLabel(verdict: SeatCheckFreshness.verdict(
                RunVerdict.make(phase: store.phase, verdict: store.summary?.overallVerdict),
                isCurrent: seatCheckIsCurrent))
            if seatCheckIsCurrent, let failing = RunVerdict.failingText(store.summary) {
                Text(failing)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if store.phase == .running {
                ProgressView().controlSize(.small)
            }
            Spacer()
            // With its age and a day word,  at the menu's own moment,  so a check from last
            // night never reads as this morning's,  and an old one says it is not current.
            if let line = SeatCheckFreshness.overviewLine(checkedAt: monitor.lastRunAt, now: watch.now) {
                let current = monitor.lastRunAt.map { SeatCheckFreshness.isCurrent(checkedAt: $0, now: watch.now) } ?? false
                Text(line)
                    .font(.callout)
                    .foregroundStyle(current ? Color.secondary : Color.orange)
            }
        }
    }

    /// What the watcher is set to watch,  always shown:  a watcher that is off or watching
    /// nothing is a warning,  never a hidden line.
    private var watchLine: some View {
        let line = GridPresentation.WatchLine.make(
            isWatching: monitor.isWatching,
            pathCount: monitor.watchedPathCount,
            seatCount: monitor.watchedSeats.count,
            recheckSeconds: DriftMonitor.safetyNetInterval
        )
        return Label(line.text, systemImage: line.tone == .watching ? "eye" : "eye.slash")
            .font(.callout)
            .foregroundStyle(line.tone == .watching ? Color.secondary : Color.orange)
            .fixedSize(horizontal: false, vertical: true)
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
            gridRequest = filter
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
