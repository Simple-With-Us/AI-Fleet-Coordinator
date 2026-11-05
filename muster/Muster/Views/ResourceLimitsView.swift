import SwiftUI

/// Server resource controls edit local drafts only.  Each explicit review button sends
/// one value and the exact revision seen in the current direct receipt to the shared
/// plan/Touch ID/apply flow owned by the caller.
struct ResourceLimitsView: View {
    typealias PolicyReader = () async -> ResourcePolicyReading
    typealias ReviewChange = (_ field: String, _ value: String, _ revision: String) -> Void

    private let readPolicy: PolicyReader
    private let onReviewChange: ReviewChange
    @ObservedObject private var controller = OpsController.shared
    @State private var reading = ResourcePolicyReading.unavailable("Refresh to read the server policy.")
    @State private var drafts: [ResourcePolicyField: Double] = [:]
    @State private var loading = false

    init(
        readPolicy: @escaping PolicyReader = { .unavailable() },
        onReviewChange: @escaping ReviewChange = { _, _, _ in }
    ) {
        self.readPolicy = readPolicy
        self.onReviewChange = onReviewChange
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                receiptStatus
                if let snapshot = reading.snapshot {
                    if snapshot.status == "drift" {
                        Label("Observed runtime limits differ from the persisted policy.\u{00A0} Compare each observed value and mismatch count before reviewing a change.", systemImage: "arrow.triangle.2.circlepath")
                            .font(.callout)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(12)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                    }
                    cpuCard(snapshot)
                    memoryCard(snapshot)
                    borrowingCard(snapshot)
                    observationCard(snapshot)
                } else {
                    unknownCard
                }
            }
            .frame(maxWidth: 920, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .task { await refresh() }
        .onChange(of: controller.completedRuns) { _, _ in
            Task { await refresh() }
        }
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await refresh() }
                } label: {
                    Label("Read From The Server", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
                .help("Reads the resource policy from the server again.")
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(MusterDestination.serverResourceLimits.title)
                .font(.largeTitle.weight(.semibold))
            Text("Set coding capacity while keeping host and production headroom visible.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    private var receiptStatus: some View {
        let state = reading.state(at: Date())
        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: state == .current ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .foregroundStyle(state == .current ? Color.green : Color.orange)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(state.title)
                    .font(.headline)
                if loading {
                    Text("Reading the server receipt…")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                } else if let reason = reading.reason(at: Date()) {
                    Text(reason)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else if let directReceiptAt = reading.directReceiptAt {
                    let age = max(0, Int(Date().timeIntervalSince(directReceiptAt)))
                    Text("Direct receipt read \(age) seconds ago.\u{00A0} Review uses this policy revision.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(state == .current ? Color.green.opacity(0.08) : Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
    }

    private var unknownCard: some View {
        GroupBox("Resource Policy") {
            Label("No trusted policy values are available.\u{00A0} Refresh to request a direct server receipt.", systemImage: "questionmark.circle")
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 6)
        }
    }

    private func cpuCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        GroupBox("CPU") {
            VStack(alignment: .leading, spacing: 16) {
                numericControl(
                    .cpuCores,
                    snapshot: snapshot,
                    range: 1...Double(max(1, snapshot.observed.cpuOnline - 2)),
                    step: 1,
                    observed: snapshot.observed.cpuCores.map { "\($0) cores" },
                    help: "The server has \(snapshot.observed.cpuOnline) online cores.\u{00A0} This cap keeps two cores available to the host."
                )
                numericControl(
                    .cpuWeight,
                    snapshot: snapshot,
                    range: 1...10_000,
                    step: 1,
                    observed: snapshot.observed.cpuWeight.map(String.init),
                    help: "CPU weight sets relative priority when services compete.\u{00A0} 50 is low priority; 100 is normal."
                )
            }
            .padding(.vertical, 4)
        }
    }

    private func memoryCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        GroupBox("Memory") {
            VStack(alignment: .leading, spacing: 16) {
                numericControl(
                    .ramBudgetGiB,
                    snapshot: snapshot,
                    range: memoryRange(snapshot, minimum: 2),
                    step: 1,
                    observed: gib(snapshot.observed.ramTargetBytes),
                    help: "Soft target for coding workloads.\u{00A0} This guides pressure management and does not guarantee an allocation."
                )
                numericControl(
                    .ramCriticalGiB,
                    snapshot: snapshot,
                    range: 1...max(1, snapshot.desired.ramPressureGiB),
                    step: 1,
                    observed: nil,
                    help: "Soft critical target used during severe host memory pressure.\u{00A0} The controller tightens memory limits; it does not stop or restart workloads."
                )
                numericControl(
                    .ramReserveGiB,
                    snapshot: snapshot,
                    range: memoryRange(snapshot, minimum: 2),
                    step: 1,
                    observed: nil,
                    help: "Headroom target for the host and production services.\u{00A0} This is not a guaranteed physical memory reservation."
                )
                if let total = snapshot.observed.ramTotalBytes {
                    Text("Host memory: \(formatGiB(Double(total) / 1_073_741_824)) total")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Text("Host memory total is unknown.\u{00A0} Memory controls cannot be reviewed.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            .padding(.vertical, 4)
        }
    }

    private func borrowingCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        GroupBox("Idle Capacity") {
            VStack(alignment: .leading, spacing: 10) {
                Toggle("Borrow Idle Memory", isOn: Binding(
                    get: { drafts[.borrowIdle] == 1 || (drafts[.borrowIdle] == nil && snapshot.desired.allowBorrowing) },
                    set: { drafts[.borrowIdle] = $0 ? 1 : 0 }
                ))
                .disabled(!hasReviewableReceipt)
                Text("Allow coding services to borrow unused memory.\u{00A0} The memory controller tightens coding limits under pressure.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                valueLine(
                    desired: snapshot.desired.allowBorrowing ? "Enabled" : "Disabled",
                    observed: nil,
                    note: nil
                )
                reviewButton(.borrowIdle, snapshot: snapshot)
            }
            .padding(.vertical, 4)
        }
    }

    private func observationCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        GroupBox("Runtime Receipt") {
            VStack(alignment: .leading, spacing: 8) {
                observationRow("Policy Status", snapshot.status.capitalized)
                observationRow("Controller Mode", snapshot.observed.mode)
                observationRow("CPU Groups", count(snapshot.observed.cpuGroups))
                observationRow("CPU Mismatches", count(snapshot.observed.cpuMismatches))
                observationRow("Coding Memory In Use", gib(snapshot.observed.ramCurrentBytes) ?? "Unknown")
                observationRow("Observed RAM Target", gib(snapshot.observed.ramTargetBytes) ?? "Unknown")
                observationRow("Coding Groups", count(snapshot.observed.codingGroups))
                observationRow("Production Groups", count(snapshot.observed.productionGroups))
                observationRow("Control Write Failures", count(snapshot.observed.controlWriteFailures))
                observationRow("Suspended Controls", count(snapshot.observed.controlSuspended))
                observationRow("High Memory Mismatches", count(snapshot.observed.actualHighMismatches))
                observationRow("Policy Stale", snapshot.observed.policyStale ? "Yes" : "No")
                observationRow("Collector Update", collectorText)
                if let age = reading.observationAge, age > ResourcePolicyReading.maxCollectorAge {
                    Label("The collector observation is older than 45 minutes.\u{00A0} Runtime measurements may be out of date.", systemImage: "clock.badge.exclamationmark")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.vertical, 4)
        }
    }

    private var collectorText: String {
        guard let age = reading.observationAge else { return "Unknown.\u{00A0} No readable observation timestamp." }
        return age < 60 ? "Observed less than a minute ago" : "Observed \(Int(age / 60)) min ago"
    }

    private var hasReviewableReceipt: Bool { reading.canReviewChanges(at: Date()) }

    @ViewBuilder
    private func numericControl(
        _ field: ResourcePolicyField,
        snapshot: ResourcePolicySnapshot,
        range: ClosedRange<Double>?,
        step: Double,
        observed: String?,
        help: String
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(field.title).font(.headline)
                Spacer()
                if field == .cpuWeight, let persisted = field.persistedValue(in: snapshot) {
                    TextField("CPU Weight", value: Binding(
                        get: { drafts[field] ?? persisted },
                        set: { value in
                            guard value.isFinite, (1...10_000).contains(value), value.rounded() == value else { return }
                            drafts[field] = value
                        }
                    ), format: .number.precision(.fractionLength(0)))
                        .font(.system(.body, design: .monospaced).weight(.semibold))
                        .monospacedDigit()
                        .multilineTextAlignment(.trailing)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 92)
                        .disabled(!hasReviewableReceipt)
                        .accessibilityLabel("CPU Weight")
                } else {
                    Text(display(draft(for: field, snapshot: snapshot), unit: field.unit))
                        .font(.system(.body, design: .monospaced).weight(.semibold))
                        .monospacedDigit()
                }
            }
            if let range, range.lowerBound <= range.upperBound,
               let persisted = field.persistedValue(in: snapshot) {
                Slider(value: draftBinding(field, persisted: persisted), in: range, step: step)
                    .disabled(!hasReviewableReceipt)
                reviewButton(field, snapshot: snapshot)
            } else {
                Label("Unknown.\u{00A0} The host limits or persisted value are unavailable.", systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            valueLine(
                desired: field.persistedValue(in: snapshot).map { display($0, unit: field.unit) },
                observed: observed,
                note: field.persistedValue(in: snapshot) == nil ? "The persisted value is unknown." : nil
            )
            Text(help).font(.caption).foregroundStyle(.secondary)
        }
    }

    private func reviewButton(_ field: ResourcePolicyField, snapshot: ResourcePolicySnapshot) -> some View {
        let value = draft(for: field, snapshot: snapshot)
        let validation = field.validate(value: value, in: snapshot)
        return VStack(alignment: .leading, spacing: 4) {
            Button("Review Change") {
                guard reading.canReviewChanges(at: Date()) else {
                    Task { await refresh() }
                    return
                }
                onReviewChange(field.rawValue, serialized(value, for: field), snapshot.policyRevision)
            }
            .disabled(!hasReviewableReceipt || validation != nil || field.persistedValue(in: snapshot) == value)
            if let validation {
                Text(validation).font(.caption).foregroundStyle(.orange)
            }
        }
    }

    private func valueLine(desired: String?, observed: String?, note: String?) -> some View {
        HStack(spacing: 12) {
            Label("Persisted: \(desired ?? "Unknown")", systemImage: "externaldrive")
            Label("Observed: \(observed ?? "Unknown")", systemImage: "waveform.path")
            Spacer(minLength: 0)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .help(note ?? "Persisted desired settings and observed runtime values are reported separately.")
    }

    private func observationRow(_ title: String, _ value: String) -> some View {
        HStack {
            Text(title).foregroundStyle(.secondary)
            Spacer()
            Text(value).monospacedDigit()
        }
        .font(.callout)
    }

    private func memoryRange(_ snapshot: ResourcePolicySnapshot, minimum: Double) -> ClosedRange<Double>? {
        guard let total = snapshot.observed.ramTotalBytes, total > 0 else { return nil }
        let maximum = floor(Double(total) / 1_073_741_824 - 2)
        guard maximum >= minimum else { return nil }
        return minimum...maximum
    }

    private func draft(for field: ResourcePolicyField, snapshot: ResourcePolicySnapshot) -> Double {
        drafts[field] ?? field.persistedValue(in: snapshot) ?? 0
    }

    private func draftBinding(_ field: ResourcePolicyField, persisted: Double) -> Binding<Double> {
        Binding(get: { drafts[field] ?? persisted }, set: { drafts[field] = $0 })
    }

    private func display(_ value: Double?, unit: String) -> String {
        guard let value, value.isFinite else { return "Unknown" }
        let amount = value.rounded() == value ? String(Int(value)) : String(format: "%.1f", value)
        return unit.isEmpty ? amount : "\(amount) \(unit)"
    }

    private func serialized(_ value: Double, for field: ResourcePolicyField) -> String {
        if field == .borrowIdle { return value >= 0.5 ? "1" : "0" }
        if value.rounded() == value { return String(Int(value)) }
        return String(format: "%.1f", value)
    }

    private func count(_ value: Int?) -> String { value.map(String.init) ?? "Unknown" }

    private func gib(_ bytes: Int?) -> String? {
        bytes.map { "\(formatGiB(Double($0) / 1_073_741_824)) GiB" }
    }

    private func formatGiB(_ value: Double) -> String { String(format: "%.1f", value) }

    @MainActor
    private func refresh() async {
        guard !loading else { return }
        loading = true
        reading = await readPolicy()
        loading = false
        guard let snapshot = reading.snapshot else { return }
        for field in ResourcePolicyField.allCases where drafts[field] == nil {
            drafts[field] = field.persistedValue(in: snapshot)
        }
    }
}
