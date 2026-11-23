import AppKit
import SwiftUI

/// The Bearers panel:  every identity's bearer as the Worker's owner status API reported it.
///
/// It reads one file and runs nothing.  The file is the Mac poll's copy of the Worker's answer, so a
/// stopped poll, a lapsed Cloudflare Access login or a Worker that did not answer shows as not current
/// with the reason, never as healthy.
///
/// Mint, rotate and revoke run the Worker's owner action API as the owner (`BearerActions.swift`), each
/// after a confirmation.  A new bearer is shown once in a sheet and kept nowhere:  not on disk, not in a
/// log, not in the pasteboard unless the owner presses Copy.
struct BearersView: View {
    @State private var reading: BearerStatusReading?
    @State private var readAt: Date?
    @State private var pending: PendingAction?
    @State private var running = false
    @State private var resultLine: String?
    @State private var shownBearer: ShownBearer?

    private struct PendingAction: Identifiable {
        let action: BearerAction
        let row: BearerStatusRow
        var id: String { action.rawValue + row.identity }
    }

    private struct ShownBearer: Identifiable {
        let identity: String
        let bearer: String
        let expiresAt: String?
        var id: String { identity }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if let reading {
                    if let resultLine { Text(resultLine).font(.callout).textSelection(.enabled) }
                    if let problem = reading.problem { problemBox(problem) }
                    if !reading.isCurrent && reading.problem == nil {
                        problemBox("The poll's last answer is older than fifteen minutes, so these rows are not current.")
                    }
                    if !reading.alarms.isEmpty { alarms(reading.alarms) }
                    if reading.rows.isEmpty {
                        Text("No bearer rows were read.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } else {
                        rows(reading)
                    }
                } else {
                    Text("Reading the bearer status\u{2026}")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .confirmationDialog(confirmTitle, isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } }),
                            titleVisibility: .visible, presenting: pending) { item in
            Button(item.action.verb + " " + item.row.identity, role: item.action == .mint ? nil : .destructive) { run(item) }
            Button("Cancel", role: .cancel) {}
        } message: { item in
            Text(confirmMessage(item))
        }
        .sheet(item: $shownBearer) { shown in
            BearerShownSheet(shown: shown.bearer, identity: shown.identity, expiresAt: shown.expiresAt) { shownBearer = nil }
        }
        .rereadsOnTickAndWake {
            reading = BearerStatusReader.read()
            readAt = Date()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(MusterDestination.bearers.title)
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    reading = BearerStatusReader.read()
                    readAt = Date()
                } label: {
                    Label("Read The Mac's Copy Again", systemImage: "arrow.clockwise")
                }
                .help("Reads bearer-status.json again.\u{00A0} It asks the Worker nothing.")
            }
            Text("Each identity's bearer as the Worker last reported it to the Mac's poll.  Names, kinds, times and counts only.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let generatedAt = reading?.generatedAt {
                Text("Poll answer from \(generatedAt.formatted(date: .abbreviated, time: .standard))")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func alarms(_ alarms: [BearerStatusAlarm]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Alarms In Force").font(.headline)
            ForEach(alarms) { alarm in
                Text(alarm.summary)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.red)
            }
        }
    }

    private func rows(_ reading: BearerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(reading.rows) { row in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Image(systemName: row.isHealthy && reading.isCurrent ? "checkmark.circle.fill" : "exclamationmark.circle")
                        .foregroundStyle(row.isHealthy && reading.isCurrent ? Color.green : Color.orange)
                        .accessibilityHidden(true)
                    Text(row.identity).font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .frame(width: 130, alignment: .leading)
                    Text(reading.isCurrent ? row.verdict : "not known").font(.callout)
                        .frame(width: 170, alignment: .leading)
                    Text(detail(row)).font(.caption).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    actionButtons(row, current: reading.isCurrent)
                }
                .padding(.vertical, 6)
                Divider()
            }
        }
    }

    private var confirmTitle: String {
        guard let pending else { return "" }
        return pending.action.verb + " the bearer for " + pending.row.identity + "?"
    }

    private func confirmMessage(_ item: PendingAction) -> String {
        switch item.action {
        case .mint: return "A new bearer is shown once.  It is not stored here."
        case .rotate: return "The live bearer stops working as soon as the new one is minted.  The seat needs the new one."
        case .revoke: return "The live bearer stops working now.  The seat is locked out until a new one is minted."
        }
    }

    /// Mint needs no live bearer and rotate and revoke need one;  the Worker refuses the rest, and a row with
    /// missing credential records cannot mint or rotate, so those buttons are off and say which records.
    @ViewBuilder
    private func actionButtons(_ row: BearerStatusRow, current: Bool) -> some View {
        let live = row.active && !row.expired
        let blocked = !row.missingRecords.isEmpty
        HStack(spacing: 6) {
            Button(live ? "Rotate" : "Mint") { pending = PendingAction(action: live ? .rotate : .mint, row: row) }
                .disabled(running || !current || blocked)
                .help(blocked ? "Missing records:  " + row.missingRecords.joined(separator: ", ")
                              : (current ? "Mints a new bearer and shows it once." : "The poll's answer is not current."))
            if live {
                Button("Revoke", role: .destructive) { pending = PendingAction(action: .revoke, row: row) }
                    .disabled(running || !current)
            }
        }
        .controlSize(.small)
    }

    private func run(_ item: PendingAction) {
        running = true
        resultLine = "Running " + item.action.verb.lowercased() + " for " + item.row.identity + "\u{2026}"
        Task { @MainActor in
            // A mint needs a value (the Worker refuses a seat mint without one);  a rotate passes the live
            // bearer's own value through, nil when it is not known, and the Worker keeps it.
            let request = BearerActionRequest.make(item.action, identity: item.row.identity,
                                                   mcpAccept: BearerActionRequest.mcpAccept(for: item.action, live: item.row.mcpAccept))
            let outcome = await BearerActionRunner(transport: LiveBearerActionTransport()).run(request)
            resultLine = outcome.summary
            if case .minted(let identity, let bearer, let expiresAt) = outcome {
                shownBearer = ShownBearer(identity: identity, bearer: bearer, expiresAt: expiresAt)
            }
            running = false
        }
    }

    private func detail(_ row: BearerStatusRow) -> String {
        var parts: [String] = []
        if let kind = row.kind { parts.append(kind) }
        if let expiresAt = row.expiresAt { parts.append("expires \(expiresAt)") }
        if let accept = row.mcpAccept { parts.append(accept ? "MCP accepted" : "MCP not accepted") }
        if row.paused == true { parts.append("paused") }
        if !row.missingRecords.isEmpty { parts.append("missing records: " + row.missingRecords.joined(separator: ", ")) }
        if let count = row.mismatchCount, count > 0 { parts.append("\(count) mismatches") }
        return parts.joined(separator: " \u{00B7} ")
    }

    private func problemBox(_ text: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text(text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.10))
    }
}

/// A new bearer, shown once.  Closing the sheet drops it;  nothing writes it anywhere.
private struct BearerShownSheet: View {
    let shown: String
    let identity: String
    let expiresAt: String?
    let done: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New bearer for \(identity)").font(.title3.weight(.semibold))
            Text("This is the only time it is shown.  The Worker keeps a hash.  Copy it to where the seat reads it, then close this."
                 + (expiresAt.map { "  Expires \($0)." } ?? ""))
                .font(.callout)
                .foregroundStyle(.secondary)
            Text(shown)
                .font(.system(size: 11, design: .monospaced))
                .textSelection(.enabled)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.secondary.opacity(0.12))
            HStack {
                Button("Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(shown, forType: .string)
                }
                Spacer()
                Button("Close", action: done).keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 520)
    }
}
