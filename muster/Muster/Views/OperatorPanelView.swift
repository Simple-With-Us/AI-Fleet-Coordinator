import AppKit
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
/// section, which goes Show Plan, then Touch ID, through the shared action sheet.
struct OperatorPanelView: View {
    /// Opens the Controls panel, where the Operator's knobs are.
    var openControls: () -> Void = {}

    @ObservedObject private var store = OperatorPanelStore.shared
    @State private var seatGrouping: OperatorSeatGrouping = .byPlatform
    @State private var seatFilter: OperatorSeatFilter = .all
    @State private var seatSearch: String = ""
    @State private var showAllSeats: Bool = false
    @State private var collapsedSeatGroups: Set<String> = []
    @State private var wakeGrouping: OperatorWakeGrouping = .newestFirst
    @State private var wakeFilter: OperatorWakeFilter = .all
    @State private var wakeSearch: String = ""
    @State private var showAllWakes: Bool = false
    @State private var collapsedWakeSeats: Set<String> = []

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
                    TimelineView(.periodic(from: .now, by: 10)) { context in
                        VStack(alignment: .leading, spacing: 14) {
                            header(now: context.date)
                            readings(now: context.date)
                        }
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

    private func header(now: Date) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(OperatorPanelCopy.heading)
                .font(.title2.weight(.semibold))
            Spacer()
            Text(headerAge(now: now))
                .font(.caption)
                .foregroundStyle(.secondary)
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
                let table = OperatorPanelPresentation.seatsTable(
                    document,
                    status: store.status,
                    now: now,
                    limit: showAllSeats ? Int.max : OperatorPanelPresentation.seatRowLimit,
                    filter: seatFilter,
                    search: seatSearch,
                    grouping: seatGrouping
                )
                VStack(alignment: .leading, spacing: 8) {
                    seatsToolbar(table: table)
                    if table.rows.isEmpty {
                        if document.sessions.isEmpty {
                            EmptyNote(text: "The registry holds no chats.")
                        } else {
                            HStack(spacing: 8) {
                                Text("No chats match the current filter.")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                Button("Reset Filters") {
                                    seatFilter = .all
                                    seatSearch = ""
                                }
                                .buttonStyle(.link)
                            }
                        }
                    } else if seatGrouping == .flat {
                        VStack(alignment: .leading, spacing: 4) {
                            OperatorSeatHeader(showPlatform: true)
                            Divider()
                            ForEach(table.rows) { OperatorSeatRowView(row: $0, showPlatform: true) }
                        }
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(table.groups) { group in
                                seatGroupView(group)
                            }
                        }
                    }
                    if !showAllSeats && table.moreCount > 0 {
                        Button("Show All \(table.totalMatchingCount) Chats (\(table.moreCount) More)") {
                            showAllSeats = true
                        }
                        .font(.caption.weight(.medium))
                        .buttonStyle(.link)
                    } else if showAllSeats && table.totalMatchingCount > OperatorPanelPresentation.seatRowLimit {
                        Button("Show First \(OperatorPanelPresentation.seatRowLimit)") {
                            showAllSeats = false
                        }
                        .font(.caption.weight(.medium))
                        .buttonStyle(.link)
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
    }

    private func seatsToolbar(table: OperatorSeatsTable) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                MusterSearchField(prompt: "Filter seat, platform, letter, or issue\u{2026}", text: $seatSearch)
                Picker("Filter", selection: $seatFilter) {
                    ForEach(OperatorSeatFilter.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
            HStack(spacing: 8) {
                Picker("Group", selection: $seatGrouping) {
                    ForEach(OperatorSeatGrouping.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                Spacer()
                if table.totalMatchingCount > 0 {
                    Text(seatGrouping == .flat
                         ? "Showing \(table.rows.count) of \(table.totalMatchingCount)"
                         : "Showing \(table.rows.count) of \(table.totalMatchingCount) across \(table.groups.count) groups")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if seatGrouping != .flat && !table.groups.isEmpty {
                    let allCollapsed = table.groups.allSatisfy { collapsedSeatGroups.contains($0.id) }
                    Button(allCollapsed ? "Expand All" : "Collapse All") {
                        if allCollapsed {
                            collapsedSeatGroups.removeAll()
                        } else {
                            collapsedSeatGroups = Set(table.groups.map(\.id))
                        }
                    }
                    .font(.caption)
                    .buttonStyle(.link)
                }
            }
        }
    }

    private func seatGroupView(_ group: OperatorSeatGroup) -> some View {
        let isCollapsed = collapsedSeatGroups.contains(group.id)
        let showPlatform = (seatGrouping != .byPlatform)
        return VStack(alignment: .leading, spacing: 6) {
            MusterGroupHeaderButton(
                title: group.title,
                isExpanded: !isCollapsed,
                summary: group.seatsSummary,
                badges: seatGroupBadges(group)
            ) {
                if isCollapsed {
                    collapsedSeatGroups.remove(group.id)
                } else {
                    collapsedSeatGroups.insert(group.id)
                }
            }
            if !isCollapsed {
                VStack(alignment: .leading, spacing: 4) {
                    OperatorSeatHeader(showPlatform: showPlatform)
                    Divider()
                    ForEach(group.rows) { OperatorSeatRowView(row: $0, showPlatform: showPlatform) }
                }
                .padding(.leading, 14)
            }
        }
    }

    private func seatGroupBadges(_ group: OperatorSeatGroup) -> [MusterBadgeSpec] {
        var badges: [MusterBadgeSpec] = []
        if group.liveCount > 0 {
            badges.append(MusterBadgeSpec(text: "\(group.liveCount) live", color: .green))
        }
        if group.goneCount > 0 {
            badges.append(MusterBadgeSpec(text: "\(group.goneCount) gone", color: .secondary))
        }
        if group.problemCount > 0 {
            badges.append(MusterBadgeSpec(text: "\(group.problemCount) attention", color: .orange))
        }
        return badges
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
                let table = OperatorPanelPresentation.wakesTable(
                    document,
                    now: now,
                    limit: showAllWakes ? Int.max : OperatorPanelPresentation.wakeRowLimit,
                    filter: wakeFilter,
                    search: wakeSearch
                )
                VStack(alignment: .leading, spacing: 8) {
                    wakesToolbar(table: table)
                    if table.rows.isEmpty {
                        let anyLoaded = document.seats.contains { !$0.recent.isEmpty }
                        if !anyLoaded {
                            EmptyNote(text: "No wakes in the last 24 hours.")
                        } else {
                            HStack(spacing: 8) {
                                Text("No wakes match the current filter.")
                                    .font(.callout)
                                    .foregroundStyle(.secondary)
                                Button("Reset Filters") {
                                    wakeFilter = .all
                                    wakeSearch = ""
                                }
                                .buttonStyle(.link)
                            }
                        }
                    } else if wakeGrouping == .newestFirst {
                        VStack(alignment: .leading, spacing: 4) {
                            OperatorWakeHeader()
                            Divider()
                            ForEach(table.rows) { OperatorWakeRowView(row: $0) }
                        }
                    } else {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(table.seatGroups) { group in
                                wakeGroupView(group)
                            }
                        }
                    }
                    if !showAllWakes && table.loadedMoreCount > 0 {
                        Button("Show All \(table.totalLoadedCount) Loaded Wakes (\(table.loadedMoreCount) More)") {
                            showAllWakes = true
                        }
                        .font(.caption.weight(.medium))
                        .buttonStyle(.link)
                    } else if showAllWakes && table.totalLoadedCount > OperatorPanelPresentation.wakeRowLimit {
                        Button("Show First \(OperatorPanelPresentation.wakeRowLimit)") {
                            showAllWakes = false
                        }
                        .font(.caption.weight(.medium))
                        .buttonStyle(.link)
                    }
                    if table.unloadedServerCount > 0 {
                        Text("\(table.unloadedServerCount) older wakes on the server are counted in the total and not included in this list.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
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
    }

    private func wakesToolbar(table: OperatorWakesTable) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                MusterSearchField(prompt: "Filter seat, state, or reason\u{2026}", text: $wakeSearch)
                Picker("Filter", selection: $wakeFilter) {
                    ForEach(OperatorWakeFilter.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
            HStack(spacing: 8) {
                Picker("Group", selection: $wakeGrouping) {
                    ForEach(OperatorWakeGrouping.allCases) { option in
                        Text(option.label).tag(option)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                Spacer()
                if table.totalLoadedCount > 0 {
                    Text("Showing \(table.rows.count) of \(table.totalLoadedCount) loaded")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if wakeGrouping == .bySeat && !table.seatGroups.isEmpty {
                    let allCollapsed = table.seatGroups.allSatisfy { collapsedWakeSeats.contains($0.id) }
                    Button(allCollapsed ? "Expand All" : "Collapse All") {
                        if allCollapsed {
                            collapsedWakeSeats.removeAll()
                        } else {
                            collapsedWakeSeats = Set(table.seatGroups.map(\.id))
                        }
                    }
                    .font(.caption)
                    .buttonStyle(.link)
                }
            }
        }
    }

    private func wakeGroupView(_ group: OperatorWakeSeatGroup) -> some View {
        let isCollapsed = collapsedWakeSeats.contains(group.id)
        var badges = [MusterBadgeSpec(text: "\(group.rows.count)", color: .secondary)]
        if group.problemCount > 0 {
            badges.append(MusterBadgeSpec(text: "\(group.problemCount) stopped", color: .orange))
        }
        return VStack(alignment: .leading, spacing: 6) {
            MusterGroupHeaderButton(
                title: group.seat,
                isExpanded: !isCollapsed,
                summary: nil,
                badges: badges
            ) {
                if isCollapsed {
                    collapsedWakeSeats.remove(group.id)
                } else {
                    collapsedWakeSeats.insert(group.id)
                }
            }
            if !isCollapsed {
                VStack(alignment: .leading, spacing: 4) {
                    OperatorWakeHeader()
                    Divider()
                    ForEach(group.rows) { OperatorWakeRowView(row: $0) }
                }
                .padding(.leading, 14)
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
    static let seat: CGFloat = 105
    static let letter: CGFloat = 52
    static let platform: CGFloat = 76
    static let host: CGFloat = 50
    static let state: CGFloat = 66
    static let wake: CGFloat = 98
    static let router: CGFloat = 108
    static let time: CGFloat = 140
}

private struct OperatorSeatHeader: View {
    let showPlatform: Bool

    var body: some View {
        HStack(spacing: 8) {
            header("Seat", OperatorColumns.seat)
            header("Letter", OperatorColumns.letter)
            if showPlatform {
                header("Platform", OperatorColumns.platform)
            }
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
    let showPlatform: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text(row.seat)
                    .font(.system(size: 11, design: .monospaced).weight(.semibold))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(width: OperatorColumns.seat, alignment: .leading)
                Text(row.letter)
                    .font(.system(size: 11, design: .monospaced))
                    .frame(width: OperatorColumns.letter, alignment: .leading)
                if showPlatform {
                    Text(row.platform)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .frame(width: OperatorColumns.platform, alignment: .leading)
                }
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
            if !row.issues.isEmpty {
                Text("Issues: " + row.issues.joined(separator: " · "))
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .padding(.leading, 8)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
        .textSelection(.enabled)
        .contextMenu {
            Button("Copy Seat & Letter") {
                MusterClipboard.copy("\(row.seat) \(row.letter) (\(row.platform), \(row.host))")
            }
            Button("Copy Seat Name") {
                MusterClipboard.copy(row.seat)
            }
        }
    }
}

private struct OperatorWakeHeader: View {
    var body: some View {
        HStack(spacing: 8) {
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
        HStack(spacing: 8) {
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
        .contextMenu {
            Button("Copy Wake Summary") {
                MusterClipboard.copy("\(row.time) \(row.seat) — \(row.state.text): \(row.reason)")
            }
            Button("Copy Seat Name") {
                MusterClipboard.copy(row.seat)
            }
        }
    }
}

// MARK: - Shared UI controls

enum MusterClipboard {
    static func copy(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }
}

struct MusterSearchField: View {
    let prompt: String
    @Binding var text: String

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "magnifyingglass")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextField(prompt, text: $text)
                .textFieldStyle(.roundedBorder)
            if !text.isEmpty {
                Button {
                    text = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Clear filter text")
            }
        }
    }
}

struct MusterBadgeSpec: Hashable {
    let text: String
    let color: Color
}

struct MusterCountBadge: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .foregroundStyle(color == .secondary ? Color.primary.opacity(0.75) : color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: Capsule())
    }
}

struct MusterGroupHeaderButton: View {
    let title: String
    let isExpanded: Bool
    let summary: String?
    let badges: [MusterBadgeSpec]
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 8) {
                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .frame(width: 10)
                Text(title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                ForEach(badges, id: \.self) { badge in
                    MusterCountBadge(text: badge.text, color: badge.color)
                }
                if let summary, !summary.isEmpty {
                    Text(summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                Spacer()
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 6))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
