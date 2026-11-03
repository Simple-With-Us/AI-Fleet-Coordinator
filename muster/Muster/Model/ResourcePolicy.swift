import Foundation

/// The direct receipt Muster accepts for the server's coding resource policy.
///
/// Desired policy is not evidence that a workload adopted the policy.  The observed
/// fields and their timestamps stay separate so a stale or missing receipt cannot
/// read as a successful apply.
struct ResourcePolicySnapshot: Codable, Equatable {
    let schemaVersion: Int
    let generatedAt: String
    let policyRevision: String
    let desired: Desired
    let observed: Observed
    let status: String

    struct Desired: Codable, Equatable {
        let cpuCores: Int?
        let cpuWeight: Int?
        let ramPressureGiB: Double
        let ramCriticalGiB: Double
        let reserveGiB: Double
        let allowBorrowing: Bool
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
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion, generatedAt, policyRevision, desired, observed, status
    }

    /// The current collector contract.  Missing optional measurements remain nil and
    /// are rendered as unknown; required receipt fields fail the whole read closed.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try c.decode(Int.self, forKey: .schemaVersion)
        generatedAt = try c.decode(String.self, forKey: .generatedAt)
        policyRevision = try c.decode(String.self, forKey: .policyRevision)
        desired = try c.decode(Desired.self, forKey: .desired)
        observed = try c.decode(Observed.self, forKey: .observed)
        status = try c.decode(String.self, forKey: .status)
    }
}

extension ResourcePolicySnapshot.Desired {
    enum CodingKeys: String, CodingKey {
        case cpuCores, cpuWeight, ramPressureGiB, ramCriticalGiB, reserveGiB, allowBorrowing
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cpuCores = try c.decodeIfPresent(Int.self, forKey: .cpuCores)
        cpuWeight = try c.decodeIfPresent(Int.self, forKey: .cpuWeight)
        ramPressureGiB = try c.decode(Double.self, forKey: .ramPressureGiB)
        ramCriticalGiB = try c.decode(Double.self, forKey: .ramCriticalGiB)
        reserveGiB = try c.decode(Double.self, forKey: .reserveGiB)
        allowBorrowing = try c.decode(Bool.self, forKey: .allowBorrowing)
    }
}

extension ResourcePolicySnapshot.Observed {
    enum CodingKeys: String, CodingKey {
        case cpuOnline, cpuCores, cpuWeight, cpuGroups, cpuMismatches
        case ramTotalBytes, ramCurrentBytes, ramTargetBytes, mode, codingGroups
        case productionGroups, controlWriteFailures, controlSuspended, policyStale
        case actualHighMismatches, updatedAt
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

    var title: String {
        switch self {
        case .cpuCores: return "CPU Cores"
        case .cpuWeight: return "CPU Weight"
        case .ramBudgetGiB: return "RAM Budget"
        case .ramCriticalGiB: return "RAM Critical Threshold"
        case .ramReserveGiB: return "Host RAM Reserve"
        case .borrowIdle: return "Borrow Idle Capacity"
        }
    }

    var unit: String {
        switch self {
        case .cpuCores: return "cores"
        case .cpuWeight: return "weight"
        case .ramBudgetGiB, .ramCriticalGiB, .ramReserveGiB: return "GiB"
        case .borrowIdle: return ""
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

    /// Check a proposed single-field change against the persisted counterpart values.
    /// Drafts in other controls never become the limits for this validation.
    func validate(value: Double, in snapshot: ResourcePolicySnapshot) -> String? {
        guard value.isFinite else { return "Enter a finite value." }
        switch self {
        case .cpuCores:
            let online = snapshot.observed.cpuOnline
            guard value.rounded() == value, value >= 1, value <= Double(online - 2) else {
                return "Choose 1 through \(max(0, online - 2)) cores to leave two host cores available."
            }
        case .cpuWeight:
            guard value.rounded() == value, value >= 1, value <= 10_000 else {
                return "Choose a CPU weight from 1 to 10,000."
            }
        case .ramBudgetGiB:
            guard let total = totalMemoryGiB(in: snapshot) else { return "Total host memory is unknown." }
            guard value.rounded() == value, value >= 2, value <= floor(total - 2) else {
                return "Choose a whole GiB value of at least 2 and leave 2 GiB for the host."
            }
            let critical = snapshot.desired.ramCriticalGiB
            guard critical <= value else { return "The persisted critical threshold exceeds this budget." }
        case .ramCriticalGiB:
            guard value.rounded() == value, value >= 1 else { return "Choose a whole GiB value of at least 1." }
            let budget = snapshot.desired.ramPressureGiB
            guard value <= budget else { return "The threshold cannot exceed the persisted RAM budget." }
        case .ramReserveGiB:
            guard let total = totalMemoryGiB(in: snapshot) else { return "Total host memory is unknown." }
            guard value.rounded() == value, value >= 2, value <= floor(total - 2) else {
                return "Choose a whole GiB value and leave at least 2 GiB for the host."
            }
        case .borrowIdle:
            guard value == 0 || value == 1 else { return "Choose enabled or disabled." }
        }
        return nil
    }

    private func totalMemoryGiB(in snapshot: ResourcePolicySnapshot) -> Double? {
        guard let bytes = snapshot.observed.ramTotalBytes, bytes > 0 else { return nil }
        return Double(bytes) / 1_073_741_824
    }
}

enum ResourcePolicyReadState: Equatable {
    case loading
    case current
    case stale
    case unavailable
    case invalid

    var title: String {
        switch self {
        case .loading: return "Reading Policy"
        case .current: return "Current Receipt"
        case .stale: return "Stale Receipt"
        case .unavailable: return "Unavailable"
        case .invalid: return "Unknown Policy"
        }
    }
}

struct ResourcePolicyReading: Equatable {
    static let maxReceiptAge: TimeInterval = 60
    static let maxCollectorAge: TimeInterval = 45 * 60
    static let supportedSchemaVersion = 1

    let snapshot: ResourcePolicySnapshot?
    let state: ResourcePolicyReadState
    let reason: String?
    let receiptAge: TimeInterval?
    let collectorAge: TimeInterval?
    let directReceiptAt: Date?

    var canReviewChanges: Bool { canReviewChanges(at: Date()) }

    func canReviewChanges(at date: Date) -> Bool {
        guard state == .current, snapshot?.status != "unavailable",
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
            return "The direct receipt is older than 60 seconds.\u{00A0} Refresh before reviewing a change."
        }
        return reason
    }

    static func unavailable(_ reason: String = "The server did not return a resource policy receipt.") -> Self {
        .init(snapshot: nil, state: .unavailable, reason: reason, receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
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
                return .init(snapshot: nil, state: .invalid, reason: "This policy report uses an unsupported format.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
            }
            guard snapshot.policyRevision.count == 64,
                  snapshot.policyRevision.allSatisfy({ $0.isASCII && $0.isHexDigit }) else {
                return .init(snapshot: nil, state: .invalid, reason: "The policy revision is missing or malformed.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
            }
            guard ["applied", "drift", "unavailable"].contains(snapshot.status) else {
                return .init(snapshot: nil, state: .invalid, reason: "The server returned an unknown policy status.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
            }
            guard let generatedAt = parseDate(snapshot.generatedAt) else {
                return .init(snapshot: nil, state: .invalid, reason: "The policy report has no readable source timestamp.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
            }
            guard numericValuesAreValid(snapshot) else {
                return .init(snapshot: nil, state: .invalid, reason: "The policy report contains an invalid numeric value.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
            }
            let sourceAge = now.timeIntervalSince(generatedAt)
            guard sourceAge >= 0 else {
                return .init(snapshot: nil, state: .invalid, reason: "The policy source timestamp is in the future.", receiptAge: nil, collectorAge: sourceAge, directReceiptAt: nil)
            }
            if let updatedAt = snapshot.observed.updatedAt {
                guard let parsed = parseDate(updatedAt) else {
                    return .init(snapshot: nil, state: .invalid, reason: "The collector timestamp could not be read.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
                }
                if now.timeIntervalSince(parsed) < 0 {
                    return .init(snapshot: nil, state: .invalid, reason: "The collector timestamp is in the future.", receiptAge: nil, collectorAge: now.timeIntervalSince(parsed), directReceiptAt: nil)
                }
            }
            let collectorAge = sourceAge
            guard let directReceiptAt else {
                return .init(snapshot: snapshot, state: .unavailable, reason: "No direct server read time is available. Refresh before reviewing changes.", receiptAge: nil, collectorAge: collectorAge, directReceiptAt: nil)
            }
            let receiptAge = now.timeIntervalSince(directReceiptAt)
            if receiptAge < 0 {
                return .init(snapshot: nil, state: .invalid, reason: "The direct read timestamp is in the future.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: nil)
            }
            if snapshot.status == "unavailable" {
                return .init(snapshot: snapshot, state: .unavailable, reason: "The server reported that resource policy is unavailable.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt)
            }
            if snapshot.observed.policyStale {
                return .init(snapshot: snapshot, state: .stale, reason: "The server marks the policy source stale.\u{00A0} Refresh after the policy is current.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt)
            }
            if receiptAge > maxReceiptAge {
                return .init(snapshot: snapshot, state: .stale, reason: "The direct receipt is older than 60 seconds.\u{00A0} Refresh before reviewing a change.", receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt)
            }
            return .init(snapshot: snapshot, state: .current, reason: nil, receiptAge: receiptAge, collectorAge: collectorAge, directReceiptAt: directReceiptAt)
        } catch {
            return .init(snapshot: nil, state: .invalid, reason: "The resource policy report could not be read.", receiptAge: nil, collectorAge: nil, directReceiptAt: nil)
        }
    }

    private static func numericValuesAreValid(_ s: ResourcePolicySnapshot) -> Bool {
        let desired = [s.desired.ramPressureGiB, s.desired.ramCriticalGiB, s.desired.reserveGiB]
        guard desired.allSatisfy({ $0.isFinite && $0 >= 0 }) else { return false }
        let observed = s.observed
        let counters = [observed.cpuOnline, observed.cpuCores, observed.cpuWeight, observed.cpuGroups,
                        observed.cpuMismatches, observed.ramTotalBytes, observed.ramCurrentBytes,
                        observed.ramTargetBytes, observed.codingGroups, observed.productionGroups,
                        observed.controlWriteFailures, observed.controlSuspended, observed.actualHighMismatches]
        guard counters.compactMap({ $0 }).allSatisfy({ $0 >= 0 }) else { return false }
        if observed.cpuOnline < 3 { return false }
        if let cores = s.desired.cpuCores, !(1...(observed.cpuOnline - 2)).contains(cores) { return false }
        if let weight = s.desired.cpuWeight, !(1...10_000).contains(weight) { return false }
        if let weight = observed.cpuWeight, !(1...10_000).contains(weight) { return false }
        if let bytes = observed.ramTotalBytes, bytes == 0 { return false }
        if let total = observed.ramTotalBytes {
            let maxBudget = floor(Double(total) / 1_073_741_824 - 2)
            if s.desired.ramPressureGiB < 2 || s.desired.ramPressureGiB > maxBudget { return false }
            if s.desired.reserveGiB < 2 || s.desired.reserveGiB > maxBudget || s.desired.reserveGiB.rounded() != s.desired.reserveGiB { return false }
        }
        let critical = s.desired.ramCriticalGiB
        guard critical >= 1, critical.rounded() == critical, critical <= s.desired.ramPressureGiB else { return false }
        if let total = observed.ramTotalBytes, observed.ramCurrentBytes > total { return false }
        if let total = observed.ramTotalBytes, observed.ramTargetBytes > total { return false }
        return true
    }

    private static func parseDate(_ string: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: string) { return date }
        let ordinary = ISO8601DateFormatter()
        ordinary.formatOptions = [.withInternetDateTime]
        return ordinary.date(from: string)
    }
}
