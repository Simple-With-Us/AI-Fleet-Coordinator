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
            Text(cell.ageText)
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 7)
        .frame(width: width, height: 30)
        .background(cell.state.background, in: RoundedRectangle(cornerRadius: 7))
        .overlay {
            RoundedRectangle(cornerRadius: 7)
                .strokeBorder(cell.state.border, lineWidth: 1)
        }
        .contentShape(Rectangle())
    }
}

/// The seat by config grid:  rows are seats, columns are the six config kinds.
struct FleetGridView: View {
    @EnvironmentObject private var store: FleetStore
    @State private var selectedCell: GridCell?

    private let seatWidth: CGFloat = 132
    private let columnWidth: CGFloat = 108

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                    ForEach(store.roster, id: \.self) { seat in
                        row(for: seat)
                    }
                    legend
                        .padding(.top, 18)
                }
                .padding(.horizontal, 22)
                .padding(.vertical, 18)
            }
            Divider()
            // The panel is always there,  not only after a click:  an operator opening
            // the grid should see what the first cell refers to without touching it.
            ConfigFilePanel(
                cell: selectedCell ?? store.cells.first ?? GridCell.placeholder,
                artifacts: artifacts
            )
            .frame(maxHeight: 420)
        }
        .sheet(item: $selectedCell) { cell in
            CellDetailSheet(cell: cell)
        }
    }

    /// The artifacts the panel shows.
    private var artifacts: FleetStore.CellArtifacts {
        if let cell = selectedCell { return store.artifacts(for: cell) }
        if let first = store.cells.first { return store.artifacts(for: first) }
        return FleetStore.CellArtifacts(rules: [], views: [], coverReason: nil)
    }

    private var header: some View {
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
        }
        .padding(.bottom, 8)
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
                        CellBadge(cell: cell, width: columnWidth)
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

/// The detail sheet for one cell:  expected against observed, and the last proof.
struct CellDetailSheet: View {
    let cell: GridCell
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if cell.rules.isEmpty {
                        Text(
                            cell.coverReason
                                ?? "No rule and no not-applicable cover account for this seat and kind."
                        )
                        .foregroundStyle(.secondary)
                    } else {
                        ForEach(cell.rules) { rule in
                            ruleDetail(rule)
                        }
                    }
                }
                .padding(20)
            }
        }
        .frame(width: 660, height: 560)
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(cell.seat)
                .font(.system(size: 15, weight: .semibold, design: .monospaced))
            Text("/")
                .foregroundStyle(.secondary)
            Text(cell.kind.title)
                .font(.headline)
            HStack(spacing: 5) {
                Circle()
                    .fill(cell.state.tint)
                    .frame(width: 8, height: 8)
                Text(cell.state.rawValue)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
            }
            .foregroundStyle(cell.state.tint)
            Spacer()
            Button("Close") {
                dismiss()
            }
            .keyboardShortcut(.cancelAction)
        }
        .padding(20)
    }

    private func ruleDetail(_ rule: CheckResult) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(rule.ruleId)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                Text(rule.platform)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(rule.state)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                    .foregroundStyle(rule.checkState == .passed ? Color.green : Color.orange)
            }

            Text(rule.reason)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(alignment: .top, spacing: 18) {
                ValueTable(title: "Expected", values: rule.expected)
                ValueTable(title: "Observed", values: rule.observed)
            }

            GroupBox("Last proof") {
                VStack(alignment: .leading, spacing: 4) {
                    ProofLine(label: "Proof", value: rule.proofText)
                    ProofLine(label: "Check type", value: rule.checkType)
                    ProofLine(label: "Collector", value: rule.collector)
                    ProofLine(label: "Reason code", value: rule.reasonCode)
                    ProofLine(label: "Checked at", value: rule.checkedAt)
                    ProofLine(label: "Expires at", value: rule.expiresAt ?? "—")
                    ProofLine(label: "Time to live", value: "\(rule.ttlSeconds)s")
                    ProofLine(label: "Stale", value: rule.stale ? "yes" : "no")
                    ProofLine(label: "Counts as failing", value: rule.failing ? "yes" : "no")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }
        }
        .padding(.bottom, 6)
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
