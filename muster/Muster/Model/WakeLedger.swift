import Foundation

/// The seat's wake ledger:  `~/.agent-sync/<SEAT>/wakes.jsonl`.
///
/// One row per state change of one wake.  The panel counts the wakes the runtime counts,
/// so the number it shows is the number the budget is measured against.
enum WakeLedger {
    /// One wake,  merged from its rows.
    struct Wake: Equatable {
        /// The last state the wake reached.
        var state: String
        /// Whether the owner asked for it.
        var owner: Bool
        /// The channel and topic it belongs to.
        var topicKey: String?
        /// When it was accepted,  or when it was started when it never was.
        var at: Date?
    }

    /// One row of the ledger.
    private struct Row: Decodable {
        var ts: Double?
        var wake_id: String?
        var state: String?
        var owner: Bool?
        var topic_key: String?
        var topic: String?
        var channel: String?
    }

    /// One wake,  as the panel shows it.
    struct Outcome: Equatable, Identifiable {
        /// The wake's identifier.
        let id: String
        /// The state the wake reached.
        let state: String
        /// The topic it belongs to.
        let topic: String?
        /// Whether the owner asked for it.
        let owner: Bool
        /// When it was accepted.
        let at: Date?
    }

    /// The states a budget counts.
    static let countedStates: Set<String> = ["accepted", "started", "done", "failed"]

    /// The wakes in the ledger,  merged.
    ///
    /// Later rows win for the fields,  and the first time each state was reached is kept:
    /// that is the runtime's own rule,  so a wake that was accepted and then failed counts
    /// from the moment it was accepted.
    static func wakes(from text: String) -> [Wake] {
        var merged: [String: (state: String, owner: Bool, topicKey: String?, times: [String: Double])] = [:]
        let decoder = JSONDecoder()
        for line in text.split(whereSeparator: \.isNewline) {
            guard let data = line.data(using: .utf8),
                  let row = try? decoder.decode(Row.self, from: data),
                  let identifier = row.wake_id else { continue }
            var view = merged[identifier] ?? ("", false, nil, [:])
            if let state = row.state { view.state = state }
            if let owner = row.owner { view.owner = owner }
            if let topicKey = row.topic_key { view.topicKey = topicKey }
            if let ts = row.ts, let state = row.state, view.times[state] == nil {
                view.times[state] = ts
            }
            merged[identifier] = view
        }
        return merged.values.map { view in
            let accepted = view.times["accepted"] ?? view.times["started"]
            return Wake(
                state: view.state,
                owner: view.owner,
                topicKey: view.topicKey,
                at: accepted.map { Date(timeIntervalSince1970: $0) }
            )
        }
    }

    /// The last few wakes,  newest first.
    ///
    /// This is what the panel shows of the seat's recent wakes and their outcomes,  so an
    /// unanswered message is visible next to the setting that would have answered it.
    static func outcomes(from text: String, limit: Int = 8) -> [Outcome] {
        let decoder = JSONDecoder()
        var merged: [String: Outcome] = [:]
        for line in text.split(whereSeparator: \.isNewline) {
            guard let data = line.data(using: .utf8),
                  let row = try? decoder.decode(Row.self, from: data),
                  let identifier = row.wake_id else { continue }
            var outcome = merged[identifier] ?? Outcome(
                id: identifier,
                state: row.state ?? "",
                topic: row.topic,
                owner: row.owner ?? false,
                at: row.ts.map { Date(timeIntervalSince1970: $0) }
            )
            if let state = row.state { outcome = Outcome(
                id: identifier,
                state: state,
                topic: row.topic ?? outcome.topic,
                owner: row.owner ?? outcome.owner,
                at: row.ts.map { Date(timeIntervalSince1970: $0) } ?? outcome.at
            ) }
            merged[identifier] = outcome
        }
        return merged.values
            .sorted { ($0.at ?? .distantPast) > ($1.at ?? .distantPast) }
            .prefix(limit)
            .map { $0 }
    }

    /// How much of the budget the seat has used.
    ///
    /// The counting rule is the runtime's:  a wake counts from the moment it was accepted,
    /// and the owner's budget and everybody else's are counted apart.
    static func usage(from text: String, now: Date) -> WakeUsage {
        let wakes = self.wakes(from: text)
        let day: TimeInterval = 86_400
        let hour: TimeInterval = 3_600

        func within(_ wake: Wake, _ window: TimeInterval) -> Bool {
            guard let at = wake.at else { return false }
            return now.timeIntervalSince(at) < window
        }
        func counted(owner: Bool) -> [Wake] {
            wakes.filter {
                $0.owner == owner && countedStates.contains($0.state) && within($0, day)
            }
        }

        let ordinary = counted(owner: false)
        let owner = counted(owner: true)
        var usage = WakeUsage()
        usage.wakesThisHour = ordinary.filter { within($0, hour) }.count
        usage.wakesThisDay = ordinary.count
        usage.ownerThisDay = owner.count
        usage.worstTopicThisHour = busiest(ordinary.filter { within($0, hour) })
        usage.worstOwnerTopicThisHour = busiest(owner.filter { within($0, hour) })
        return usage
    }

    /// The busiest topic's count.
    private static func busiest(_ wakes: [Wake]) -> Int {
        var counts: [String: Int] = [:]
        for wake in wakes {
            guard let topic = wake.topicKey else { continue }
            counts[topic, default: 0] += 1
        }
        return counts.values.max() ?? 0
    }
}
