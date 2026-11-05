import Foundation

/// The direct receipt Muster accepts for the server's coding resource policy.
///
/// Desired policy is not evidence that a workload adopted the policy.  The observed
/// fields and their timestamps stay separate so a stale or missing receipt cannot
/// read as a successful apply.
struct ResourcePolicySnapshot: Codable, Equatable {
    let schemaVersion: Int
    let generatedAt: String
    /// The revision the memory controller reports it is running.  Nil when the controller
    /// wrote no receipt.
    let policyRevision: String?
    /// The revision of the saved policy the server read from its source,  which is the one
    /// the server checks a change against.  Nil when the source could not be read.
    let desiredRevision: String?
    let desired: Desired
    /// The controller's measurements.  Nil when its receipt is missing or incomplete,  so the
    /// saved values can still be shown,  read only,  beside the reasons.
    let observed: Observed?
    let status: String
    /// Fixed reason codes for a status that is not applied,  in the server's order.
    let reasons: [String]

    struct Desired: Codable, Equatable {
        let cpuCores: Int?
        let cpuWeight: Int?
        let ramPressureGiB: Double
        let ramCriticalGiB: Double
        let reserveGiB: Double
        let allowBorrowing: Bool
        /// Available memory below which pressure mode starts,  when the server says.
        let pressureAvailableGiB: Double?
        /// Available memory below which critical mode starts,  when the server says.
        let criticalAvailableGiB: Double?
    }

    struct Observed: Codable, Equatable {
        let cpuOnline: Int
        let cpuCores: Int?
        let cpuWeight: Int?
        let cpuGroups: Int
        let cpuMismatches: Int
        let ramTotalBytes: Int?
        let ramCurrentBytes: Int
        let ramTargetBytes: Int
        let mode: String
        let codingGroups: Int
        let productionGroups: Int
        let controlWriteFailures: Int
        let controlSuspended: Int
        let policyStale: Bool
        let actualHighMismatches: Int
        let updatedAt: String?
        /// The counts below are present only when the controller measured them.
        let availableBytes: Int?
        let productionProtectedGroups: Int?
        let productionUnprotectedGroups: Int?
        let oomKillDelta: Int?
        let excludedUnits: Int?
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion, generatedAt, policyRevision, desiredRevision, desired, observed, status, reasons
    }

    /// The current collector contract.  Missing optional measurements remain nil and are
    /// rendered as unknown.  An incomplete measurement block becomes nil rather than failing
    /// the whole read,  so the saved policy stays visible when the controller is unhealthy;
    /// the reader then refuses to call that report applied.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)
        generatedAt = try c.decode(String.self, forKey: .generatedAt)
        policyRevision = try c.decodeIfPresent(String.self, forKey: .policyRevision)
        desiredRevision = try c.decodeIfPresent(String.self, forKey: .desiredRevision)
        desired = try c.decode(Desired.self, forKey: .desired)
        observed = try? c.decodeIfPresent(Observed.self, forKey: .observed)
        status = try c.decode(String.self, forKey: .status)
        reasons = (try? c.decodeIfPresent([String].self, forKey: .reasons)) ?? []
    }
}

extension ResourcePolicySnapshot.Desired {
    enum CodingKeys: String, CodingKey {
        case cpuCores, cpuWeight, ramPressureGiB, ramCriticalGiB, reserveGiB, allowBorrowing
        case pressureAvailableGiB, criticalAvailableGiB
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cpuCores = try c.decodeIfPresent(Int.self, forKey: .cpuCores)
        cpuWeight = try c.decodeIfPresent(Int.self, forKey: .cpuWeight)
        ramPressureGiB = try c.decode(Double.self, forKey: .ramPressureGiB)
        ramCriticalGiB = try c.decode(Double.self, forKey: .ramCriticalGiB)
        reserveGiB = try c.decode(Double.self, forKey: .reserveGiB)
        allowBorrowing = try c.decode(Bool.self, forKey: .allowBorrowing)
        pressureAvailableGiB = try? c.decodeIfPresent(Double.self, forKey: .pressureAvailableGiB)
        criticalAvailableGiB = try? c.decodeIfPresent(Double.self, forKey: .criticalAvailableGiB)
    }
}

extension ResourcePolicySnapshot.Observed {
    enum CodingKeys: String, CodingKey {
        case cpuOnline, cpuCores, cpuWeight, cpuGroups, cpuMismatches
        case ramTotalBytes, ramCurrentBytes, ramTargetBytes, mode, codingGroups
        case productionGroups, controlWriteFailures, controlSuspended, policyStale
        case actualHighMismatches, updatedAt
        case availableBytes, productionProtectedGroups, productionUnprotectedGroups
        case oomKillDelta, excludedUnits
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cpuOnline = try c.decode(Int.self, forKey: .cpuOnline)
        cpuCores = try c.decodeIfPresent(Int.self, forKey: .cpuCores)
        cpuWeight = try c.decodeIfPresent(Int.self, forKey: .cpuWeight)
        cpuGroups = try c.decode(Int.self, forKey: .cpuGroups)
        cpuMismatches = try c.decode(Int.self, forKey: .cpuMismatches)
        ramTotalBytes = try c.decodeIfPresent(Int.self, forKey: .ramTotalBytes)
        ramCurrentBytes = try c.decode(Int.self, forKey: .ramCurrentBytes)
        ramTargetBytes = try c.decode(Int.self, forKey: .ramTargetBytes)
        mode = try c.decode(String.self, forKey: .mode)
        codingGroups = try c.decode(Int.self, forKey: .codingGroups)
        productionGroups = try c.decode(Int.self, forKey: .productionGroups)
        controlWriteFailures = try c.decode(Int.self, forKey: .controlWriteFailures)
        controlSuspended = try c.decode(Int.self, forKey: .controlSuspended)
        policyStale = try c.decode(Bool.self, forKey: .policyStale)
        actualHighMismatches = try c.decode(Int.self, forKey: .actualHighMismatches)
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt)
        availableBytes = try? c.decodeIfPresent(Int.self, forKey: .availableBytes)
        productionProtectedGroups = try? c.decodeIfPresent(Int.self, forKey: .productionProtectedGroups)
        productionUnprotectedGroups = try? c.decodeIfPresent(Int.self, forKey: .productionUnprotectedGroups)
        oomKillDelta = try? c.decodeIfPresent(Int.self, forKey: .oomKillDelta)
        excludedUnits = try? c.decodeIfPresent(Int.self, forKey: .excludedUnits)
    }
}

extension ResourcePolicySnapshot {
    static let bytesPerGiB = 1_073_741_824.0

    /// The revision a change is reviewed against.  The server compares a change with the
    /// saved policy's revision,  so that one is sent;  the controller's stands in only when
    /// the saved one was not read,  and the server then refuses a stale one by itself.
    var reviewRevision: String? { desiredRevision ?? policyRevision }

    /// True when the server holds a saved policy the controller is not running yet.
    var savedPolicyNotInEffect: Bool {
        guard let desiredRevision, let policyRevision else { return false }
        return desiredRevision != policyRevision
    }

    /// The server's memory in GiB,  when the receipt measured it.
    var totalMemoryGiB: Double? {
        guard let bytes = observed?.ramTotalBytes, bytes > 0 else { return nil }
        return Double(bytes) / Self.bytesPerGiB
    }

    /// The most protected memory production can be promised for these limits.
    ///
    /// The controller shares the server's memory minus Host RAM Reserve minus Pressure RAM
    /// Limit among the production services as protected memory,  scaled down to each
    /// service's own cap,  so this is an upper bound.  Nil when the total is unknown.
    func productionProtectionGiB(pressure: Double? = nil, reserve: Double? = nil) -> Double? {
        guard let total = totalMemoryGiB else { return nil }
        return max(0, total - (reserve ?? desired.reserveGiB) - (pressure ?? desired.ramPressureGiB))
    }
}

enum ResourcePolicyField: String, CaseIterable, Hashable, Identifiable {
    case cpuCores = "cpu-cores"
    case cpuWeight = "cpu-weight"
    case ramBudgetGiB = "ram-budget-gib"
    case ramCriticalGiB = "ram-critical-gib"
    case ramReserveGiB = "ram-reserve-gib"
    case borrowIdle = "borrow-idle"

    var id: String { rawValue }

    /// The one name each control has,  on the page and on the review sheet.  The server's
    /// FIELD_TITLES in scripts/fleet_resource_control.py holds the same words,  and the
    /// contract fixture keeps the two equal.
    var title: String {
        switch self {
        case .cpuCores: return "CPU Cores"
        case .cpuWeight: return "CPU Weight"
        case .ramBudgetGiB: return "Pressure RAM Limit"
        case .ramCriticalGiB: return "Critical RAM Limit"
        case .ramReserveGiB: return "Host RAM Reserve"
        case .borrowIdle: return "Borrow Idle Memory"
        }
    }

    var unit: String {
        switch self {
        case .cpuCores: return "cores"
        case .ramBudgetGiB, .ramCriticalGiB, .ramReserveGiB: return "GiB"
        case .cpuWeight, .borrowIdle: return ""
        }
    }

    func persistedValue(in snapshot: ResourcePolicySnapshot) -> Double? {
        switch self {
        case .cpuCores: return snapshot.desired.cpuCores.map(Double.init)
        case .cpuWeight: return snapshot.desired.cpuWeight.map(Double.init)
        case .ramBudgetGiB: return snapshot.desired.ramPressureGiB
        case .ramCriticalGiB: return snapshot.desired.ramCriticalGiB
        case .ramReserveGiB: return snapshot.desired.reserveGiB
        case .borrowIdle: return snapshot.desired.allowBorrowing ? 1 : 0
        }
    }

    /// A value in the owner's units:  `12 GiB`,  `8 cores`,  `100`,  `On`.  An unknown value
    /// says Unknown and is never shown as zero.
    func display(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "Unknown" }
        if self == .borrowIdle { return value >= 0.5 ? "On" : "Off" }
        let amount = value.rounded() == value ? String(Int(value)) : String(format: "%.1f", value)
        if self == .cpuCores && value == 1 { return "1 core" }
        return unit.isEmpty ? amount : "\(amount) \(unit)"
    }

    /// The value as the server's change grammar takes it.
    func serialized(_ value: Double) -> String {
        if self == .borrowIdle { return value >= 0.5 ? "1" : "0" }
        if value.rounded() == value { return String(Int(value)) }
        return String(format: "%.1f", value)
    }

    /// The words for one change,  for the review sheet's title and the Touch ID question:
    /// the field's own name and the value in its units,  never a bare `1` or `24`.
    static func changePhrase(field: String, value: String) -> String {
        guard let known = ResourcePolicyField(rawValue: field) else { return "Change the resource policy" }
        if known == .borrowIdle {
            return value == "1" ? "Turn on \(known.title)" : "Turn off \(known.title)"
        }
        return "Set \(known.title) to \(known.display(Double(value)))"
    }

    /// The values a slider offers,  bounded by the same rules the server checks,  so a
    /// slider never offers a value the server always refuses.  Nil when a bound is unknown
    /// or the field takes no slider.
    func range(in snapshot: ResourcePolicySnapshot) -> ClosedRange<Double>? {
        switch self {
        case .cpuCores:
            guard let online = snapshot.observed?.cpuOnline, online >= 3 else { return nil }
            return 1...Double(online - 2)
        case .cpuWeight, .borrowIdle:
            return nil
        case .ramBudgetGiB:
            guard let total = snapshot.totalMemoryGiB else { return nil }
            return Self.bounded(2, floor(total - snapshot.desired.reserveGiB))
        case .ramCriticalGiB:
            return Self.bounded(1, floor(snapshot.desired.ramPressureGiB))
        case .ramReserveGiB:
            guard let total = snapshot.totalMemoryGiB else { return nil }
            return Self.bounded(2, floor(total - snapshot.desired.ramPressureGiB))
        }
    }

    private static func bounded(_ low: Double, _ high: Double) -> ClosedRange<Double>? {
        low <= high ? low...high : nil
    }

    /// Check a proposed single-field change against the saved values of the other fields.
    /// Drafts in other controls never become the limits for this validation.  A refusal
    /// says which field to change first when two settings are linked.
    func validate(value: Double, in snapshot: ResourcePolicySnapshot) -> String? {
        guard value.isFinite else { return "Enter a finite value." }
        let gap = ResourceLimitsCopy.gap
        switch self {
        case .cpuCores:
            guard let online = snapshot.observed?.cpuOnline else { return "The server's core count is unknown." }
            guard value.rounded() == value, value >= 1, value <= Double(online - 2) else {
                return "Choose 1 through \(max(0, online - 2)) cores.\(gap)The server keeps 2 of its \(online) cores free."
            }
        case .cpuWeight:
            guard value.rounded() == value, value >= 1, value <= 10_000 else {
                return "Choose a CPU weight from 1 to 10,000."
            }
        case .ramBudgetGiB:
            guard let total = snapshot.totalMemoryGiB else { return "The server's total memory is unknown." }
            guard value.rounded() == value, value >= 2 else { return "Choose a whole number of GiB, at least 2." }
            let reserve = snapshot.desired.reserveGiB
            let maximum = floor(total - reserve)
            guard value <= maximum else {
                return "With Host RAM Reserve at \(Self.ramReserveGiB.display(reserve)), this limit can be at most \(display(maximum)).\(gap)Lower Host RAM Reserve first to go higher."
            }
            let critical = snapshot.desired.ramCriticalGiB
            guard critical <= value else {
                return "Critical RAM Limit is \(display(critical)), more than this.\(gap)Lower Critical RAM Limit to \(display(value)) or less first."
            }
        case .ramCriticalGiB:
            guard value.rounded() == value, value >= 1 else { return "Choose a whole number of GiB, at least 1." }
            let budget = snapshot.desired.ramPressureGiB
            guard value <= budget else {
                return "This cannot be more than Pressure RAM Limit, which is \(Self.ramBudgetGiB.display(budget)).\(gap)Raise Pressure RAM Limit first."
            }
        case .ramReserveGiB:
            guard let total = snapshot.totalMemoryGiB else { return "The server's total memory is unknown." }
            guard value.rounded() == value, value >= 2 else { return "Choose a whole number of GiB, at least 2." }
            let budget = snapshot.desired.ramPressureGiB
            let maximum = floor(total - budget)
            guard value <= maximum else {
                return "With Pressure RAM Limit at \(Self.ramBudgetGiB.display(budget)), the reserve can be at most \(display(maximum)).\(gap)Lower Pressure RAM Limit first to go higher."
            }
        case .borrowIdle:
            guard value == 0 || value == 1 else { return "Choose on or off." }
        }
        return nil
    }
}

/// The owner's unsent edits on the page.
///
/// Only an edited field has an entry,  so an untouched field always follows the server's
/// saved value.  An edit remembers the saved value it started from,  so the page can say
/// when the server's value moved underneath it instead of quietly undoing someone else's
/// change.
struct ResourceDrafts: Equatable {
    struct Draft: Equatable {
        var value: Double
        /// The saved value when the edit started.
        var base: Double?
    }

    private(set) var entries: [ResourcePolicyField: Draft] = [:]

    /// The value the control shows:  the edit,  or the saved value.  Nil when neither is known.
    func value(_ field: ResourcePolicyField, in snapshot: ResourcePolicySnapshot) -> Double? {
        entries[field]?.value ?? field.persistedValue(in: snapshot)
    }

    func isEdited(_ field: ResourcePolicyField) -> Bool { entries[field] != nil }

    /// Record an edit.  Setting a field back to the saved value clears the edit.
    mutating func set(_ field: ResourcePolicyField, to value: Double, in snapshot: ResourcePolicySnapshot) {
        let persisted = field.persistedValue(in: snapshot)
        if value == persisted {
            entries[field] = nil
            return
        }
        entries[field] = Draft(value: value, base: entries[field]?.base ?? persisted)
    }

    mutating func revert(_ field: ResourcePolicyField) { entries[field] = nil }

    /// Keep the edit and accept the server's new saved value as its starting point.
    mutating func rebase(_ field: ResourcePolicyField, in snapshot: ResourcePolicySnapshot) {
        guard var draft = entries[field] else { return }
        draft.base = field.persistedValue(in: snapshot)
        entries[field] = draft
    }

    /// After a new read:  an edit the server now matches is done,  and every other edit
    /// stays,  with its starting point,  for `movedValue` to compare.
    mutating func follow(_ snapshot: ResourcePolicySnapshot) {
        for (field, draft) in entries where draft.value == field.persistedValue(in: snapshot) {
            entries[field] = nil
        }
    }

    /// The server's new saved value when it moved after this edit started,  or nil.
    func movedValue(_ field: ResourcePolicyField, in snapshot: ResourcePolicySnapshot) -> Double? {
        guard let draft = entries[field], let persisted = field.persistedValue(in: snapshot) else { return nil }
        return draft.base == persisted ? nil : persisted
    }
}

/// One row of the "What The Server Is Doing" card.
struct ResourceReceiptRow: Equatable, Identifiable {
    enum Kind: Equatable {
        /// Always shown.
        case primary
        /// A count that says something is wrong:  shown with a warning sign,  never by colour alone.
        case problem
        /// A count that is fine,  folded under Details.
        case detail
    }

    let title: String
    let value: String
    let kind: Kind
    var id: String { title }
}

/// The page's words,  built from live values so each limit says what it really does.
enum ResourceLimitsCopy {
    /// A real U+00A0 and a space after a sentence.
    static let gap = "\u{00A0} "

    static func gib(_ value: Double) -> String {
        guard value.isFinite else { return "Unknown" }
        let rounded = (value * 10).rounded() / 10
        return rounded.rounded() == rounded ? "\(Int(rounded)) GiB" : String(format: "%.1f GiB", rounded)
    }

    static func gib(bytes: Int) -> String { gib(Double(bytes) / ResourcePolicySnapshot.bytesPerGiB) }

    static func count(_ number: Int, _ noun: String) -> String {
        number == 1 ? "1 \(noun)" : "\(number) \(noun)s"
    }

    /// The one sentence that leads the page:  whether the limits are in effect,  and the
    /// memory mode the server is in.
    static func summary(_ s: ResourcePolicySnapshot) -> String {
        let status: String
        switch s.status {
        case "applied": status = "These limits are in effect."
        case "drift": status = "Some limits differ from the saved policy."
        default: status = "The server cannot confirm these limits are in effect."
        }
        guard let mode = s.observed?.mode else { return status }
        switch mode {
        case "pressure":
            return status + gap + "The server is under memory pressure, so coding is held to Pressure RAM Limit."
        case "critical":
            return status + gap + "The server is in critical memory mode, so coding is held to Critical RAM Limit."
        default:
            return status + gap + "Memory is normal."
        }
    }

    static func cpuCoresHelp(_ s: ResourcePolicySnapshot, cores: Double?) -> String {
        guard let online = s.observed?.cpuOnline else { return "The server's core count is unknown." }
        guard let cores, cores.isFinite, cores >= 1 else {
            return "The server has \(online) online cores.\(gap)At least 2 always stay free for the host."
        }
        let pinned = Int(cores)
        let rest = max(0, online - pinned)
        return "Coding runs on the first \(pinned) of the server's \(online) cores and cannot use the other \(rest), even when they are idle.\(gap)At least 2 always stay free for the host."
    }

    static let cpuWeightHelp =
        "Weight matters only when coding and other work compete for the same cores.\u{00A0} 50 gives coding low priority; 100 is normal."

    /// What Pressure RAM Limit really does (RESOURCES-1):  with borrowing on it is a floor in
    /// normal operation and the limit only under pressure.
    static func pressureHelp(_ s: ResourcePolicySnapshot, pressure: Double) -> String {
        let held = "Under memory pressure coding is held to \(gib(pressure)), and in critical mode to \(gib(s.desired.ramCriticalGiB))."
        let lead: String
        if s.desired.allowBorrowing {
            if let o = s.observed, o.mode == "normal", o.codingGroups > 0, o.ramTargetBytes > 0 {
                lead = "Coding may use up to \(gib(bytes: o.ramTargetBytes)) right now because Borrow Idle Memory is on."
            } else {
                lead = "With Borrow Idle Memory on, coding may use more than this while the server has memory to spare."
            }
        } else {
            lead = "Borrow Idle Memory is off, so coding is held to \(gib(pressure)) in normal operation too."
        }
        return lead + gap + held + gap + "At its limit coding is slowed and its memory reclaimed; nothing is stopped or restarted."
    }

    static func criticalHelp(_ s: ResourcePolicySnapshot, critical: Double) -> String {
        let start: String
        if let level = s.desired.criticalAvailableGiB {
            start = "Critical mode starts when the server has less than \(gib(level)) of memory available, or when memory stalls get severe."
        } else {
            start = "Critical mode starts when the server runs very low on available memory."
        }
        return start + gap + "In critical mode coding is held to \(gib(critical)).\(gap)This limit does not decide when critical mode starts."
    }

    static func reserveHelp(_ s: ResourcePolicySnapshot, reserve: Double) -> String {
        "Coding borrows idle memory only until the server has \(gib(reserve)) available.\(gap)Below that, memory stalls can start pressure mode.\(gap)The reserve also comes out of the memory production is promised."
    }

    static func borrowHelp(on: Bool) -> String {
        on
            ? "In normal operation coding may grow past Pressure RAM Limit into memory nothing else is using, until only Host RAM Reserve is left.\(gap)Under pressure it is pulled back."
            : "Coding stays at Pressure RAM Limit even when the server has memory to spare."
    }

    /// Production's protected memory for the saved values,  and for a draft that changes it
    /// (RESOURCES-2).  `warning` is true when a draft lowers it.
    static func productionLine(_ s: ResourcePolicySnapshot, pressure: Double, reserve: Double) -> (text: String, warning: Bool)? {
        guard let now = s.productionProtectionGiB(),
              let then = s.productionProtectionGiB(pressure: pressure, reserve: reserve) else { return nil }
        if then == now {
            return ("Production is promised up to \(gib(now)) of protected memory: the server's memory, less Host RAM Reserve and Pressure RAM Limit.", false)
        }
        let sentence = "This change leaves production up to \(gib(then)) of protected memory (now up to \(gib(now)))."
        return then < now ? ("Warning: " + sentence, true) : (sentence, false)
    }

    /// Everything the page knows about why the status is what it is:  the named drift
    /// causes,  then each reason the server gave,  in plain words.
    static func explanation(_ s: ResourcePolicySnapshot) -> [String] {
        // Drift causes already name these,  with numbers.
        let covered: Set<String> = ["policy-not-in-effect", "cpu-mismatch", "memory-limit-mismatch", "cpu-values-differ"]
        var lines = s.status == "drift" ? driftCauses(s) : []
        let codes = s.status == "drift" ? s.reasons.filter { !covered.contains($0) } : s.reasons
        lines += codes.map { reasonSentence($0) }
        if lines.isEmpty && s.status != "applied" {
            lines.append("The server gave no reason.")
        }
        return lines
    }

    /// A custom CPU weight as typed,  or nil when it is not a whole number from 1 to 10,000.
    static func parseCPUWeight(_ text: String) -> Int? {
        let trimmed = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "")
        guard !trimmed.isEmpty, trimmed.allSatisfy({ $0.isASCII && $0.isNumber }),
              let number = Int(trimmed), (1...10_000).contains(number) else { return nil }
        return number
    }

    /// What differs,  named,  for a drift status (RESOURCES-11).
    static func driftCauses(_ s: ResourcePolicySnapshot) -> [String] {
        var causes: [String] = []
        if s.savedPolicyNotInEffect {
            causes.append("The saved policy is not yet in effect.\(gap)The memory controller is still running an older copy.")
        }
        guard let o = s.observed else { return causes }
        if o.cpuMismatches > 0 {
            causes.append("CPU settings differ from the saved policy on \(count(o.cpuMismatches, "group")).")
        }
        if o.actualHighMismatches > 0 {
            causes.append("Memory limits differ from the saved policy on \(count(o.actualHighMismatches, "coding group")).")
        }
        if let saved = s.desired.cpuCores, let live = o.cpuCores, saved != live {
            causes.append("\(live) cores are in effect; the saved policy says \(saved).")
        }
        if let saved = s.desired.cpuWeight, let live = o.cpuWeight, saved != live {
            causes.append("CPU weight \(live) is in effect; the saved policy says \(saved).")
        }
        return causes
    }

    /// One plain sentence for each reason code the server sends (RESOURCES-13).
    static func reasonSentence(_ code: String) -> String {
        let unit = "Check fleet-coding-memory.service in Server Status."
        switch code {
        case "receipt-missing":
            return "The memory controller has written no status receipt, so it may not be running.\(gap)\(unit)"
        case "receipt-stale":
            return "The memory controller's last receipt is more than a minute old, so it may have stopped.\(gap)\(unit)"
        case "receipt-incomplete":
            return "The memory controller's receipt is missing measurements."
        case "receipt-invalid":
            return "The memory controller's receipt has values this server cannot have."
        case "saved-policy-unreadable":
            return "The saved policy could not be read from its source, so the values shown are the controller's copy."
        case "policy-not-in-effect":
            return "The saved policy is not yet in effect.\(gap)The memory controller is still running an older copy."
        case "cpu-values-differ":
            return "The CPU cores or weight in effect differ from the saved policy."
        case "no-controller-report":
            return "The memory controller has not finished its first memory check."
        case "policy-stale":
            return "The memory controller could not refresh the saved policy and is running an older copy."
        case "no-coding-groups":
            return "No coding services are running, so there is nothing to limit."
        case "cpu-controls-unreadable":
            return "The memory controller could not read the CPU controls of the coding services."
        case "no-online-cpus":
            return "The memory controller could not count the server's cores."
        case "no-production-groups":
            return "The memory controller found no production services to protect."
        case "production-unprotected":
            return "Some production services get no protected memory, because they have no memory cap."
        case "cpu-policy-missing":
            return "The saved policy sets no CPU cores or CPU weight, so CPU limits are not managed."
        case "control-write-failures":
            return "The memory controller could not write some limits."
        case "controls-suspended":
            return "Some controls are suspended by hand on the server."
        case "cpu-mismatch":
            return "CPU settings on some coding services differ from the saved policy."
        case "memory-limit-mismatch":
            return "Memory limits on some coding services differ from the saved policy."
        default:
            return "The server gave a reason this app does not recognize (\(code))."
        }
    }

    /// The "What The Server Is Doing" rows,  in plain words (RESOURCES-20,  RESOURCES-21).
    static func receiptRows(_ s: ResourcePolicySnapshot) -> [ResourceReceiptRow] {
        guard let o = s.observed else { return [] }
        var rows: [ResourceReceiptRow] = []
        let limit = o.ramTargetBytes > 0 ? "of a \(gib(bytes: o.ramTargetBytes)) limit" : "with no coding limit set"
        rows.append(.init(title: "Coding Memory In Use", value: "\(gib(bytes: o.ramCurrentBytes)) \(limit)", kind: .primary))
        if let available = o.availableBytes {
            let total = o.ramTotalBytes.map { " of \(gib(bytes: $0))" } ?? ""
            rows.append(.init(title: "Memory Available", value: "\(gib(bytes: available))\(total)", kind: .primary))
        }
        rows.append(.init(title: "Coding Services Limited", value: "\(o.codingGroups)", kind: .primary))
        if let protected = o.productionProtectedGroups {
            rows.append(.init(title: "Production Services Protected", value: "\(protected) of \(o.productionGroups)", kind: .primary))
        } else {
            rows.append(.init(title: "Production Services", value: "\(o.productionGroups)", kind: .primary))
        }
        func counter(_ title: String, _ value: Int?) {
            guard let value else { return }
            rows.append(.init(title: title, value: "\(value)", kind: value > 0 ? .problem : .detail))
        }
        counter("Production Services Without Protection", o.productionUnprotectedGroups)
        counter("Out-Of-Memory Kills Since The Last Check", o.oomKillDelta)
        counter("Limits That Could Not Be Written", o.controlWriteFailures)
        counter("Controls Suspended By Hand", o.controlSuspended)
        counter("CPU Groups That Differ", o.cpuMismatches)
        counter("Memory Limits That Differ", o.actualHighMismatches)
        rows.append(.init(title: "Saved Policy Copy Out Of Date", value: o.policyStale ? "Yes" : "No", kind: o.policyStale ? .problem : .detail))
        if let exempt = o.excludedUnits {
            rows.append(.init(title: "Services Exempt From These Limits", value: "\(exempt)", kind: .detail))
        }
        rows.append(.init(title: "CPU Groups Checked", value: "\(o.cpuGroups)", kind: .detail))
        return rows
    }

    /// When the controller measured,  on Jay's clock.
    static func measuredText(_ s: ResourcePolicySnapshot) -> String {
        guard let text = s.observed?.updatedAt, let date = ResourcePolicyReading.parseDate(text) else {
            return "The controller's measurement time is unknown."
        }
        return "Measured by the memory controller at \(FleetTime.clock(date))."
    }
}

enum ResourcePolicyReadState: Equatable {
    case loading
    case current
    case stale
    case unavailable
    case invalid
    /// Read only,  from the server report the Mac pulls,  because the direct read failed.
    case fromReport

    var title: String {
        switch self {
        case .loading: return "Reading The Server"
        case .current: return "Live From The Server"
        case .stale: return "Out Of Date"
        case .unavailable: return "Not Confirmed"
        case .invalid: return "Unreadable Report"
        case .fromReport: return "From The Server Report"
        }
    }
}

struct ResourcePolicyReading: Equatable {
    static let maxReceiptAge: TimeInterval = 60
    static let maxCollectorAge: TimeInterval = 45 * 60
    /// How far ahead of the Mac's clock a server timestamp may be and still count as now.
    /// The same 60 seconds the server report,  tools,  worker and Mac service readers allow.
    static let clockSkewTolerance: TimeInterval = 60
    static let supportedSchemaVersion = 1

    let snapshot: ResourcePolicySnapshot?
    let state: ResourcePolicyReadState
    let reason: String?
    let receiptAge: TimeInterval?
    /// Age of the source report's `generatedAt` timestamp.
    let collectorAge: TimeInterval?
    let directReceiptAt: Date?
    /// Age of the collector's actual observation timestamp, when the report carries one.
    let observationAge: TimeInterval?
    /// When the server report this reading came from was written,  for a reading taken from it.
    let reportAt: Date?

    init(
        snapshot: ResourcePolicySnapshot?,
        state: ResourcePolicyReadState,
        reason: String?,
        receiptAge: TimeInterval?,
        collectorAge: TimeInterval?,
        directReceiptAt: Date?,
        observationAge: TimeInterval? = nil,
        reportAt: Date? = nil
    ) {
        self.snapshot = snapshot
        self.state = state
        self.reason = reason
        self.receiptAge = receiptAge
        self.collectorAge = collectorAge
        self.directReceiptAt = directReceiptAt
        self.observationAge = observationAge
        self.reportAt = reportAt
    }

    /// The page before its first read has finished.
    static let loading = ResourcePolicyReading(
        snapshot: nil, state: .loading, reason: nil, receiptAge: nil, collectorAge: nil, directReceiptAt: nil
    )

    var canReviewChanges: Bool { canReviewChanges(at: Date()) }

    func canReviewChanges(at date: Date) -> Bool {
        guard state == .current, let snapshot, snapshot.status != "unavailable",
              snapshot.observed != nil, snapshot.reviewRevision != nil,
              let directReceiptAt else { return false }
        let age = date.timeIntervalSince(directReceiptAt)
        return age >= 0 && age <= Self.maxReceiptAge
    }

    func state(at date: Date) -> ResourcePolicyReadState {
        guard state == .current, let directReceiptAt else { return state }
        let age = date.timeIntervalSince(directReceiptAt)
        return age < 0 || age > Self.maxReceiptAge ? .stale : .current
    }

    func reason(at date: Date) -> String? {
        if state(at: date) == .stale, state == .current {
            return "The direct read is older than 60 seconds.\u{00A0} Refresh before reviewing a change."
        }
        return reason
    }

    static func unavailable(_ reason: String = "The server did not return a resource policy receipt.") -> Self {
        .init(snapshot: nil, state: .unavailable, reason: reason, receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
    }

    private static func invalid(_ reason: String, collectorAge: TimeInterval? = nil) -> Self {
        .init(snapshot: nil, state: .invalid, reason: reason, receiptAge: nil, collectorAge: collectorAge, directReceiptAt: nil)
    }

    private static func isRevision(_ value: String) -> Bool {
        value.count == 64 && value.allSatisfy { $0.isASCII && $0.isHexDigit }
    }

    static func decode(_ data: Data, now: Date = Date(), directReceiptAt: Date? = nil) -> Self {
        do {
            // Ops replies wrap the report under `resourcePolicy`; tests and fixtures may
            // also pass the snapshot itself.
            let root = try JSONSerialization.jsonObject(with: data)
            let payload: Data
            if let object = root as? [String: Any], let nested = object["resourcePolicy"] {
                payload = try JSONSerialization.data(withJSONObject: nested)
            } else {
                payload = data
            }
            let snapshot = try JSONDecoder().decode(ResourcePolicySnapshot.self, from: payload)
            guard snapshot.schemaVersion == supportedSchemaVersion else {
                return invalid("This policy report uses an unsupported format.")
            }
            let revisions = [snapshot.policyRevision, snapshot.desiredRevision].compactMap { $0 }
            guard revisions.allSatisfy({ isRevision($0) }) else {
                return invalid("The policy revision is malformed.")
            }
            guard ["applied", "drift", "unavailable"].contains(snapshot.status) else {
                return invalid("The server returned an unknown policy status.")
            }
            // A report the server stands behind names the revision a change is checked against.
            guard snapshot.reviewRevision != nil || snapshot.status == "unavailable" else {
                return invalid("The policy revision is missing.")
            }
            // Measurements are what an applied or drift status rests on.
            guard snapshot.observed != nil || snapshot.status == "unavailable" else {
                return invalid("The server reported the policy \(snapshot.status) but sent no complete measurements.")
            }
            guard let generatedAt = parseDate(snapshot.generatedAt) else {
                return invalid("The policy report has no readable source timestamp.")
            }
            guard numericValuesAreValid(snapshot) else {
                return invalid("The policy report contains an invalid numeric value.")
            }
            let sourceAge = now.timeIntervalSince(generatedAt)
            guard sourceAge >= -clockSkewTolerance else {
                return invalid("The policy source timestamp is more than a minute ahead of this Mac's clock.", collectorAge: sourceAge)
            }
            var observationAge: TimeInterval?
            if let updatedAt = snapshot.observed?.updatedAt {
                guard let parsed = parseDate(updatedAt) else {
                    return invalid("The collector timestamp could not be read.")
                }
                let age = now.timeIntervalSince(parsed)
                guard age >= -clockSkewTolerance else {
                    return invalid("The collector timestamp is more than a minute ahead of this Mac's clock.", collectorAge: age)
                }
                observationAge = max(0, age)
            }
            // A server clock slightly ahead of the Mac's reads as now,  not as the future.
            let collectorAge = max(0, sourceAge)
            guard let directReceiptAt else {
                return .init(snapshot: snapshot, state: .unavailable, reason: "No direct server read time is available.\u{00A0} Refresh before reviewing changes.", receiptAge: nil, collectorAge: collectorAge, directReceiptAt: nil, observationAge: observationAge)
            }
            let receiptAge = now.timeIntervalSince(directReceiptAt)
            if receiptAge < 0 {
                return .init(snapshot: nil, state: .invalid, reason: "The direct read timestamp is in the future.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: nil)
            }
            if snapshot.status == "unavailable" {
                return .init(snapshot: snapshot, state: .unavailable, reason: "The server cannot confirm these limits are in effect, so they cannot be changed here until it can.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt, observationAge: observationAge)
            }
            if snapshot.observed?.policyStale == true {
                return .init(snapshot: snapshot, state: .stale, reason: "The server marks the policy source stale.\u{00A0} Refresh after the policy is current.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt, observationAge: observationAge)
            }
            if collectorAge > maxCollectorAge {
                return .init(snapshot: snapshot, state: .stale, reason: "The resource policy source is older than 45 minutes.\u{00A0} Refresh the server report before reviewing changes.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt, observationAge: observationAge)
            }
            if let observationAge, observationAge > maxCollectorAge {
                return .init(snapshot: snapshot, state: .stale, reason: "The collector observation is older than 45 minutes.\u{00A0} Refresh the server report before reviewing changes.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt, observationAge: observationAge)
            }
            if receiptAge > maxReceiptAge {
                return .init(snapshot: snapshot, state: .stale, reason: "The direct read is older than 60 seconds.\u{00A0} Refresh before reviewing a change.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt, observationAge: observationAge)
            }
            return .init(snapshot: snapshot, state: .current, reason: nil, receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt, observationAge: observationAge)
        } catch {
            return invalid("The resource policy report could not be read.")
        }
    }

    /// The policy copy inside the server report the Mac pulls,  read only,  for when the
    /// direct read fails (RESOURCES-22).  Nil when the report carries no usable copy.
    static func fromServerReport(_ report: ServerStatusReading, directFailure: String?, now: Date = Date()) -> Self? {
        guard let carried = report.resourcePolicy, let reportAt = report.generatedAt,
              let data = try? JSONEncoder().encode(carried) else { return nil }
        let decoded = decode(data, now: now, directReceiptAt: nil)
        guard let snapshot = decoded.snapshot else { return nil }
        let gap = ResourceLimitsCopy.gap
        var reason = "Showing the server report from \(FleetTime.clock(reportAt)), read only.\(gap)Refresh when the server is reachable to change limits."
        if let directFailure, !directFailure.isEmpty {
            reason = directFailure + gap + reason
        }
        return .init(snapshot: snapshot, state: .fromReport, reason: reason, receiptAge: nil,
                     collectorAge: decoded.collectorAge, directReceiptAt: nil,
                     observationAge: decoded.observationAge, reportAt: reportAt)
    }

    private static func numericValuesAreValid(_ s: ResourcePolicySnapshot) -> Bool {
        let desired = [s.desired.ramPressureGiB, s.desired.ramCriticalGiB, s.desired.reserveGiB]
        guard desired.allSatisfy({ $0.isFinite && $0 > 0 }) else { return false }
        for level in [s.desired.pressureAvailableGiB, s.desired.criticalAvailableGiB] {
            if let level, !(level.isFinite && level >= 0) { return false }
        }
        // Fractional GiB values are legal on the server:  they are shown as they are,  and
        // only a new value from this page has to be whole.
        guard s.desired.ramCriticalGiB <= s.desired.ramPressureGiB else { return false }
        if let cores = s.desired.cpuCores, cores < 1 { return false }
        if let weight = s.desired.cpuWeight, !(1...10_000).contains(weight) { return false }
        guard let observed = s.observed else { return true }
        let counters: [Int?] = [
            observed.cpuOnline, observed.cpuCores, observed.cpuWeight, observed.cpuGroups,
            observed.cpuMismatches, observed.ramTotalBytes, observed.ramCurrentBytes,
            observed.ramTargetBytes, observed.codingGroups, observed.productionGroups,
            observed.controlWriteFailures, observed.controlSuspended, observed.actualHighMismatches,
            observed.availableBytes, observed.productionProtectedGroups,
            observed.productionUnprotectedGroups, observed.oomKillDelta, observed.excludedUnits,
        ]
        guard counters.compactMap({ $0 }).allSatisfy({ $0 >= 0 }) else { return false }
        if observed.cpuOnline < 3 { return false }
        if let cores = s.desired.cpuCores, cores > observed.cpuOnline - 2 { return false }
        if let weight = observed.cpuWeight, !(1...10_000).contains(weight) { return false }
        if let total = observed.ramTotalBytes {
            if total == 0 { return false }
            if observed.ramCurrentBytes > total || observed.ramTargetBytes > total { return false }
        }
        // The controller writes a zero target when no coding service is running,  and only then.
        if observed.ramTargetBytes == 0 && observed.codingGroups > 0 { return false }
        return true
    }

    /// Reads the server's timestamps.  Python writes 6 fractional digits and Coolify 8 or 9,
    /// and the formatter is only promised to read 3,  so a longer fraction is cut to
    /// milliseconds first.
    static func parseDate(_ string: String) -> Date? {
        let text = millisecondPrecision(string)
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: text) { return date }
        let ordinary = ISO8601DateFormatter()
        ordinary.formatOptions = [.withInternetDateTime]
        return ordinary.date(from: text)
    }

    static func millisecondPrecision(_ string: String) -> String {
        guard let dot = string.firstIndex(of: ".") else { return string }
        let start = string.index(after: dot)
        guard let end = string[start...].firstIndex(where: { !($0.isASCII && $0.isNumber) }) else { return string }
        let digits = string[start..<end]
        guard digits.count > 3 else { return string }
        return String(string[..<start]) + String(digits.prefix(3)) + String(string[end...])
    }
}
