import SwiftUI

/// The server status panel:  what the server's own report says.
///
/// The panel reads one file and asks nothing of the network:  the collector's report
/// at `~/.local/state/fleet-tools/server-status.json`.  It shows how Coolify is
/// working,  the busiest processes on the host,  and the watched config files whose
/// drift is not `unchanged`.
///
/// A report that cannot be trusted reads as unknown,  which is information and not a
/// fault,  so a collector that could not run shows as an unknown panel and not as a
/// broken server.
///
/// The panel never runs the collector and never writes the report.  Its buttons start an
/// action through `OpsController`,  which plans it,  shows the plan and asks Touch ID before
/// anything runs on the server:  a unit the report marks `controllable` gets Start,  Stop and
/// Restart,  and a Coolify application whose uuid came from a Coolify label gets Redeploy when
/// the allowlist allows redeploys (the same rule the server applies before it plans one).
///
/// The report is the one the menu reads,  held by `PlaceWatch`,  and the state at the top
/// is the menu's own words for the server,  so the panel and the menu cannot disagree.
struct ServerStatusPanel: View {
    @EnvironmentObject private var watch: PlaceWatch
    @EnvironmentObject private var admin: FleetAdmin
    @EnvironmentObject private var monitor: DriftMonitor
    @EnvironmentObject private var router: ServiceActionRouter
    @ObservedObject private var controller = OpsController.shared

    /// The allowlist in the Mac's checkout,  which decides the buttons.  It is the router's,
    /// which the menu reads too.
    private var allowlist: OpsAllowlist? { router.allowlist }

    var body: some View {
        let menu = MachineMenu(MachineMenuInputs(admin: admin, monitor: monitor, watch: watch, router: router))
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                stateLine(menu.serverCard)
                if let reading = watch.status {
                    pathLine(reading)
                    if let problem = reading.problem {
                        problemBox(problem)
                    } else if !reading.isCurrent {
                        problemBox("This report is not current, so its rows are not current health.\u{00A0} The last known units are below, with the repairs the allowlist names.")
                        lastKnownSection(reading)
                    } else {
                        if reading.hostMetrics != nil {
                            hostSection(reading)
                        }
                        if reading.hasServicesHalf {
                            servicesSection(reading)
                        }
                        coolifySection(reading)
                        processesSection(reading)
                        configSection(reading)
                    }
                }
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 18)
        }
        // A finished action changes what the server reports,  and `PlaceWatch` reads the
        // report again after every run,  so the panel follows it with nothing of its own.
        .task {
            router.reloadAllowlist()
            watch.readNow()
        }
    }

    // MARK: The header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(MusterDestination.serverStatus.title)
                    .font(.largeTitle.weight(.semibold))
                Spacer()
                Button {
                    router.reloadAllowlist()
                    watch.readNow()
                } label: {
                    Label("Read The Mac's Copy Again", systemImage: "arrow.clockwise")
                }
                .help("Reads the copy of the server's report the Mac holds.\u{00A0} It asks the server nothing.")
            }
            Text("What the server's own report says about the host, the units it watches, its containers, its processes, and the fleet's config files.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// The server's state,  in the menu's own words,  and the menu's first line about it.
    private func stateLine(_ card: PlaceCard) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            PlaceHealthLabel(health: card.health)
            if let first = card.lines.first {
                Text(first)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// Where the Mac keeps its copy of the report.
    private func pathLine(_ reading: ServerStatusReading) -> some View {
        Text(reading.path)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(.secondary)
            .textSelection(.enabled)
            .lineLimit(1)
            .truncationMode(.middle)
            .help(reading.path)
    }

    /// The last report's units,  said to be not current,  with the repairs the allowlist
    /// names.  Each one plans first and asks Touch ID before it runs on the server.
    private func lastKnownSection(_ reading: ServerStatusReading) -> some View {
        let from = reading.generatedAt.map { "from the report at \(FleetTime.clock($0))" } ?? ""
        return VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Last Known, Not Current", nil, from)
            ForEach(ServerLastKnown.units(reading.services), id: \.self) { unit in
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(OpsNames.unitTitle(unit))
                            .font(.system(size: 12, weight: .semibold))
                        Text(unit)
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 0)
                    Text(ServerLastKnown.lastState(unit: unit, services: reading.services))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                    ForEach(ServerLastKnown.actions(unit: unit, allowlist: allowlist), id: \.title) { action in
                        Button(Self.verbWord(action)) {
                            controller.plan(action)
                        }
                        .controlSize(.small)
                        .disabled(controller.isPresenting)
                        .help(action.title)
                        .accessibilityLabel(action.title)
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    /// The short word on a last-known row's button.  The full title,  which names the
    /// server,  is its help and its spoken label.
    private static func verbWord(_ action: OpsAction) -> String {
        if case .logs = action { return "Logs" }
        return "Restart"
    }

    /// Why the report could not be read at all.
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

    // MARK: The sections

    /// The title of a section,  with the state of that half of the report.
    private func sectionHeader(_ title: String, _ state: CellState?, _ detail: String) -> some View {
        HStack(spacing: 10) {
            Text(title)
                .font(.headline)
            if let state {
                FleetToolsStateBadge(state: state, width: 92)
            }
            Spacer()
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// A note under a section header,  in words.
    private func sectionNote(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.orange)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// Why a half was not read whole,  when the collector says.
    @ViewBuilder
    private func collectionNote(_ entry: ServerCollectionEntry?) -> some View {
        if let text = entry?.problemText {
            sectionNote(text)
        }
    }

    private func hostSection(_ reading: ServerStatusReading) -> some View {
        let metrics = reading.hostMetrics
        let uptime = metrics?.uptimeText.map { "up \($0)" } ?? "uptime not read"
        return VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Host", reading.hostState, uptime)
            if let note = metrics?.restartNote {
                sectionNote(note)
            }
            if let note = metrics?.rebootNote {
                sectionNote(note)
            }
            if let metrics {
                if let load = metrics.loadText {
                    let cpus = metrics.cpuCount.map { " on \($0) \($0 == 1 ? "cpu" : "cpus")" } ?? ""
                    metricRow("Load", "\(load)\(cpus)", fraction: metrics.loadFraction)
                }
                if let memory = metrics.memory, let total = memory.totalBytes {
                    let available = memory.availableBytes.map { ServerStatusFormat.bytes($0) } ?? "not read"
                    metricRow(
                        "Memory",
                        "\(available) free of \(ServerStatusFormat.bytes(total))",
                        fraction: memory.usedFraction
                    )
                }
                if let swap = metrics.swap, let total = swap.totalBytes, total > 0 {
                    let free = swap.freeBytes.map { ServerStatusFormat.bytes($0) } ?? "not read"
                    metricRow(
                        "Swap",
                        "\(free) free of \(ServerStatusFormat.bytes(total))",
                        fraction: swap.usedFraction
                    )
                }
                ForEach(metrics.disks) { disk in
                    metricRow(
                        disk.mount.isEmpty ? "disk" : disk.mount,
                        diskText(disk),
                        fraction: disk.usedFraction
                    )
                }
                if metrics.loadText == nil && metrics.memory == nil && metrics.disks.isEmpty {
                    Text("The report carries no host numbers.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func diskText(_ disk: HostDisk) -> String {
        guard let free = disk.freeBytes else { return "not read" }
        if let total = disk.totalBytes {
            return "\(ServerStatusFormat.bytes(free)) free of \(ServerStatusFormat.bytes(total))"
        }
        return "\(ServerStatusFormat.bytes(free)) free"
    }

    /// One measured number,  with a bar when there is a share to draw.
    ///
    /// The bar is amber from 85 percent and red from 95,  and a load bar is drawn against
    /// one per cpu,  so a machine that is fully busy fills it.
    private func metricRow(_ label: String, _ text: String, fraction: Double?) -> some View {
        HStack(spacing: 10) {
            Text(label)
                .font(.system(size: 11, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .help(label)
                .frame(width: 140, alignment: .leading)
            usageBar(fraction)
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
                .help(text)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 1)
    }

    private func usageBar(_ fraction: Double?) -> some View {
        let width: CGFloat = 160
        let share = min(max(fraction ?? 0, 0), 1)
        let tint: Color = {
            guard let fraction else { return .secondary }
            if fraction >= 0.95 { return .red }
            if fraction >= 0.85 { return .orange }
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

    private func servicesSection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Fleet Units", reading.servicesState, unitsDetail(reading))
            collectionNote(reading.collection?.services)
            collectionNote(reading.collection?.failedUnits)
            ForEach(reading.failedUnits ?? []) { unit in
                failedUnitRow(unit)
            }
            ForEach(reading.services) { service in
                serviceRow(service)
            }
        }
    }

    /// How many units are listed and how many of them Muster can act on,  so a short list
    /// never reads as every service on the server.
    private func unitsDetail(_ reading: ServerStatusReading) -> String {
        let count = reading.services.count
        let controllable = reading.services.filter(\.controllable).count
        return "\(count) \(count == 1 ? "unit" : "units"), \(controllable) can be controlled"
    }

    /// A unit systemd says has failed,  whether or not anything else names it.
    private func failedUnitRow(_ unit: ServerFailedUnit) -> some View {
        HStack(spacing: 8) {
            Text(unit.unit)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .help(unit.helpText)
            Text(unit.scope ?? "system")
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 5)
                .padding(.vertical, 1)
                .background(Color.secondary.opacity(0.12), in: Capsule())
            Spacer(minLength: 0)
            Text(unit.detailText())
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            FleetToolsStateBadge(state: .missing, width: 92)
        }
        .padding(.vertical, 2)
    }

    private func serviceRow(_ service: ServerService) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(service.unit)
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .help(service.unit)
                    Text(service.scope ?? "system")
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.12), in: Capsule())
                }
                if let description = service.description, !description.isEmpty {
                    Text(description)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .help(description)
                }
                if let run = service.runText() {
                    Text(run)
                        .font(.system(size: 10))
                        .foregroundStyle(service.lastRunFailed ? Color.red : Color.secondary)
                        .lineLimit(1)
                        .help(run)
                }
            }
            Spacer(minLength: 0)
            Text(service.stateText)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            if let since = service.sinceText {
                Text("for \(since)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let enabled = service.enabled {
                Text(enabled)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if service.controllable {
                ForEach(verbs(for: service), id: \.self) { verb in
                    Button(verb.title) {
                        controller.plan(.serverUnit(verb: verb, unit: service.unit))
                    }
                    .controlSize(.small)
                    .disabled(controller.isPresenting)
                    .help(verb == .stop ? (service.stopConsequence ?? verb.title) : verb.title)
                }
                if service.offersLogs {
                    Button("Logs") {
                        controller.plan(.logs(unit: service.unit))
                    }
                    .controlSize(.small)
                    .disabled(controller.isPresenting)
                    .help("Plan reading this unit's last journal lines.")
                }
            }
            FleetToolsStateBadge(state: service.state, width: 92)
        }
        .padding(.vertical, 2)
    }

    /// The verbs a controllable unit gets:  the ones the report lists,  because the collector
    /// asked the helper on the server.  A report that lists none gets the ones this Mac's
    /// allowlist names for it,  or
    /// all three when this checkout has no entry for it.  The server checks its own
    /// allowlist again either way.
    private func verbs(for service: ServerService) -> [OpsVerb] {
        if let listed = service.listedVerbs { return listed }
        let named = allowlist?.verbs(forServerUnit: service.unit) ?? []
        return named.isEmpty ? OpsVerb.allCases : named
    }

    private func coolifySection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader("Containers", reading.coolifyState, containersDetail(reading))
            collectionNote(reading.collection?.coolify)
            if reading.coolify.isEmpty {
                Text("The report names no containers.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.coolifyProblemsFirst) { resource in
                    coolifyRow(resource, reading: reading)
                }
            }
        }
    }

    /// How many containers the report lists and how many of them are running.
    private func containersDetail(_ reading: ServerStatusReading) -> String {
        let count = reading.coolify.count
        let running = reading.coolify.filter { $0.resourceStatus == .running }.count
        return "\(count) \(count == 1 ? "container" : "containers"), \(running) running"
    }

    private func coolifyRow(_ resource: CoolifyResource, reading: ServerStatusReading) -> some View {
        let offersRedeploy = reading.offersRedeploy(resource)
        return HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(resource.name.isEmpty ? "unnamed" : resource.name)
                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(resource.container ?? resource.name)
                HStack(spacing: 6) {
                    Text(resource.resourceType.rawValue)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .font(.system(size: 9, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.12), in: Capsule())
                    if let image = resource.image, !image.isEmpty {
                        Text(image)
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .help(image)
                    }
                }
                if let signal = resource.signalText() {
                    Text(signal)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .help(signal)
                }
                if !offersRedeploy, let blocked = resource.redeployBlockedText {
                    Text(blocked)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .help(blocked)
                }
            }
            Spacer(minLength: 0)
            if let cpu = resource.cpuText {
                Text("cpu \(cpu)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let memory = resource.memoryText {
                Text("mem \(memory)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let updated = resource.updatedLabel {
                Text(updated)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            // Only an application whose uuid came from a Coolify label is offered:  the
            // server refuses every other row,  so a button there would only lead to a
            // refused plan.
            if allowlist?.coolifyRedeploy == true, offersRedeploy {
                Button("Redeploy") {
                    controller.plan(.coolifyRedeploy(
                        uuid: resource.uuid,
                        name: reading.redeployName(of: resource)
                    ))
                }
                .controlSize(.small)
                .disabled(controller.isPresenting)
                .help(reading.redeployHelp(of: resource))
            }
            FleetToolsStateBadge(state: resource.state, width: 92)
        }
        .padding(.vertical, 2)
    }

    private func processesSection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            // No badge:  a list of busy processes checks nothing,  so it cannot pass.
            sectionHeader("Top Processes", nil, processesDetail(reading))
            collectionNote(reading.collection?.processes)
            if reading.processes.isEmpty {
                Text("The report names no processes.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.processes) { process in
                    processRow(process)
                }
            }
        }
    }

    /// How many processes are listed,  and how long their cpu was measured over.
    private func processesDetail(_ reading: ServerStatusReading) -> String {
        let count = "\(reading.processes.count) \(reading.processes.count == 1 ? "process" : "processes")"
        guard let seconds = reading.processSampleSeconds else { return count }
        return "\(count), measured over \(String(format: "%g", seconds))s"
    }

    private func processRow(_ process: ServerProcess) -> some View {
        HStack(spacing: 8) {
            Text("\(process.pid)")
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 70, alignment: .leading)
            Text(process.displayName)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .lineLimit(1)
                .truncationMode(.middle)
                .help(process.displayName)
            Text(process.owner ?? "unknown")
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(.secondary)
            if let home = process.belongsTo {
                Text(home)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(home)
            }
            Spacer(minLength: 0)
            if let cpu = process.coresText {
                Text("cpu \(cpu)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let memory = process.memoryText {
                Text("mem \(memory)")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }

    private func configSection(_ reading: ServerStatusReading) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionHeader(
                "Changed Since The Last Report",
                reading.configState,
                "\(reading.driftedFiles.count) of \(reading.files.count) \(reading.files.count == 1 ? "file" : "files") changed"
            )
            if reading.filesTruncated {
                sectionNote(partialListText(reading.filesSummary))
            } else {
                collectionNote(reading.collection?.files)
            }
            ForEach(reading.removedCheckouts) { checkout in
                Text(checkout.text)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if reading.files.isEmpty {
                Text("The report watches no files.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if reading.driftedFiles.isEmpty {
                Text(reading.noticedFiles.isEmpty
                    ? "No watched file changed since the last report."
                    : "Only files kept for information changed since the last report.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(reading.driftedFiles) { file in
                    fileRow(file)
                }
            }
            if !reading.noticedFiles.isEmpty {
                Text("Changed, for information only")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
                ForEach(reading.noticedFiles) { file in
                    fileRow(file)
                }
            }
        }
    }

    /// The note for a file list the collector cut short.
    private func partialListText(_ summary: ServerFilesSummary?) -> String {
        let cut = summary?.cutCheckouts ?? []
        let ending = cut.isEmpty
            ? ""
            : "\(ServerStatusCopy.sentenceGap)Cut short: \(cut.joined(separator: ", "))."
        if let found = summary?.found, let limit = summary?.limit {
            return "Partial list: the collector found \(found) files and read \(limit).\(ending)"
        }
        return "Partial list: the collector stopped at its limit.\(ending)"
    }

    private func fileRow(_ file: WatchedFile) -> some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(file.fileName)
                    .font(.system(size: 11, weight: .semibold, design: .monospaced))
                Text(file.path)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(file.path)
            }
            Spacer(minLength: 0)
            if let when = file.whenText() {
                Text(when)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if let size = file.sizeText {
                Text(size)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Text(file.fileDrift.rawValue)
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(tint(for: file.state))
        }
        .padding(.vertical, 2)
    }

    /// The colour a row's state carries,  from the app's own palette.
    private func tint(for state: CellState) -> Color {
        switch state {
        case .missing: return .red
        case .drift: return .orange
        case .unproven, .unknown: return .indigo
        case .passed: return .green
        case .unsupported: return .secondary
        case .covered: return .secondary
        }
    }
}
