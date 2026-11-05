import Foundation

// Reading the wake settings:  the configuration file,  the eavesdropper's own status,  the pins
// that prove a binary works,  and the ledger that says how much of the budget is used.
//
// Nothing here decides anything.  It reads,  and hands what it read to the builder.

// MARK: - Where the state lives

/// Where the eavesdropper keeps its state on this Mac.
struct ListenerStateLocation: Equatable {
    /// The eavesdropper's state directory:  `~/.agent-sync`.
    var root: URL

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.root = home.appendingPathComponent(".agent-sync", isDirectory: true)
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

    /// The pin file for a runtime.
    func pinURL(seat: String, runtime: String) -> URL {
        let name = runtime == "claude" ? "wake-pin.json" : "wake-pin-\(runtime).json"
        return seatDirectory(seat).appendingPathComponent(name)
    }

    /// The candidate a passing `test-wake --run` leaves for a runtime, waiting to be pinned.
    func candidateURL(seat: String, runtime: String) -> URL {
        let name = runtime == "claude" ? "wake-pin-candidate.json" : "wake-pin-candidate-\(runtime).json"
        return seatDirectory(seat).appendingPathComponent(name)
    }

    /// The seat's wake ledger.
    func ledgerURL(seat: String) -> URL {
        seatDirectory(seat).appendingPathComponent("wakes.jsonl")
    }
}

// MARK: - Reading

/// Reads everything the wake settings panel shows.
struct WakeSettingsReader {
    /// Where the state lives.
    var location: ListenerStateLocation
    /// The command line,  for the eavesdropper's own account of itself.
    var runner: AgentSyncRunning
    /// The seats to show.
    var roster: [String]
    /// Where the seat partition is,  or nil when the checkout could not be found.
    var partitionURL: URL?
    /// The clock.
    var now: () -> Date

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: AgentSyncRunning = AgentSyncCLI(),
        roster: [String] = [],
        partitionURL: URL? = ListenerPartition.defaultURL(),
        now: @escaping () -> Date = { Date() }
    ) {
        self.location = location
        self.runner = runner
        self.roster = roster
        self.partitionURL = partitionURL
        self.now = now
    }

    /// The maximum size of the configuration and pin files this reads.
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
            problems.append(problem)
        }

        // The eavesdropper's own account of itself.
        var status: ListenerStatus?
        do {
            let result = try await runner.run(AgentSyncInvocation.status())
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
        for tag in sources.config.seatTags where !tags.contains(tag) {
            tags.append(tag)
        }
        for tag in status?.seatTags ?? [] where !tags.contains(tag) {
            tags.append(tag)
        }
        sources.seats = tags

        // The pins,  the pin candidates,  and the ledgers.
        var pins: [String: [String: PinEvidence]] = [:]
        var unreadablePins: [String: [String: String]] = [:]
        var candidates: [String: [String: Date]] = [:]
        var usage: [String: WakeUsage] = [:]
        var recent: [String: [WakeLedger.Outcome]] = [:]
        var usageProblems: [String: String] = [:]
        var tornRows: [String: Int] = [:]
        // The builder's own answer decides which seats have them,  so a seat it shows live data for
        // never shows zero usage or no pins for want of a read.
        for tag in tags where !WakeSettingsBuilder.host(of: tag, sources: sources).isElsewhere {
            // The eavesdropper keeps a seat's files under its upper-cased name.
            let directoryName = tag.uppercased()
            var seatPins: [String: PinEvidence] = [:]
            var seatUnreadable: [String: String] = [:]
            var seatCandidates: [String: Date] = [:]
            for runtime in WakeType.pinKeys {
                switch readPin(seat: directoryName, runtime: runtime) {
                case .found(let evidence): seatPins[runtime] = evidence
                case .unreadable(let why): seatUnreadable[runtime] = why
                case .missing: break
                }
                if let at = readCandidate(seat: directoryName, runtime: runtime) {
                    seatCandidates[runtime] = at
                }
            }
            pins[tag] = seatPins
            if !seatUnreadable.isEmpty { unreadablePins[tag] = seatUnreadable }
            if !seatCandidates.isEmpty { candidates[tag] = seatCandidates }

            let ledgerURL = location.ledgerURL(seat: directoryName)
            switch readFile(ledgerURL, limit: Self.maximumLedgerTailBytes, tail: true) {
            case .missing:
                // No ledger is a seat that has never woken:  zero is the truth.
                break
            case .unreadable(let why):
                usageProblems[tag] = "The wake ledger at \(ledgerURL.path) could not be read, so this seat's use is not known:  \(why)"
                problems.append("\(tag):  the wake ledger could not be read, so its use is not known.")
            case .text(let text, let truncated):
                // One read and one parse for both views:  the ledger was not decoded once
                // per question.
                let parsed = WakeLedger.parse(text, now: readAt)
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
        sources.pins = pins
        sources.unreadablePins = unreadablePins
        sources.candidates = candidates
        sources.usage = usage
        sources.recentWakes = recent
        sources.usageProblems = usageProblems
        sources.tornRows = tornRows
        sources.operatorOptions = OperatorOptions.build(from: sources.config)
        sources.problems = problems
        return sources
    }

    /// What reading a pin file found.
    enum PinRead: Equatable {
        case missing
        case found(PinEvidence)
        case unreadable(String)
    }

    /// The pin that proves a seat's binary works.  A Claude, muse or mcode pin names a binary;  an
    /// OpenRouter pin names a model and a request, and no binary.
    private func readPin(seat: String, runtime: String) -> PinRead {
        let url = location.pinURL(seat: seat, runtime: runtime)
        let text: String
        switch readFile(url, limit: Self.maximumFileBytes) {
        case .missing: return .missing
        case .unreadable(let why): return .unreadable(why)
        case .text(let read, _): text = read
        }
        struct PinFile: Decodable {
            var at: Double?
            var model: String?
            var claude: String?
            var muse: String?
            var mcode: String?
        }
        guard let data = text.data(using: .utf8),
              let file = try? JSONDecoder().decode(PinFile.self, from: data) else {
            return .unreadable("The pin file at \(url.path) is not a pin this app can read.")
        }
        let binary = file.claude ?? file.muse ?? file.mcode
        let isOpenRouter = runtime.hasPrefix("openrouter")
        guard binary != nil || (isOpenRouter && file.model != nil) else {
            return .unreadable("The pin file at \(url.path) names no binary or model.")
        }
        return .found(PinEvidence(
            runtime: runtime,
            binary: binary,
            model: file.model,
            verifiedAt: file.at.map { Date(timeIntervalSince1970: $0) },
            pinned: true,
            why: nil
        ))
    }

    /// When a passing test wake of a runtime left its candidate, or nil when there is none.
    private func readCandidate(seat: String, runtime: String) -> Date? {
        let url = location.candidateURL(seat: seat, runtime: runtime)
        guard case .text(let text, _) = readFile(url, limit: Self.maximumFileBytes),
              let data = text.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let at = object["at"] as? Double else { return nil }
        return Date(timeIntervalSince1970: at)
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
