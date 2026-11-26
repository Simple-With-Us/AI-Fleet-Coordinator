import SwiftUI

/// The Operator panel's Wake Router section:  each seat the one wake router serves, whether its
/// routine is installed, whether its delivery is on or off, and the router's reason word.  Read
/// only:  the router's switches are knobs, and their home is Controls.
struct OperatorRouterSeatsSection: View {
    let reading: OperatorReading<OperatorRouterHealthDocument>?

    var body: some View {
        SectionPanel(title: "Wake Router Seats", subtitle: "What the router itself says, seat by seat.") {
            switch reading {
            case nil:
                EmptyNote(text: "Reading the wake router\u{2026}")
            case .unreachable(let why)?, .unavailable(let why)?:
                EmptyNote(text: why)
            case .read(let document)?:
                let lines = OperatorPanelPresentation.routerSeatLines(document)
                if lines.isEmpty {
                    EmptyNote(text: "The router serves no seat.")
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        OperatorRouterSeatHeader()
                        Divider()
                        ForEach(lines) { line in
                            OperatorRouterSeatRowView(line: line)
                        }
                        if document.truncated == true {
                            Text("The router serves more seats than the server lists here.")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                    }
                }
            }
        }
    }
}

/// The Operator panel's Held Back section:  for each seat with wakes held back or notes waiting
/// for the owner, how many, why, and how old.  Read only:  nothing here releases, replays or
/// dismisses anything.
struct OperatorQueuesSection: View {
    let wakes: OperatorReading<OperatorWakesDocument>?
    let now: Date

    var body: some View {
        SectionPanel(title: "Held Back And Waiting For The Owner",
                     subtitle: "Wakes the Operator held back, and notes waiting for you, by seat.") {
            switch wakes {
            case nil:
                EmptyNote(text: "Reading the wake record\u{2026}")
            case .unreachable(let why)?, .unavailable(let why)?:
                EmptyNote(text: why)
            case .read(let document)?:
                let lines = OperatorPanelPresentation.queueLines(document, now: now)
                if lines.isEmpty {
                    EmptyNote(text: "No seat has wakes held back or notes waiting for you.")
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        OperatorQueueHeader()
                        Divider()
                        ForEach(lines) { line in
                            OperatorQueueRowView(line: line)
                        }
                        Text("The oldest held wake is the oldest of the rows the server lists, at most ten a seat.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                if !document.unreadable.isEmpty {
                    Text("The wake record of \(document.unreadable.joined(separator: ", ")) could not be read, so this list is a floor.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if document.truncated == true {
                    Text("The server lists more seats here than it sends, so this list is a floor.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

private enum OperatorRouterColumns {
    static let seat: CGFloat = 105
    static let installed: CGFloat = 110
    static let delivery: CGFloat = 90
    static let health: CGFloat = 100
    static let held: CGFloat = 80
    static let reasons: CGFloat = 220
    static let oldest: CGFloat = 90
    static let owner: CGFloat = 70
}

private func operatorRouterHeader(_ title: String, _ width: CGFloat?) -> some View {
    Text(title)
        .font(.caption2.weight(.semibold))
        .foregroundStyle(.secondary)
        .frame(width: width, alignment: .leading)
        .frame(maxWidth: width == nil ? .infinity : nil, alignment: .leading)
}

private func operatorRouterCell(_ cell: OperatorCell, width: CGFloat) -> some View {
    let tint: Color
    switch cell.tone {
    case .good: tint = .green
    case .warning: tint = .orange
    case .bad: tint = .red
    case .neutral: tint = .secondary
    }
    return Text(cell.text)
        .font(.system(size: 11, weight: cell.tone == .neutral ? .regular : .medium))
        .foregroundStyle(tint)
        .lineLimit(1)
        .help(cell.help)
        .frame(width: width, alignment: .leading)
}

private struct OperatorRouterSeatHeader: View {
    var body: some View {
        HStack(spacing: 8) {
            operatorRouterHeader("Seat", OperatorRouterColumns.seat)
            operatorRouterHeader("Routine", OperatorRouterColumns.installed)
            operatorRouterHeader("Delivery", OperatorRouterColumns.delivery)
            operatorRouterHeader("Health", OperatorRouterColumns.health)
            operatorRouterHeader("Router's reason", nil)
        }
    }
}

private struct OperatorRouterSeatRowView: View {
    let line: OperatorRouterSeatLine

    var body: some View {
        HStack(spacing: 8) {
            Text(line.seat)
                .font(.system(size: 11, design: .monospaced).weight(.semibold))
                .lineLimit(1)
                .frame(width: OperatorRouterColumns.seat, alignment: .leading)
            operatorRouterCell(line.installed, width: OperatorRouterColumns.installed)
            operatorRouterCell(line.delivery, width: OperatorRouterColumns.delivery)
            operatorRouterCell(line.health, width: OperatorRouterColumns.health)
            Text(line.reason)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 2)
        .textSelection(.enabled)
    }
}

private struct OperatorQueueHeader: View {
    var body: some View {
        HStack(spacing: 8) {
            operatorRouterHeader("Seat", OperatorRouterColumns.seat)
            operatorRouterHeader("Held back", OperatorRouterColumns.held)
            operatorRouterHeader("Why", OperatorRouterColumns.reasons)
            operatorRouterHeader("Oldest held", OperatorRouterColumns.oldest)
            operatorRouterHeader("For you", OperatorRouterColumns.owner)
            operatorRouterHeader("Newest notes", nil)
        }
    }
}

private struct OperatorQueueRowView: View {
    let line: OperatorQueueLine

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(line.seat)
                .font(.system(size: 11, design: .monospaced).weight(.semibold))
                .lineLimit(1)
                .frame(width: OperatorRouterColumns.seat, alignment: .leading)
            Text(line.heldBack)
                .font(.system(size: 11, weight: line.tone == .warning ? .medium : .regular))
                .foregroundStyle(line.tone == .warning ? Color.orange : Color.secondary)
                .frame(width: OperatorRouterColumns.held, alignment: .leading)
            Text(line.reasons)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(width: OperatorRouterColumns.reasons, alignment: .leading)
            Text(line.oldestHeld)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .frame(width: OperatorRouterColumns.oldest, alignment: .leading)
            Text(line.ownerQueue)
                .font(.system(size: 11))
                .frame(width: OperatorRouterColumns.owner, alignment: .leading)
            Text(line.ownerRows)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 2)
        .textSelection(.enabled)
    }
}
