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

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(cell.state.tint)
                .frame(width: 7, height: 7)
            Text(cell.state.shortLabel)
                .font(.system(size: 10, weight: .semibold, design: .monospaced))
            Spacer(minLength: 0)
            Text(cell.proofLevel ?? "—")
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
    enum GridFilter: String, CaseIterable, Identifiable {
        case all = "All Seats"
        case attention = "Needing Attention"

        var id: String { rawValue }
    }

    @EnvironmentObject private var store: FleetStore
    @State private var selectedCell: GridCell?
    @State private var gridFilter: GridFilter = .all
    @State private var searchText: String = ""
    /// The height of the detail panel,  which the operator drags and which survives
    /// the next launch.
    @AppStorage("muster.detailPanelHeight") private var panelHeight: Double = 420

    private let seatWidth: CGFloat = 132
    private let columnWidth: CGFloat = 108
    private let minPanelHeight: CGFloat = 170
    private let maxPanelHeight: CGFloat = 900

    private var visibleRoster: [String] {
        let base: [String]
        switch gridFilter {
        case .all:
            base = store.roster
        case .attention:
            base = store.roster.filter { seat in
                ConfigKind.columnOrder.contains { kind in
                    guard let state = store.cell(seat: seat, kind: kind)?.state else { return false }
                    return state == .drift || state == .missing || state == .unproven || state == .unknown
                }
            }
        }
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !query.isEmpty else { return base }
        return base.filter { $0.lowercased().contains(query) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    filterBar
                        .padding(.bottom, 12)
                    header
                    ForEach(visibleRoster, id: \.self) { seat in
                        row(for: seat)
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
            if let cell = selectedCell {
                PanelResizeHandle(height: panelHeightBinding)
                ConfigFilePanel(
                    cell: cell,
                    artifacts: store.artifacts(for: cell),
                    sources: store.ruleSources(seat: cell.seat, kind: cell.kind),
                    onClose: { selectedCell = nil }
                )
                .frame(height: CGFloat(panelHeight))
            }
        }
    }

    private var filterBar: some View {
        HStack(spacing: 12) {
            Picker("Seats", selection: $gridFilter) {
                ForEach(GridFilter.allCases) { item in
                    Text(item.rawValue).tag(item)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 280)
            TextField("Filter seats by tag…", text: $searchText)
                .textFieldStyle(.roundedBorder)
                .font(.system(size: 11, design: .monospaced))
                .frame(maxWidth: 200)
            Spacer()
            Text("\(visibleRoster.count) of \(store.roster.count) seat(s)")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private func complianceSection(_ manifest: SeatManifest) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Divider()
            Text("Fleet Compliance Rules")
                .font(.headline)
            Text("Cross-seat compliance rules and documented coverage gaps from fleet-seats.json.")
                .font(.caption)
                .foregroundStyle(.secondary)
            ForEach(manifest.allComplianceRules, id: \.id) { rule in
                VStack(alignment: .leading, spacing: 2) {
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
                        if let checkType = rule.check?.type, !checkType.isEmpty {
                            Text(checkType)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        if rule.implemented == false {
                            Text("UNIMPLEMENTED")
                                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.orange)
                        } else {
                            Text("ACTIVE")
                                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.green)
                        }
                        Spacer()
                    }
                    if let target = rule.check?.target, !target.isEmpty {
                        Text("Target:  \(target)")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let reason = rule.implementationReason, !reason.isEmpty {
                        Text("Reason:  \(reason)")
                            .font(.system(size: 10))
                            .foregroundStyle(rule.implemented == false ? .orange : .secondary)
                    }
                }
                .padding(.vertical, 2)
            }
            if !manifest.complianceRulesNotImplemented.isEmpty {
                Text("Documented Coverage Gaps (\(manifest.complianceRulesNotImplemented.count))")
                    .font(.subheadline.weight(.semibold))
                    .padding(.top, 6)
                ForEach(manifest.complianceRulesNotImplemented, id: \.id) { gap in
                    HStack(spacing: 8) {
                        Text(gap.id)
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        Text(gap.implementationReason ?? "Not implemented yet")
                            .font(.system(size: 10))
                            .foregroundStyle(.orange)
                        Spacer()
                    }
                    .padding(.vertical, 1)
                }
            }
        }
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

    private var header: some View {
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
            if let common = store.commonCheckedAt {
                // The run checks every rule at one moment,  so the time goes here once
                // instead of in ninety cells.
                HStack(spacing: 8) {
                    Spacer().frame(width: seatWidth)
                    Text("Checked \(FleetTime.clock(common)),  \(FleetTime.ageText(since: common)) ago")
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                    .help("When the engine checked the rules this run.  A cell shows its own age only when it was checked apart from the rest.")
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
            Text("What a badge says")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 22) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("L0").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("The config is there and matches by hash.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("L1").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("The hook was sent a write and it denied it.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("L2").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("A headless canary showed the platform loads the hook.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("2m").font(.system(size: 10, weight: .semibold, design: .monospaced))
                    Text("The age of the observation the badge shows.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(for seat: String) -> some View {
        HStack(spacing: 8) {
            Text(seat)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .frame(width: seatWidth, alignment: .leading)
            ForEach(ConfigKind.columnOrder, id: \.self) { kind in
                if let cell = store.cell(seat: seat, kind: kind) {
                    Button {
                        selectedCell = cell
                    } label: {
                        CellBadge(cell: cell, age: exceptionAge(for: cell), width: columnWidth)
                    }
                    .buttonStyle(.plain)
                    .help(cell.tooltip)
                } else {
                    Color.clear.frame(width: columnWidth, height: 30)
                }
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
