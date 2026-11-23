import SwiftUI

/// The Bearers panel:  every identity's bearer as the Worker's owner status API reported it.
///
/// It reads one file and runs nothing.  The file is the Mac poll's copy of the Worker's answer, so a
/// stopped poll, a lapsed Cloudflare Access login or a Worker that did not answer shows as not current
/// with the reason, never as healthy.  Mint, rotate and revoke are not offered here:  the Worker's owner
/// action API takes them behind the owner's Access login, and no Muster route to it has been reviewed yet.
struct BearersView: View {
    @State private var reading: BearerStatusReading?
    @State private var readAt: Date?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if let reading {
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
                }
                .padding(.vertical, 6)
                Divider()
            }
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
