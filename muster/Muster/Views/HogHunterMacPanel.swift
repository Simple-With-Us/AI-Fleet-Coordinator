import SwiftUI

/// What Hog Hunter says about this Mac:  load,  CPU,  memory,  swap,  disks,  the busiest
/// processes,  the last cleanup,  the housekeeper lock and the cleaning regimen.
///
/// The panel reads one local file (`~/.local/state/fleet-tools/hoghunter-mac.json`) and offers
/// no control of Hog Hunter at all.  Hog Hunter closed,  or a file that fails the ownership
/// check,  reads as unknown with a sentence that says so.
struct HogHunterMacPanel: View {
    @State private var reading: HogHunterMacReading?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text("Hog Hunter On This Mac")
                    .font(.headline)
                if let reading {
                    FleetToolsStateBadge(state: reading.state, width: 92)
                }
                Spacer()
                if let age = reading?.age, reading?.isCurrent == true {
                    Text("Report read \(age) ago")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            if let reading {
                Text(reading.path)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let problem = reading.problem {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "info.circle")
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(problem)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                } else {
                    rows(reading)
                    hogs(reading)
                }
            } else {
                Text("Reading the Hog Hunter report…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .task {
            // The report is small and local:  read it again every half minute.
            while !Task.isCancelled {
                let read = await Task.detached(priority: .utility) { HogHunterMacReportReader.readDefault() }.value
                reading = read
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
    }

    private func rows(_ reading: HogHunterMacReading) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(reading.rows) { row in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(row.label)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .frame(width: 150, alignment: .leading)
                    Text(row.value)
                        .font(.system(size: 12, design: .monospaced))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    if let state = row.state {
                        Circle()
                            .fill(state.tint)
                            .frame(width: 7, height: 7)
                            .accessibilityLabel(state.shortLabel)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
        .padding(.horizontal, 12)
    }

    @ViewBuilder
    private func hogs(_ reading: HogHunterMacReading) -> some View {
        let hogs = reading.hogs
        VStack(alignment: .leading, spacing: 3) {
            Text("Busiest Processes")
                .font(.subheadline.weight(.semibold))
                .padding(.top, 4)
            if reading.processesMalformed {
                Text("Not proven.\u{00A0} The report's process list could not be read.")
                    .font(.callout)
                    .foregroundStyle(.orange)
            } else if reading.processes == nil {
                Text("Not reported")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if hogs.isEmpty {
                Text("No processes listed")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            ForEach(Array(hogs.enumerated()), id: \.offset) { _, process in
                HStack(spacing: 10) {
                    Text(process.displayName)
                        .font(.system(size: 12, design: .monospaced))
                        .lineLimit(1)
                        .frame(width: 220, alignment: .leading)
                    Text(process.cpuText ?? "—")
                        .font(.system(size: 12, design: .monospaced))
                        .frame(width: 70, alignment: .trailing)
                    Text(process.memoryText ?? "—")
                        .font(.system(size: 12, design: .monospaced))
                        .frame(width: 90, alignment: .trailing)
                    Spacer(minLength: 0)
                }
            }
        }
        .padding(.horizontal, 12)
    }
}
