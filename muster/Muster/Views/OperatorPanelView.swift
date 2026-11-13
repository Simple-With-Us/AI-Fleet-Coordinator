import SwiftUI

/// The Operator panel:  the one service on the server that reads Zulip, wakes the right chat and
/// holds the letters.
///
/// From the top:  the header, a banner when the server cannot be reached, three cards (the
/// Eavesdropper, the wake router and the letter registry), the chats the registry holds, the
/// wakes of the last day, the Operator's settings, and a footer.  Every word comes from
/// `OperatorPanelPresentation`, which is plain code with tests;  this view only draws it.
///
/// It reads the server and nothing on the Mac:  `server eavesdropper-status`,
/// `server operator-sessions` and `server operator-wakes`, together, each kept on its own so a
/// read that fails never blanks another.  It reads when it opens, every minute while it is open
/// and when the Mac wakes, and on Refresh.  Nothing here changes anything except the settings
/// section, which coalesces typed edits and authenticates automatically through the shared
/// server command; only verified persistence, readback and runtime application appear Saved.
struct OperatorPanelView: View {
    /// Opens the Controls panel, where the Operator's knobs are.
    var openControls: () -> Void = {}

    @ObservedObject private var store = OperatorPanelStore.shared

    /// How often the panel reads again while it is open.
    static let rereadSeconds: TimeInterval = 60

    var body: some View {
        VStack(spacing: 0) {
            if let reason = OperatorPanelPresentation.unreachableReason(
                status: store.status, sessions: store.sessions, wakes: store.wakes) {
                FailureBanner(reason: reason, title: OperatorPanelPresentation.unreachableTitle)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    header
                    TimelineView(.periodic(from: .now, by: 10)) { context in
                        readings(now: context.date)
                    }
                    SectionPanel(title: "Settings") {
                        ServerListenerSettingsSection()
                    }
                    footer
                }
                .padding(.horizontal, 22)
                .padding(.vertical, 18)
            }
        }
        .rereadsOnTickAndWake(every: Self.rereadSeconds) {
            await Self.readEverything(store)
        }
    }

    /// Reads every server answer the panel shows, together:  the three of the store, and the
    /// Settings section's.  A read in progress is joined, never started twice.
    static func readEverything(_ store: OperatorPanelStore) async {
        async let panel: Void = store.refresh()
        async let settings: Void = ServerListenerSettingsStore.shared.refresh()
        _ = await (panel, settings)
    }

    // MARK: The header

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(OperatorPanelCopy.heading)
                .font(.title2.weight(.semibold))
            Spacer()
            TimelineView(.periodic(from: .now, by: 5)) { context in
                Text(headerAge(now: context.date))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if store.loading { ProgressView().controlSize(.small) }
            Button {
                Task { await Self.readEverything(store) }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .disabled(store.loading)
            .help("Reads the Operator on the server again.")
        }
    }

    /// "Refreshed Ns ago" only when that refresh read something;  otherwise it says it tried.
    private func headerAge(now: Date) -> String {
        if let readAt = store.readAt { return "Refreshed \(OperatorPanelText.relative(readAt, now: now))" }
        if let triedAt = store.triedAt { return "Tried \(OperatorPanelText.relative(triedAt, now: now)), nothing was read" }
        return "Not read yet"
    }

    // MARK: What the server says

    @ViewBuilder
    private func readings(now: Date) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                OperatorCardView(card: OperatorPanelPresentation.eavesdropper(store.status, now: now))
                OperatorCardView(card: OperatorPanelPresentation.router(wakes: store.wakes, status: store.status, now: now))
                OperatorCardView(card: OperatorPanelPresentation.registry(sessions: store.sessions, status: store.status, now: now))
            }
            // One height for the three cards, whatever each says.
            .fixedSize(horizontal: false, vertical: true)
            let warnings = OperatorPanelPresentation.registryWarnings(sessions: store.sessions, status: store.status, now: now)
            if !warnings.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(warnings, id: \.self) { warning in
                        Label {
                            Text(warning).fixedSize(horizontal: false, vertical: true)
                        } icon: {
                            Image(systemName: "exclamationmark.triangle.fill").accessibilityHidden(true)
                        }
                        .font(.caption)
                        .foregroundStyle(Color.orange)
                    }
                }
            }
            seatsAndChats(now: now)
            recentWakes(now: now)
        }
    }

    @ViewBuilder
    private func seatsAndChats(now: Date) -> some View {
        SectionPanel(title: OperatorPanelCopy.seatsHeading, subtitle: "Live chats first, newest first.") {
            switch store.sessions {
            case nil:
                EmptyNote(text: "Reading the letter registry\u{2026}")
            case .unreachable(let why)?, .unavailable(let why)?:
                EmptyNote(text: why)
            case .read(let document)?:
                let table = OperatorPanelPresentation.seatsTable(document, status: store.status, now: now)
                if table.rows.isEmpty {
                    EmptyNote(text: "The registry holds no chats.")
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        OperatorSeatHeader()
                        Divider()
                        ForEach(table.rows) { OperatorSeatRowView(row: $0) }
                    }
                }
                if table.moreCount > 0 {
                    Text("and \(table.moreCount) more")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let note = table.note {
                    Text(note)
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    @ViewBuilder
    private func recentWakes(now: Date) -> some View {
        SectionPanel(title: OperatorPanelCopy.wakesHeading, subtitle: "The last 24 hours, newest first.") {
            switch store.wakes {
            case nil:
                EmptyNote(text: "Reading the wake record\u{2026}")
            case .unreachable(let why)?, .unavailable(let why)?:
                EmptyNote(text: why)
            case .read(let document)?:
                let table = OperatorPanelPresentation.wakesTable(document, now: now)
                if table.rows.isEmpty {
                    EmptyNote(text: "No wakes in the last 24 hours.")
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        OperatorWakeHeader()
                        Divider()
                        ForEach(table.rows) { OperatorWakeRowView(row: $0) }
                    }
                }
                if table.moreCount > 0 {
                    Text("and \(table.moreCount) more")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let summary = table.summary {
                    Text(summary)
                        .font(.callout)
                }
                ForEach(table.notes, id: \.self) { note in
                    Text(note)
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    // MARK: The footer

    private var footer: some View {
        HStack(spacing: 6) {
            Text(OperatorPanelCopy.retiredListener)
                .font(.caption)
                .foregroundStyle(.secondary)
            Button(OperatorPanelCopy.controlsPointer) { openControls() }
                .buttonStyle(.link)
                .font(.caption)
                .help("Opens Controls, where the Operator's knobs are pinned in their own section.")
            Spacer(minLength: 0)
        }
    }
}

// MARK: - Pieces

private extension OperatorTone {
    var tint: Color {
        switch self {
        case .good: return .green
        case .warning: return .orange
        case .bad: return .red
        case .neutral: return .secondary
        }
    }

    var symbol: String {
        switch self {
        case .good: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .bad: return "xmark.octagon.fill"
        case .neutral: return "questionmark.circle"
        }
    }
}

/// One of the three cards:  its title, its state in words with a symbol that matches, and its lines.
private struct OperatorCardView: View {
    let card: OperatorStatusCard

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(card.title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Label {
                Text(card.state).font(.title3.weight(.semibold))
            } icon: {
                Image(systemName: card.tone.symbol).accessibilityHidden(true)
            }
            .foregroundStyle(card.tone.tint)
            ForEach(card.lines, id: \.self) { line in
                Text(line)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
        .overlay {
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(.quaternary, lineWidth: 1)
        }
        .accessibilityElement(children: .combine)
    }
}

/// A table cell:  its words in the tone's colour, and why in the tooltip.
private struct OperatorCellView: View {
    let cell: OperatorCell

    var body: some View {
        Text(cell.text)
            .font(.system(size: 11, weight: cell.tone == .neutral ? .regular : .medium))
            .foregroundStyle(cell.tone == .neutral ? Color.secondary : cell.tone.tint)
            .lineLimit(1)
            .truncationMode(.tail)
            .help(cell.help)
    }
}

private enum OperatorColumns {
    static let seat: CGFloat = 120
    static let letter: CGFloat = 56
    static let host: CGFloat = 60
    static let state: CGFloat = 70
    static let wake: CGFloat = 110
    static let router: CGFloat = 120
    static let time: CGFloat = 150
}

private struct OperatorSeatHeader: View {
    var body: some View {
        HStack(spacing: 10) {
            header("Seat", OperatorColumns.seat)
            header("Letter", OperatorColumns.letter)
            header("Host", OperatorColumns.host)
            header("State", OperatorColumns.state)
            header("Wake", OperatorColumns.wake)
            header("Router", OperatorColumns.router)
            header("Last seen", nil)
        }
    }

    private func header(_ title: String, _ width: CGFloat?) -> some View {
        Text(title)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .leading)
            .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
    }
}

private struct OperatorSeatRowView: View {
    let row: OperatorSeatRow

    var body: some View {
        HStack(spacing: 10) {
            Text(row.seat)
                .font(.system(size: 11, design: .monospaced).weight(.semibold))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: OperatorColumns.seat, alignment: .leading)
            Text(row.letter)
                .font(.system(size: 11, design: .monospaced))
                .frame(width: OperatorColumns.letter, alignment: .leading)
            Text(row.host)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(width: OperatorColumns.host, alignment: .leading)
            OperatorCellView(cell: row.state)
                .frame(width: OperatorColumns.state, alignment: .leading)
            OperatorCellView(cell: row.wake)
                .frame(width: OperatorColumns.wake, alignment: .leading)
            OperatorCellView(cell: row.router)
                .frame(width: OperatorColumns.router, alignment: .leading)
            Text(row.lastSeen)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 2)
        .textSelection(.enabled)
    }
}

private struct OperatorWakeHeader: View {
    var body: some View {
        HStack(spacing: 10) {
            header("Time", OperatorColumns.time)
            header("Seat", OperatorColumns.seat)
            header("State", OperatorColumns.wake)
            header("Reason", nil)
        }
    }

    private func header(_ title: String, _ width: CGFloat?) -> some View {
        Text(title)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .leading)
            .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
    }
}

private struct OperatorWakeRowView: View {
    let row: OperatorWakeLine

    var body: some View {
        HStack(spacing: 10) {
            Text(row.time)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .frame(width: OperatorColumns.time, alignment: .leading)
            Text(row.seat)
                .font(.system(size: 11, design: .monospaced).weight(.semibold))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: OperatorColumns.seat, alignment: .leading)
            OperatorCellView(cell: row.state)
                .frame(width: OperatorColumns.wake, alignment: .leading)
            Text(row.reason)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 2)
        .textSelection(.enabled)
    }
}
