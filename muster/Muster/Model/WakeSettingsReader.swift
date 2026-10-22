import Foundation

// Reading the wake settings:  the configuration file,  the listener's own status,  the pins
// that prove a binary works,  and the ledger that says how much of the budget is used.
//
// Nothing here decides anything.  It reads,  and hands what it read to the builder.

// MARK: - Where the state lives

/// Where the listener keeps its state on this Mac.
struct ListenerStateLocation: Equatable {
    /// The listener's state directory:  `~/.agent-sync`.
    var root: URL

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser) {
        self.root = home.appendingPathComponent(".agent-sync", isDirectory: true)
    }

    /// The configuration file.
    var configURL: URL {
        root.appendingPathComponent("listener.toml")
    }

    /// The listener's own published state.
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
    /// The command line,  for the listener's own account of itself.
    var runner: AgentSyncRunning
    /// The seats to show.
    var roster: [String]
    /// The clock.
    var now: () -> Date

    init(
        location: ListenerStateLocation = ListenerStateLocation(),
        runner: AgentSyncRunning = AgentSyncCLI(),
        roster: [String] = [],
        now: @escaping () -> Date = { Date() }
    ) {
        self.location = location
        self.runner = runner
        self.roster = roster
        self.now = now
    }

    /// The maximum size of the files this reads.
    static let maximumFileBytes = 32 * 1024 * 1024

    /// Reads everything,  and hands it to the builder.
    func read() async -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        var problems: [String] = []

        // The configuration file.
        let configText = readText(location.configURL)
        guard let configText else {
            sources.config = ListenerConfigDocument(text: "")
            sources.problems = ["The configuration file at \(location.configURL.path) could not be read."]
            sources.configPath = location.configURL.path
            sources.seats = roster
            return sources
        }
        sources.config = ListenerConfigDocument(text: configText)
        sources.configPath = location.configURL.path

        // The listener's own account of itself.
        var status: ListenerStatus?
        do {
            let result = try await runner.run(AgentSyncInvocation.status())
            if result.exitCode == 0, let data = result.stdout.data(using: .utf8) {
                status = try? JSONDecoder().decode(ListenerStatus.self, from: data)
                if status == nil {
                    problems.append("The listener's status could not be read,  so nothing is known about the running listener.")
                }
            } else {
                problems.append("The listener's status command failed:  \(result.stderrTail)")
            }
        } catch {
            problems.append("The listener's status could not be run:  \(error.localizedDescription)")
        }
        sources.status = status

        // The seats to show:  the roster,  then every seat the file or the listener knows.
        var tags = roster
        for section in sources.config.sections where section.name.hasPrefix("seat.") {
            let tag = String(section.name.dropFirst("seat.".count))
            if !tag.isEmpty, !tags.contains(tag) { tags.append(tag) }
        }
        for tag in status?.seatTags ?? [] where !tags.contains(tag) {
            tags.append(tag)
        }
        sources.seats = tags

        // The pins,  and the ledgers.
        var pins: [String: [String: PinEvidence]] = [:]
        var usage: [String: WakeUsage] = [:]
        var recent: [String: [WakeLedger.Outcome]] = [:]
        for tag in tags {
            var seatPins: [String: PinEvidence] = [:]
            for runtime in ["claude", "muse", "mcode"] {
                if let evidence = readPin(seat: tag, runtime: runtime) {
                    seatPins[runtime] = evidence
                }
            }
            pins[tag] = seatPins
            if let ledger = readText(location.ledgerURL(seat: tag)) {
                // One read and one parse for both views:  the ledger was not decoded once
                // per question.
                let parsed = WakeLedger.parse(ledger)
                usage[tag] = WakeLedger.usage(from: parsed, now: now())
                recent[tag] = parsed.outcomes
                if parsed.malformed > 0 {
                    problems.append("\(tag): \(parsed.malformed) wake ledger row(s) could not be read,  so its counters may be low.")
                }
            }
        }
        sources.pins = pins
        sources.usage = usage
        sources.recentWakes = recent
        sources.operatorOptions = OperatorOptions.build(from: sources.config)
        sources.problems = problems
        return sources
    }

    /// The pin that proves a seat's binary works.
    private func readPin(seat: String, runtime: String) -> PinEvidence? {
        guard let text = readText(location.pinURL(seat: seat, runtime: runtime)) else { return nil }
        struct PinFile: Decodable {
            var at: Double?
            var model: String?
            var claude: String?
            var muse: String?
            var mcode: String?
        }
        guard let data = text.data(using: .utf8),
              let file = try? JSONDecoder().decode(PinFile.self, from: data) else { return nil }
        let binary = file.claude ?? file.muse ?? file.mcode
        guard binary != nil else { return nil }
        return PinEvidence(
            runtime: runtime,
            binary: binary,
            model: file.model,
            verifiedAt: file.at.map { Date(timeIntervalSince1970: $0) },
            pinned: true,
            why: nil
        )
    }

    /// A file's text,  or nil when it cannot be read.
    private func readText(_ url: URL) -> String? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard data.count <= Self.maximumFileBytes else { return nil }
        return String(decoding: data, as: UTF8.self)
    }
}
