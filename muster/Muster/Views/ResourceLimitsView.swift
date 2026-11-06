import SwiftUI

/// Server resource controls edit local drafts only.  Each explicit review button sends
/// one value and the saved policy's revision from the current direct receipt to the shared
/// plan/Touch ID/apply flow owned by the caller.
///
/// Every limit says what it really does,  from live values;  saved values and what the
/// memory controller measured are labelled apart;  and the page reads the server again on
/// its own while it is open,  so its age and its controls never go stale unnoticed.
struct ResourceLimitsView: View {
    typealias PolicyReader = () async -> ResourcePolicyReading
    typealias ReportReader = () -> ServerStatusReading
    typealias ReviewChange = (_ field: String, _ value: String, _ revision: String) -> Void
    /// A measured value beside a saved one,  under its own label.
    typealias InEffect = (label: String, value: String)

    /// How often the page reads the server again while it is open.
    static let autoRefreshSeconds: UInt64 = 30

    private enum WeightChoice: Hashable {
        case low, normal, custom
    }

    private let readPolicy: PolicyReader
    private let readReport: ReportReader
    private let onReviewChange: ReviewChange
    @ObservedObject private var controller = OpsController.shared
    @State private var reading = ResourcePolicyReading.loading
    @State private var drafts = ResourceDrafts()
    /// The custom CPU weight as typed,  while the owner is typing one.
    @State private var weightText: String?
    @State private var loading = false
    @State private var showDetails = false

    init(
        readPolicy: @escaping PolicyReader = { .unavailable() },
        readReport: @escaping ReportReader = { ServerStatusReportReader.readDefault() },
        onReviewChange: @escaping ReviewChange = { _, _, _ in }
    ) {
        self.readPolicy = readPolicy
        self.readReport = readReport
        self.onReviewChange = onReviewChange
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                receiptStatus
                if let snapshot = reading.snapshot {
                    summaryCard(snapshot)
                    cpuCard(snapshot)
                    memoryCard(snapshot)
                    borrowingCard(snapshot)
                    receiptCard(snapshot)
                } else {
                    unknownCard
                }
            }
            .frame(maxWidth: 920, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .center)
        }
        .task { await keepReading() }
        .onChange(of: controller.completedRuns) { _, _ in
            Task { await refresh() }
        }
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await refresh() }
                } label: {
                    Label("Refresh Policy", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("Server Resource Limits")
                .font(.largeTitle.weight(.semibold))
            Text("Set coding capacity while keeping host and production headroom visible.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Where the values came from and how old they are

    /// Ticks every second,  so the age it shows is never frozen (RESOURCES-8).
    private var receiptStatus: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            receiptStatusLine(now: context.date)
        }
    }

    private func receiptStatusLine(now: Date) -> some View {
        let state = reading.state(at: now)
        let good = state == .current
        let waiting = state == .loading
        return HStack(alignment: .top, spacing: 10) {
            Image(systemName: good ? "checkmark.circle.fill" : (waiting ? "hourglass" : "exclamationmark.triangle.fill"))
                .foregroundStyle(good ? Color.green : (waiting ? Color.secondary : Color.orange))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(state.title)
                        .font(.headline)
                    if loading {
                        ProgressView().controlSize(.small)
                    }
                }
                if let line = statusDetail(state: state, now: now) {
                    Text(line)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(good ? Color.green.opacity(0.08) : (waiting ? Color.secondary.opacity(0.08) : Color.orange.opacity(0.10)),
                    in: RoundedRectangle(cornerRadius: 12))
    }

    private func statusDetail(state: ResourcePolicyReadState, now: Date) -> String? {
        if state == .loading {
            return "Reading the server's resource policy.\u{00A0} Nothing changes."
        }
        if let reason = reading.reason(at: now) { return reason }
        guard let checkedAt = reading.directReceiptAt else { return nil }
        let age = max(0, Int(now.timeIntervalSince(checkedAt)))
        return "Checked at \(FleetTime.clock(checkedAt)), \(age) seconds ago.\u{00A0} The page reads the server again every \(Self.autoRefreshSeconds) seconds while it is open."
    }

    /// No values at all.  The reason above already says why,  with its own next step,  so
    /// this card never tells the owner to retry something retrying cannot fix.
    private var unknownCard: some View {
        GroupBox("Resource Policy") {
            Label(
                reading.state == .loading
                    ? "Waiting for the server's answer."
                    : "No saved or live values are available.\u{00A0} The reason is above.",
                systemImage: "questionmark.circle"
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 6)
        }
    }

    /// One sentence on whether the limits are in effect,  then every named cause or reason.
    private func summaryCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        let applied = snapshot.status == "applied"
        let icon = applied ? "checkmark.circle.fill" : (snapshot.status == "drift" ? "arrow.triangle.2.circlepath" : "exclamationmark.triangle.fill")
        let lines = ResourceLimitsCopy.explanation(snapshot)
        return VStack(alignment: .leading, spacing: 6) {
            Label(ResourceLimitsCopy.summary(snapshot), systemImage: icon)
                .font(.callout.weight(.medium))
                .foregroundStyle(applied ? Color.primary : Color.orange)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(lines, id: \.self) { line in
                Text("\u{2022} \(line)")
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background((applied ? Color.secondary : Color.orange).opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - CPU

    private func cpuCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        let liveCores: Int? = snapshot.observed.flatMap { $0.cpuCores }
        return GroupBox("CPU") {
            VStack(alignment: .leading, spacing: 16) {
                numericControl(
                    .cpuCores,
                    snapshot: snapshot,
                    inEffect: liveCores.map { (label: "In Effect", value: ResourcePolicyField.cpuCores.display(Double($0))) },
                    help: ResourceLimitsCopy.cpuCoresHelp(snapshot, cores: drafts.value(.cpuCores, in: snapshot))
                )
                weightControl(snapshot)
            }
            .padding(.vertical, 4)
        }
    }

    /// CPU weight is a choice of the two useful values or a typed one,  never a 10,000-step
    /// slider,  and a typed value counts as it is typed (RESOURCES-18,  A11Y-47).
    @ViewBuilder
    private func weightControl(_ snapshot: ResourcePolicySnapshot) -> some View {
        let field = ResourcePolicyField.cpuWeight
        let value = drafts.value(field, in: snapshot)
        let liveWeight: Int? = snapshot.observed.flatMap { $0.cpuWeight }
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(field.title).font(.headline)
                if drafts.isEdited(field) { editedTag }
                Spacer()
                Text(field.display(value))
                    .font(.system(.body, design: .monospaced).weight(.semibold))
                    .monospacedDigit()
            }
            if field.persistedValue(in: snapshot) != nil {
                Picker("CPU Weight", selection: weightChoiceBinding(snapshot)) {
                    Text("Low (50)").tag(WeightChoice.low)
                    Text("Normal (100)").tag(WeightChoice.normal)
                    Text("Custom").tag(WeightChoice.custom)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .disabled(!hasReviewableReceipt)
                if weightChoice(value) == .custom {
                    HStack(spacing: 10) {
                        TextField("1 to 10,000", text: weightTextBinding(snapshot))
                            .textFieldStyle(.roundedBorder)
                            .monospacedDigit()
                            .frame(width: 120)
                            .disabled(!hasReviewableReceipt)
                            .accessibilityLabel("Custom CPU Weight")
                        if weightTextInvalid {
                            Label("Enter a whole number from 1 to 10,000.", systemImage: "exclamationmark.triangle.fill")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                    }
                }
                reviewRow(field, snapshot: snapshot)
            } else {
                Label("Unknown.\u{00A0} The saved policy sets no CPU weight, so it cannot be changed here.", systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            valueLine(field, snapshot: snapshot,
                      inEffect: liveWeight.map { (label: "In Effect", value: String($0)) })
            Text(ResourceLimitsCopy.cpuWeightHelp)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func weightChoice(_ value: Double?) -> WeightChoice {
        if weightText != nil { return .custom }
        if value == 50 { return .low }
        if value == 100 { return .normal }
        return .custom
    }

    private func weightChoiceBinding(_ snapshot: ResourcePolicySnapshot) -> Binding<WeightChoice> {
        Binding(
            get: { weightChoice(drafts.value(.cpuWeight, in: snapshot)) },
            set: { choice in
                switch choice {
                case .low:
                    weightText = nil
                    drafts.set(.cpuWeight, to: 50, in: snapshot)
                case .normal:
                    weightText = nil
                    drafts.set(.cpuWeight, to: 100, in: snapshot)
                case .custom:
                    weightText = drafts.value(.cpuWeight, in: snapshot).map { String(Int($0)) } ?? ""
                }
            }
        )
    }

    private func weightTextBinding(_ snapshot: ResourcePolicySnapshot) -> Binding<String> {
        Binding(
            get: { weightText ?? drafts.value(.cpuWeight, in: snapshot).map { String(Int($0)) } ?? "" },
            set: { text in
                weightText = text
                if let number = ResourceLimitsCopy.parseCPUWeight(text) {
                    drafts.set(.cpuWeight, to: Double(number), in: snapshot)
                }
            }
        )
    }

    private var weightTextInvalid: Bool {
        guard let weightText else { return false }
        return ResourceLimitsCopy.parseCPUWeight(weightText) == nil
    }

    // MARK: - Memory

    private func memoryCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        let pressure = drafts.value(.ramBudgetGiB, in: snapshot) ?? snapshot.desired.ramPressureGiB
        let critical = drafts.value(.ramCriticalGiB, in: snapshot) ?? snapshot.desired.ramCriticalGiB
        let reserve = drafts.value(.ramReserveGiB, in: snapshot) ?? snapshot.desired.reserveGiB
        return GroupBox("Memory") {
            VStack(alignment: .leading, spacing: 16) {
                numericControl(
                    .ramBudgetGiB,
                    snapshot: snapshot,
                    inEffect: currentLimit(snapshot),
                    help: ResourceLimitsCopy.pressureHelp(
                        snapshot, pressure: pressure,
                        borrowing: (drafts.value(.borrowIdle, in: snapshot) ?? (snapshot.desired.allowBorrowing ? 1 : 0)) >= 0.5
                    )
                )
                numericControl(
                    .ramCriticalGiB,
                    snapshot: snapshot,
                    inEffect: nil,
                    help: ResourceLimitsCopy.criticalHelp(snapshot, critical: critical)
                )
                numericControl(
                    .ramReserveGiB,
                    snapshot: snapshot,
                    inEffect: nil,
                    help: ResourceLimitsCopy.reserveHelp(snapshot, reserve: reserve)
                )
                productionHeadroom(snapshot, pressure: pressure, reserve: reserve)
                if let total = snapshot.totalMemoryGiB {
                    Text("Server memory: \(ResourceLimitsCopy.gib(total)) in all.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    Label("The server's total memory is unknown, so memory limits cannot be changed here.", systemImage: "questionmark.circle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
            .padding(.vertical, 4)
        }
    }

    /// The coding memory limit the controller is applying now,  which with borrowing on is
    /// usually above Pressure RAM Limit and is not a drift (RESOURCES-1).
    private func currentLimit(_ snapshot: ResourcePolicySnapshot) -> InEffect? {
        guard let observed = snapshot.observed else { return nil }
        guard observed.ramTargetBytes > 0 else { return (label: "Current Limit", value: "none, no coding running") }
        return (label: "Current Limit", value: "\(ResourceLimitsCopy.gib(bytes: observed.ramTargetBytes)) in \(observed.mode) mode")
    }

    /// What these limits leave production,  and what an edit would change (RESOURCES-2).
    @ViewBuilder
    private func productionHeadroom(_ snapshot: ResourcePolicySnapshot, pressure: Double, reserve: Double) -> some View {
        if let line = ResourceLimitsCopy.productionLine(snapshot, pressure: pressure, reserve: reserve) {
            Label(line.text, systemImage: line.warning ? "exclamationmark.triangle.fill" : "shield.lefthalf.filled")
                .font(.callout)
                .foregroundStyle(line.warning ? Color.orange : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: - Borrowing

    private func borrowingCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        let field = ResourcePolicyField.borrowIdle
        let on = (drafts.value(field, in: snapshot) ?? 0) >= 0.5
        return GroupBox(field.title) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 8) {
                    Toggle(field.title, isOn: Binding(
                        get: { on },
                        set: { drafts.set(field, to: $0 ? 1 : 0, in: snapshot) }
                    ))
                    .disabled(!hasReviewableReceipt)
                    if drafts.isEdited(field) { editedTag }
                }
                Text(ResourceLimitsCopy.borrowHelp(on: on))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                valueLine(field, snapshot: snapshot, inEffect: nil)
                reviewRow(field, snapshot: snapshot)
            }
            .padding(.vertical, 4)
        }
    }

    // MARK: - What the server is doing

    private func receiptCard(_ snapshot: ResourcePolicySnapshot) -> some View {
        let rows = ResourceLimitsCopy.receiptRows(snapshot)
        let details = rows.filter { $0.kind == .detail }
        return GroupBox("What The Server Is Doing") {
            VStack(alignment: .leading, spacing: 8) {
                if rows.isEmpty {
                    Text("The memory controller sent no complete measurements.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                ForEach(rows.filter { $0.kind == .primary }) { row in
                    receiptRow(row)
                }
                ForEach(rows.filter { $0.kind == .problem }) { row in
                    receiptRow(row)
                }
                if !details.isEmpty {
                    DisclosureGroup("Details", isExpanded: $showDetails) {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(details) { row in
                                receiptRow(row)
                            }
                        }
                        .padding(.top, 4)
                    }
                    .font(.callout)
                }
                Text(ResourceLimitsCopy.measuredText(snapshot))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 4)
        }
    }

    private func receiptRow(_ row: ResourceReceiptRow) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if row.kind == .problem {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                    .accessibilityLabel("Problem")
            }
            Text(row.title)
                .foregroundStyle(row.kind == .problem ? Color.primary : Color.secondary)
            Spacer()
            Text(row.value)
                .monospacedDigit()
        }
        .font(.callout)
    }

    // MARK: - Shared controls

    private var hasReviewableReceipt: Bool { reading.canReviewChanges(at: Date()) }

    private var editedTag: some View {
        Text("Edited")
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(Color.accentColor.opacity(0.15), in: Capsule())
            .accessibilityLabel("Edited, not yet sent")
    }

    @ViewBuilder
    private func numericControl(
        _ field: ResourcePolicyField,
        snapshot: ResourcePolicySnapshot,
        inEffect: InEffect?,
        help: String
    ) -> some View {
        let value = drafts.value(field, in: snapshot)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(field.title).font(.headline)
                if drafts.isEdited(field) { editedTag }
                Spacer()
                Text(field.display(value))
                    .font(.system(.body, design: .monospaced).weight(.semibold))
                    .monospacedDigit()
            }
            if let range = field.range(in: snapshot), field.persistedValue(in: snapshot) != nil {
                Slider(value: draftBinding(field, snapshot: snapshot, fallback: range.lowerBound), in: range, step: 1)
                    .disabled(!hasReviewableReceipt)
                    .accessibilityLabel(field.title)
                    .accessibilityValue(field.display(value))
                reviewRow(field, snapshot: snapshot)
            } else {
                Label("Unknown.\u{00A0} The server's limits or the saved value are not available, so this cannot be changed here.", systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            valueLine(field, snapshot: snapshot, inEffect: inEffect)
            Text(help)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Review,  Revert,  and the note when the server's saved value moved under an edit
    /// (RESOURCES-9).
    @ViewBuilder
    private func reviewRow(_ field: ResourcePolicyField, snapshot: ResourcePolicySnapshot) -> some View {
        let edited = drafts.isEdited(field)
        let value = drafts.value(field, in: snapshot)
        let validation = value.flatMap { field.validate(value: $0, in: snapshot) }
        let moved = drafts.movedValue(field, in: snapshot)
        let typingInvalid = field == .cpuWeight && weightTextInvalid
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Button("Review Change") { review(field) }
                    .disabled(!hasReviewableReceipt || !edited || validation != nil || moved != nil || typingInvalid)
                if edited {
                    Button("Revert") { revert(field) }
                }
                if moved != nil {
                    Button("Keep My Edit") { drafts.rebase(field, in: snapshot) }
                }
            }
            if let moved {
                Label("The saved value changed to \(field.display(moved)) on the server after you started this edit.", systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if edited, let validation {
                Label(validation, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func valueLine(_ field: ResourcePolicyField, snapshot: ResourcePolicySnapshot, inEffect: InEffect?) -> some View {
        HStack(spacing: 12) {
            Label("Saved: \(field.display(field.persistedValue(in: snapshot)))", systemImage: "externaldrive")
            if let inEffect {
                Label("\(inEffect.label): \(inEffect.value)", systemImage: "waveform.path")
            }
            Spacer(minLength: 0)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .help("Saved is the server's saved policy.\u{00A0} \(inEffect?.label ?? "In Effect") is what the memory controller measured.")
    }

    private func draftBinding(_ field: ResourcePolicyField, snapshot: ResourcePolicySnapshot, fallback: Double) -> Binding<Double> {
        Binding(
            get: { drafts.value(field, in: snapshot) ?? fallback },
            set: { drafts.set(field, to: $0.rounded(), in: snapshot) }
        )
    }

    private func revert(_ field: ResourcePolicyField) {
        drafts.revert(field)
        if field == .cpuWeight { weightText = nil }
    }

    /// Send the edit for review.  An expired read is read again first,  and the review then
    /// goes on by itself when the new read still allows it (RESOURCES-8).
    private func review(_ field: ResourcePolicyField) {
        if reading.canReviewChanges(at: Date()) {
            send(field)
            return
        }
        Task {
            await settleReading()
            if reading.canReviewChanges(at: Date()) {
                send(field)
            }
        }
    }

    /// Wait until a read has finished and the receipt can be reviewed,  or give up quietly.
    /// A read already running (the 30-second one,  say) is waited for,  never skipped,  and
    /// when another read starts in the gap before ours,  the second pass waits for that one.
    @MainActor
    private func settleReading() async {
        for _ in 0..<2 {
            await finishRefresh()
            if reading.canReviewChanges(at: Date()) { return }
            await refresh()
        }
        await finishRefresh()
    }

    /// Wait for a read that is already running,  for at most the plan timeout.
    @MainActor
    private func finishRefresh() async {
        var waited = 0
        while loading && waited < 900 {
            try? await Task.sleep(nanoseconds: 100_000_000)
            waited += 1
        }
    }

    private func send(_ field: ResourcePolicyField) {
        guard let snapshot = reading.snapshot,
              let revision = snapshot.reviewRevision,
              drafts.isEdited(field),
              drafts.movedValue(field, in: snapshot) == nil,
              let value = drafts.value(field, in: snapshot),
              field.validate(value: value, in: snapshot) == nil,
              !(field == .cpuWeight && weightTextInvalid) else { return }
        onReviewChange(field.rawValue, field.serialized(value), revision)
    }

    // MARK: - Reading

    /// Reads now,  then again every 30 seconds while the page is open.  `.task` ends the loop
    /// when the page closes,  and no read starts while an action is running.
    @MainActor
    private func keepReading() async {
        await refresh()
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: Self.autoRefreshSeconds * 1_000_000_000)
            if Task.isCancelled { return }
            if controller.isBusy { continue }
            await refresh()
        }
    }

    /// A direct read,  or,  when it brings back nothing,  the copy in the server report the
    /// Mac pulls,  read only (RESOURCES-22).  Untouched fields follow the new saved values.
    @MainActor
    private func refresh() async {
        guard !loading else { return }
        loading = true
        var next = await readPolicy()
        if next.snapshot == nil,
           let fallback = ResourcePolicyReading.fromServerReport(readReport(), directFailure: next.reason) {
            next = fallback
        }
        reading = next
        loading = false
        if let snapshot = next.snapshot {
            drafts.follow(snapshot)
        }
    }
}
