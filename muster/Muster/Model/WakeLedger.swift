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

    /// The ledger read once.
    ///
    /// The panel needs two things from the same rows:  the merged wakes behind the counters,
    /// and the last few wakes behind the list.  Reading the file and decoding it twice cost
    /// a second full parse of a file that grows for the life of the seat,  and it was paid on
    /// every refresh and again after every apply,  pause,  resume and pin.
    struct Parsed {
        /// Every wake in the ledger,  merged from its rows.
        var wakes: [Wake] = []
        /// The last few wakes,  newest first.
        var outcomes: [Outcome] = []
        /// How many rows were not valid ledger rows.
        ///
        /// A row can be half-written when the daemon dies mid-append,  so this is counted
        /// rather than thrown:  a torn last line must not blank the panel.  It is not left
        /// silent either:  the reader says how many rows it could not read,  so a ledger that
        /// has gone bad is visible instead of quietly under-counting the budget.
        var malformed: Int = 0
    }

    /// Reads the ledger in one pass.
    static func parse(_ text: String, limit: Int = 8) -> Parsed {
        var states: [String: String] = [:]
        var owners: [String: Bool] = [:]
        var topicKeys: [String: String?] = [:]
        var topics: [String: String?] = [:]
        var times: [String: [String: Double]] = [:]
        var lastAt: [String: Double] = [:]
        var order: [String] = []
        var malformed = 0
        let decoder = JSONDecoder()

        for line in text.split(whereSeparator: \.isNewline) {
            guard let data = line.data(using: .utf8),
                  let row = try? decoder.decode(Row.self, from: data),
                  let identifier = row.wake_id, !identifier.isEmpty,
                  let ts = row.ts, ts.isFinite else {
                malformed += 1
                continue
            }
            if times[identifier] == nil {
                times[identifier] = [:]
                order.append(identifier)
            }
            if let state = row.state {
                states[identifier] = state
                if times[identifier]?[state] == nil { times[identifier]?[state] = ts }
            }
            if let owner = row.owner { owners[identifier] = owner }
            if row.topic_key != nil { topicKeys[identifier] = row.topic_key }
            if row.topic != nil { topics[identifier] = row.topic }
            lastAt[identifier] = ts
        }

        var parsed = Parsed()
        parsed.malformed = malformed
        for identifier in order {
            let accepted = times[identifier]?["accepted"] ?? times[identifier]?["started"]
            parsed.wakes.append(Wake(
                state: states[identifier] ?? "",
                owner: owners[identifier] ?? false,
                topicKey: topicKeys[identifier] ?? nil,
                at: accepted.map { Date(timeIntervalSince1970: $0) }
            ))
            parsed.outcomes.append(Outcome(
                id: identifier,
                state: states[identifier] ?? "",
                topic: topics[identifier] ?? nil,
                owner: owners[identifier] ?? false,
                at: lastAt[identifier].map { Date(timeIntervalSince1970: $0) }
            ))
        }
        parsed.outcomes.sort { ($0.at ?? .distantPast) > ($1.at ?? .distantPast) }
        parsed.outcomes = Array(parsed.outcomes.prefix(limit))
        return parsed
    }

    /// The wakes in the ledger,  merged.
    ///
    /// Later rows win for the fields,  and the first time each state was reached is kept:
    /// that is the runtime's own rule,  so a wake that was accepted and then failed counts
    /// from the moment it was accepted.
    static func wakes(from text: String) -> [Wake] {
        parse(text).wakes
    }

    /// The last few wakes,  newest first.
    ///
    /// This is what the panel shows of the seat's recent wakes and their outcomes,  so an
    /// unanswered message is visible next to the setting that would have answered it.
    static func outcomes(from text: String, limit: Int = 8) -> [Outcome] {
        parse(text, limit: limit).outcomes
    }

    /// How much of the budget the seat has used.
    ///
    /// The counting rule is the runtime's:  a wake counts from the moment it was accepted,
    /// and the owner's budget and everybody else's are counted apart.
    static func usage(from text: String, now: Date) -> WakeUsage {
        usage(from: parse(text), now: now)
    }

    /// How much of the budget the seat has used,  from a ledger already read once.
    static func usage(from parsed: Parsed, now: Date) -> WakeUsage {
        let wakes = parsed.wakes
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
