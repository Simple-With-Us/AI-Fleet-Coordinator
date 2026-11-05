import SwiftUI

/// The virtual machines panel:  what each fleet VM's own report says.
///
/// The panel reads the files under `~/.local/state/fleet-tools/vms/` and asks nothing
/// of the network:  each VM's collector made its report,  and the doctor's `--vms`
/// pull carried it here.  It shows the VM's load,  memory and disks,  its harness
/// processes and sessions,  and the shape of its checkouts.
///
/// A report that cannot be trusted reads as unknown,  which is information and not a
/// fault.  A report past forty-five minutes reads as unknown too:  its rows are old
/// evidence,  not current health.
///
/// The panel never runs the collector,  never pulls a report and never controls a VM:
/// telemetry is read-only by the owner's call,  so there are no buttons that act.
struct VirtualMachinesView: View {
    @State private var fleet: VmFleetReading?
    @State private var loading = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                if loading && fleet == nil {
                    Text("Reading the VMs' reports…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let fleet {
                    if !fleet.directoryExists || fleet.vms.isEmpty {
                        emptyState(fleet)
                    } else {
                        ForEach(fleet.vms) { reading in
                            vmCard(reading)
                        }
                    }
                } else {
                    Text("No reports yet.  Refresh to read them.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        .task { await load() }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Virtual Machines")
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
            Text("What each fleet VM's own report says about the machine, its harness processes and sessions, and its checkouts.")
                .font(.callout)
                .foregroundStyle(.secondary)
            if let fleet {
                Text(fleet.directoryPath)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }

    /// No reports at all:  say what the owner writes so the doctor starts pulling.
    ///
    /// A directory that exists but could not be listed is a different story:  the
    /// reports may be there,  so it shows its problem instead of setup steps.
    private func emptyState(_ fleet: VmFleetReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let problem = fleet.problem {
                problemBox(problem)
                Text("Refresh after fixing the directory, and any reports already pulled show here.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                problemBox(
                    "No VM reports are here yet.  The doctor pulls them with --vms from the VMs named in \(VmStatusContract.configPathText):  write {\"vms\": [{\"label\": \"<name>\", \"destination\": \"<ssh alias>\"}]} there (0600), give the pull key a forced-command line on each VM, and this panel fills in on the next doctor run.  docs/protocols/fleet-vm-status.md has the exact steps.")
                Text("Nothing is wrong with the VMs themselves:  this panel only knows what a pulled report says, and no report has been pulled.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: One VM

    private func vmCard(_ reading: VmStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            cardHeader(reading)
            if let problem = reading.problem {
                problemBox(problem)
            } else if !reading.isCurrent {
                problemBox(
                    "This report is stale (older than 45 minutes), so its rows are old evidence, not current health.  The VM reads as unknown until the doctor pulls a fresh report.")
            } else if let report = reading.report {
                hostRows(report)
                if !report.processes.isEmpty { processRows(report) }
                if !report.harnessSessions.isEmpty { sessionRows(report) }
                if !report.checkouts.isEmpty { checkoutRows(report) }
                if report.disks.isEmpty && report.processes.isEmpty && report.harnessSessions.isEmpty
                    && report.checkouts.isEmpty && report.loadText == nil && report.memory == nil
                {
                    Text("The report carries no measurements.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(14)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private func cardHeader(_ reading: VmStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 10) {
                Text(reading.displayName)
                    .font(.headline)
                FleetToolsStateBadge(state: reading.state, width: 92)
                if let report = reading.report, !report.seats.isEmpty {
                    Text(report.seats.joined(separator: ", "))
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if let age = reading.age {
                    Text("Report read \(age) ago")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(reading.isCurrent ? Color.secondary : Color.orange)
                }
                if let uptime = reading.report?.uptimeText {
                    Text("up \(uptime)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            Text(reading.path)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    private func hostRows(_ report: VmStatusReport) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if let load = report.loadText {
                let loadValues = [report.load1, report.load5, report.load15].compactMap { $0 }
                let loadIsValid = loadValues.count == 3 && loadValues.allSatisfy { $0.isFinite && $0 >= 0 }
                let cpus = report.cpuCount.flatMap { count in
                    count > 0 ? " on \(count) \(count == 1 ? "cpu" : "cpus")" : nil
                } ?? ""
                metricRow(
                    "Load", loadIsValid ? "\(load)\(cpus)" : "Unknown",
                    fraction: loadIsValid ? report.loadFraction : nil, driftAt: 1.0, missingAt: 2.0)
            }
            if let memory = report.memory {
                metricRow(
                    "Memory", memoryText(memory), fraction: memory.usedFraction)
            }
            if let swap = report.swap, let total = swap.totalBytes, total != 0 {
                metricRow("Swap", swapText(swap, total: total), fraction: swap.usedFraction)
            }
            ForEach(report.disks) { disk in
                metricRow(
                    disk.mount.isEmpty ? "disk" : disk.mount, diskText(disk),
                    fraction: diskHasImpossibleFree(disk) ? nil : disk.usedFraction)
            }
        }
    }

    private func memoryText(_ memory: HostMemory) -> String {
        if let total = memory.totalBytes, total <= 0 { return "Unknown" }
        if let available = memory.availableBytes, available < 0 { return "Unknown" }
        guard let total = memory.totalBytes, let available = memory.availableBytes else {
            if let total = memory.totalBytes {
                return "not read free of \(ServerStatusFormat.bytes(total))"
            }
            if let available = memory.availableBytes {
                return "\(ServerStatusFormat.bytes(available)) available; total not read"
            }
            return "not read"
        }
        guard available <= total else { return "Unknown" }
        return "\(ServerStatusFormat.bytes(available)) free of \(ServerStatusFormat.bytes(total))"
    }

    private func swapText(_ swap: HostSwap, total: Int) -> String {
        guard total > 0 else { return "Unknown" }
        guard let free = swap.freeBytes else {
            return "not read free of \(ServerStatusFormat.bytes(total))"
        }
        guard free >= 0, free <= total else { return "Unknown" }
        return "\(ServerStatusFormat.bytes(free)) free of \(ServerStatusFormat.bytes(total))"
    }

    private func diskText(_ disk: HostDisk) -> String {
        if diskHasImpossibleFree(disk) { return "Unknown" }
        guard let free = disk.freeBytes else { return "not read" }
        if let total = disk.totalBytes {
            return "\(ServerStatusFormat.bytes(free)) free of \(ServerStatusFormat.bytes(total))"
        }
        return "\(ServerStatusFormat.bytes(free)) free"
    }

    private func diskHasImpossibleFree(_ disk: HostDisk) -> Bool {
        if let total = disk.totalBytes, total <= 0 { return true }
        if let free = disk.freeBytes, free < 0 { return true }
        if let total = disk.totalBytes, let free = disk.freeBytes, free > total { return true }
        return false
    }

    private func processRows(_ report: VmStatusReport) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            subHeader("Harness processes")
            // Rows are identified by position, not name:  a tolerated malformed
            // report can carry two rows with the same (or no) name,  and duplicate
            // ForEach identities misrender.
            ForEach(Array(report.processes.enumerated()), id: \.offset) { _, process in
                HStack(spacing: 10) {
                    Text(process.name)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .frame(width: 140, alignment: .leading)
                        .lineLimit(1)
                    Text(process.count.map { "×\($0)" } ?? "count not read")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if let oldest = process.oldestText {
                        Text("oldest \(oldest) ago")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private func sessionRows(_ report: VmStatusReport) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            subHeader("Harness sessions")
            ForEach(report.harnessSessions) { sessions in
                HStack(spacing: 10) {
                    Text(sessions.harness)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .frame(width: 140, alignment: .leading)
                        .lineLimit(1)
                    Text(sessions.sessionFileCount.map { "\($0) session files" } ?? "count not read")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if let newest = sessions.newestText {
                        Text("newest \(newest) ago")
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private func checkoutRows(_ report: VmStatusReport) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            subHeader("Checkouts")
            ForEach(Array(report.checkouts.enumerated()), id: \.offset) { _, checkout in
                HStack(spacing: 10) {
                    Text(checkout.name)
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .frame(width: 140, alignment: .leading)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(checkout.branch ?? "branch not read")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if let head = checkout.headShort {
                        Text(head)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if let dirty = checkout.dirtyText {
                        Text(dirty)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    if checkout.showsUnpushed {
                        Text("UNPUSHED")
                            .font(.system(size: 10, weight: .bold, design: .monospaced))
                            .foregroundStyle(.orange)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    // MARK: The pieces

    private func subHeader(_ title: String) -> some View {
        Text(title)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.secondary)
    }

    /// Why a report could not be read,  or cannot be believed.
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

    /// One measured number,  with a bar when there is a share to draw.
    ///
    /// The tint cutoffs are the metric's own:  shares of a whole (memory,  swap,
    /// disks) turn at 0.85 and 0.95,  while load is graded at 1.0 and 2.0,  the
    /// same cutoffs `VmStatusReport.state` uses,  so the bar never outruns its badge.
    private func metricRow(
        _ label: String, _ text: String, fraction: Double?,
        driftAt: Double = 0.85, missingAt: Double = 0.95
    ) -> some View {
        HStack(spacing: 10) {
            Text(label)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .frame(width: 140, alignment: .leading)
            usageBar(fraction, driftAt: driftAt, missingAt: missingAt)
            if let fraction {
                Text(String(format: "%.0f%%", fraction * 100))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .frame(width: 44, alignment: .trailing)
            }
            Text(text)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 1)
    }

    private func usageBar(_ fraction: Double?, driftAt: Double, missingAt: Double) -> some View {
        let width: CGFloat = 160
        let share = min(max(fraction ?? 0, 0), 1)
        let tint: Color = {
            guard let fraction else { return .secondary }
            if fraction >= missingAt { return .red }
            if fraction >= driftAt { return .orange }
            return .green
        }()
        return ZStack(alignment: .leading) {
            Capsule()
                .fill(Color.secondary.opacity(0.15))
            Capsule()
                .fill(tint)
                .frame(width: fraction == nil ? 0 : max(2, width * share))
        }
        .frame(width: width, height: 6)
        .accessibilityHidden(true)
    }

    private func load() async {
        loading = true
        defer { loading = false }
        let url = VmStatusReportReader.directoryURL()
        fleet = await Task.detached(priority: .userInitiated) {
            VmStatusReportReader.readDirectory(at: url)
        }.value
    }
}
