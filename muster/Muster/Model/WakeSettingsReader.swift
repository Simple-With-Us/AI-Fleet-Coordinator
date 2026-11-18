import Foundation

// Reading the wake settings:  the configuration file,  the eavesdropper's own status,  the kill
// switch,  and the ledger that says how much of the budget is used.
//
// Nothing here decides anything.  It reads,  and hands what it read to the builder.

// MARK: - Where the state lives

/// Where the eavesdropper keeps its state on this Mac.
struct ListenerStateLocation: Equatable {
    /// The eavesdropper's state directory:  `~/.simple-tools`.
    var root: URL

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.root = home.appendingPathComponent(".simple-tools", isDirectory: true)
    }

    /// The configuration file.
    var configURL: URL {
        root.appendingPathComponent("listener.toml")
    }

    /// The eavesdropper's own published state.
    var statusURL: URL {
        root.appendingPathComponent("listener/status.json")
    }

    /// The kill switch.
    var pauseURL: URL {
        root.appendingPathComponent("listener/pause.json")
    }

    /// One seat's directory.
    func seatDirectory(_ tag: String) -> URL {
        root.appendingPathComponent(tag, isDirectory: true)
    }

    /// The seat's wake ledger.
    func ledgerURL(seat: String) -> URL {
        seatDirectory(seat).appendingPathComponent("wakes.jsonl")
    }
}

// MARK: - The ledger cache

/// Wake ledgers already parsed, by path, so an unchanged ledger is not decoded on every read.
///
/// It is shared by the reads one store starts, which run off the main thread, so it holds a
/// lock.  An entry is used only while the ledger's size and modification time are the ones it
/// was parsed at, and for no longer than `maximumAge`.
final class LedgerParseCache: @unchecked Sendable {
    /// How long a parse is reused at most.  Torn rows are judged against the last day at parse
    /// time, so they are judged again at least this often.
    static let maximumAge: TimeInterval = 600

    /// What identifies one version of a ledger.
    struct Stamp: Equatable {
        let size: UInt64
        let modified: Date
    }

    private struct Entry {
        let stamp: Stamp
        let parsedAt: Date
        let parsed: WakeLedger.Parsed
        let truncated: Bool
    }

    private let lock = NSLock()
    private var entries: [String: Entry] = [:]
    private var hitCount = 0

    init() {}

    /// How many reads reused a parse.
    var hits: Int {
        lock.lock()
        defer { lock.unlock() }
        return hitCount
    }

    /// The ledger's size and modification time, or nil when they cannot be read.
    static func stamp(of url: URL) -> Stamp? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attributes[.size] as? NSNumber)?.uint64Value,
              let modified = attributes[.modificationDate] as? Date else { return nil }
        return Stamp(size: size, modified: modified)
    }

    /// The parse kept for this version of the ledger, if it is young enough.
    func parsed(for path: String, stamp: Stamp, at now: Date) -> (parsed: WakeLedger.Parsed, truncated: Bool)? {
        lock.lock()
        defer { lock.unlock() }
        guard let entry = entries[path], entry.stamp == stamp else { return nil }
        let age = now.timeIntervalSince(entry.parsedAt)
        guard age >= 0, age < Self.maximumAge else { return nil }
        hitCount += 1
        return (entry.parsed, entry.truncated)
    }

    /// Keeps a parse of this version of the ledger.
    func keep(_ parsed: WakeLedger.Parsed, truncated: Bool, for path: String, stamp: Stamp, at now: Date) {
        lock.lock()
        defer { lock.unlock() }
        entries[path] = Entry(stamp: stamp, parsedAt: now, parsed: parsed, truncated: truncated)
    }

    /// Drops what is kept for a ledger that is gone or cannot be read.
    func forget(_ path: String) {
        lock.lock()
        defer { lock.unlock() }
        entries[path] = nil
    }
}

// MARK: - Reading

/// Reads everything the wake settings panel shows.
struct WakeSettingsReader {
    /// Where the state lives.
    var location: ListenerStateLocation
    /// The command line,  for the eavesdropper's own account of itself.
    var runner: SimpleToolsRunning
    /// The seats to show.
    var roster: [String]
    /// Where the seat partition is,  or nil when the checkout could not be found.
    var partitionURL: URL?
    /// The clock.
    var now: () -> Date
    /// Ledgers already parsed, so a read that finds a ledger unchanged does not decode it again.
    /// Nil parses every ledger on every read.
    var ledgerCache: LedgerParseCache?

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: SimpleToolsRunning = SimpleToolsCLI(),
        roster: [String] = [],
        partitionURL: URL? = ListenerPartition.defaultURL(),
        now: @escaping () -> Date = { Date() },
        ledgerCache: LedgerParseCache? = nil
    ) {
        self.location = location
        self.runner = runner
        self.roster = roster
        self.partitionURL = partitionURL
        self.now = now
        self.ledgerCache = ledgerCache
    }

    /// The maximum size of the configuration and pause files this reads.
    static let maximumFileBytes = 32 * 1024 * 1024
    /// The most of a wake ledger this reads, from its end.  The counters cover one day and the
    /// list shows eight wakes, while the ledger grows for the life of the seat, so only its tail
    /// is read, and the panel says when that tail does not reach back a day.
    static let maximumLedgerTailBytes = 4 * 1024 * 1024

    /// What reading one file found.
    enum FileRead: Equatable {
        /// The file is not there.
        case missing
        /// The file's text, and whether it is only the file's end.
        case text(String, truncated: Bool)
        /// The file is there and could not be read.  The text says why.
        case unreadable(String)
    }

    /// Reads everything,  and hands it to the builder.
    func read() async -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        var problems: [String] = []
        let readAt = now()
        sources.readAt = readAt

        // Who holds each seat.  The eavesdropper refuses a section it is not given,  so this comes
        // first:  every seat's row depends on it.
        sources.partition = ListenerPartition.load(from: partitionURL)
        sources.partitionURL = partitionURL

        // The configuration file.  A file that is not there yet and one that cannot be read are
        // told apart, and neither stops the status read:  the eavesdropper can be running either way.
        sources.configPath = location.configURL.path
        // Why a file that is there could not be read.  A file that is not there is known to hold
        // no Operator section, so only this leaves that section not known.
        var configUnreadable: String?
        switch readFile(location.configURL, limit: Self.maximumFileBytes) {
        case .text(let text, _):
            sources.config = ListenerConfigDocument(text: text)
            sources.configReadable = true
        case .missing:
            sources.config = ListenerConfigDocument(text: "")
            let problem = "No eavesdropper configuration file exists at \(location.configURL.path) yet."
            sources.configProblem = problem
            problems.append(problem)
        case .unreadable(let why):
            sources.config = ListenerConfigDocument(text: "")
            let problem = "The configuration file at \(location.configURL.path) could not be read:  \(why)"
            sources.configProblem = problem
            configUnreadable = problem
            problems.append(problem)
        }

        // The eavesdropper's own account of itself.
        var status: ListenerStatus?
        do {
            let result = try await runner.run(SimpleToolsInvocation.status())
            if result.exitCode == 0, let data = result.stdout.data(using: .utf8) {
                status = try? JSONDecoder().decode(ListenerStatus.self, from: data)
                if status == nil {
                    sources.statusProblem = "The eavesdropper's status could not be read,  so nothing is known about the running eavesdropper."
                }
            } else {
                sources.statusProblem = "The eavesdropper's status command failed:  \(result.failureTail)"
            }
        } catch {
            sources.statusProblem = "The eavesdropper's status could not be run:  \(error.localizedDescription)"
        }
        if let problem = sources.statusProblem { problems.append(problem) }
        sources.status = status

        // The eavesdropper says which partition it reads,  and that copy is the one that can stop it,
        // so it decides over the copy in Muster's own checkout.  If it cannot be read,  no seat is
        // held here.
        if let path = status?.status?.partition_file, !path.isEmpty {
            let url = URL(fileURLWithPath: path)
            sources.partition = ListenerPartition.load(from: url)
            sources.partitionURL = url
        }

        // The seats to show:  the roster,  then every seat the file or the eavesdropper knows.
        var tags = roster
        // The listener folds a section's name to upper case, so `[seat.mm]` is the roster's MM and
        // is not shown again as a seat of its own.
        for tag in sources.config.seatTags where !tags.contains(where: { $0.uppercased() == tag.uppercased() }) {
            tags.append(tag)
        }
        // A retired Mac eavesdropper's last status lists the seats it held before the one-listener
        // cutover, so those are not added as seats of their own unless the status is current.
        let retiredAndStale = sources.partition.givesTheMacNoAgentSeat
            && !WakeSettingsBuilder.statusIsCurrent(sources)
        let statusTags = retiredAndStale ? [] : (status?.seatTags ?? [])
        for tag in statusTags where !tags.contains(where: { $0.uppercased() == tag.uppercased() }) {
            tags.append(tag)
        }
        sources.seats = tags

        // The ledgers.
        var usage: [String: WakeUsage] = [:]
        var recent: [String: [WakeLedger.Outcome]] = [:]
        var usageProblems: [String: String] = [:]
        var tornRows: [String: Int] = [:]
        // The builder's own answer decides which seats have them,  so a seat it shows live data for
        // never shows zero usage for want of a read.
        for tag in tags where !WakeSettingsBuilder.host(of: tag, sources: sources).isElsewhere {
            // The eavesdropper keeps a seat's files under its upper-cased name.
            let directoryName = tag.uppercased()
            let ledgerURL = location.ledgerURL(seat: directoryName)
            switch readLedger(ledgerURL, at: readAt) {
            case .missing:
                // No ledger is a seat that has never woken:  zero is the truth.
                break
            case .unreadable(let why):
                usageProblems[tag] = "The wake ledger at \(ledgerURL.path) could not be read, so this seat's use is not known:  \(why)"
                problems.append("\(tag):  the wake ledger could not be read, so its use is not known.")
            case .parsed(let parsed, let truncated):
                // One read and one parse for both views:  the ledger was not decoded once
                // per question.  The counts are always worked out again for this read's time.
                var counted = WakeLedger.usage(from: parsed, now: readAt)
                // The end of a large ledger that does not reach back a day leaves rows out of the
                // window, so the counts are a floor.
                if truncated, (parsed.oldest.map { readAt.timeIntervalSince($0) < 86_400 } ?? true) {
                    counted.atLeast = true
                }
                if parsed.malformedRecent > 0 {
                    counted.atLeast = true
                    tornRows[tag] = parsed.malformedRecent
                    problems.append("\(tag): \(parsed.malformedRecent) wake ledger row(s) from the last day could not be read,  so its counts are at least what is shown.")
                }
                usage[tag] = counted
                recent[tag] = parsed.outcomes
            }
        }
        sources.usage = usage
        sources.recentWakes = recent
        sources.usageProblems = usageProblems
        sources.tornRows = tornRows
        sources.operatorOptions = OperatorOptions.build(from: sources.config, configProblem: configUnreadable)
        // The kill switch itself, so the pause a seat is under is known when the status is not
        // current or could not be read.
        sources.pauseFile = readPauseFile()
        sources.problems = problems
        return sources
    }

    /// The kill switch, `~/.simple-tools/listener/pause.json`:  a JSON object keyed by scope (a seat
    /// tag, or `*` for every seat).  An entry that is not an object is no pause, as the eavesdropper
    /// reads it.  A file that is there and cannot be read is unreadable, never no pause.
    func readPauseFile() -> WakePauseFile {
        let url = location.pauseURL
        let text: String
        switch readFile(url, limit: Self.maximumFileBytes) {
        case .missing: return .missing
        case .unreadable(let why): return .unreadable("The pause file at \(url.path) could not be read:  \(why)")
        case .text(let read, _): text = read
        }
        guard let data = text.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return .unreadable("The pause file at \(url.path) is not a JSON object.")
        }
        var entries: [String: ListenerStatus.PauseEntry] = [:]
        for (scope, value) in object {
            guard let entry = value as? [String: Any] else { continue }
            entries[scope] = ListenerStatus.PauseEntry(
                wakes_only: entry["wakes_only"] as? Bool,
                by: entry["by"] as? String,
                ts: (entry["ts"] as? NSNumber)?.doubleValue
            )
        }
        return .entries(entries)
    }

    /// What reading a wake ledger found.
    enum LedgerRead {
        case missing
        /// The ledger's rows, and whether only its end was read.
        case parsed(WakeLedger.Parsed, truncated: Bool)
        case unreadable(String)
    }

    /// A seat's wake ledger, parsed.  An open panel reads every minute, and decoding a 4 MB tail
    /// for every seat each time is work for nothing when the ledger has not changed, so a ledger
    /// whose size and modification time match the last parse reuses it.  It is parsed again at
    /// least every `LedgerParseCache.maximumAge`, because which torn rows fall inside the last
    /// day moves with the clock.  The counts are worked out from the rows on every read.
    func readLedger(_ url: URL, at readAt: Date) -> LedgerRead {
        let stamp = LedgerParseCache.stamp(of: url)
        if let stamp, let cached = ledgerCache?.parsed(for: url.path, stamp: stamp, at: readAt) {
            return .parsed(cached.parsed, truncated: cached.truncated)
        }
        switch readFile(url, limit: Self.maximumLedgerTailBytes, tail: true) {
        case .missing:
            ledgerCache?.forget(url.path)
            return .missing
        case .unreadable(let why):
            ledgerCache?.forget(url.path)
            return .unreadable(why)
        case .text(let text, let truncated):
            let parsed = WakeLedger.parse(text, now: readAt)
            if let stamp {
                ledgerCache?.keep(parsed, truncated: truncated, for: url.path, stamp: stamp, at: readAt)
            }
            return .parsed(parsed, truncated: truncated)
        }
    }

    /// A file's text.  A file larger than the limit is refused, or, for a tail read, read from
    /// the end with its first partial line dropped.
    func readFile(_ url: URL, limit: Int, tail: Bool = false) -> FileRead {
        guard FileManager.default.fileExists(atPath: url.path) else { return .missing }
        do {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            let size = try handle.seekToEnd()
            let cap = UInt64(limit)
            guard size > cap else {
                try handle.seek(toOffset: 0)
                let data = try handle.readToEnd() ?? Data()
                return .text(String(decoding: data, as: UTF8.self), truncated: false)
            }
            guard tail else {
                return .unreadable("It is \(size / 1_048_576) MB, over the \(limit / 1_048_576) MB this app reads.")
            }
            // Start one byte early, so a tail that begins exactly at a row keeps that row:  the
            // byte before it is the line feed that ends the row before.
            let start = size - cap - 1
            try handle.seek(toOffset: start)
            var data = try handle.readToEnd() ?? Data()
            if let newline = data.firstIndex(of: 0x0A) {
                data = Data(data[data.index(after: newline)...])
            } else {
                data = Data()
            }
            return .text(String(decoding: data, as: UTF8.self), truncated: true)
        } catch {
            return .unreadable(error.localizedDescription)
        }
    }
}
