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

    /// The maximum size of the files this reads.
    static let maximumFileBytes = 32 * 1024 * 1024

    /// Reads everything,  and hands it to the builder.
    func read() async -> WakeSettingsSources {
        var sources = WakeSettingsSources()
        var problems: [String] = []

        // Who holds each seat.  The eavesdropper refuses a section it is not given,  so this comes
        // first:  every seat's row depends on it.
        sources.partition = ListenerPartition.load(from: partitionURL)
        sources.partitionURL = partitionURL

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
        sources.configReadable = true
        sources.configPath = location.configURL.path

        // The eavesdropper's own account of itself.
        var status: ListenerStatus?
        do {
            let result = try await runner.run(AgentSyncInvocation.status())
            if result.exitCode == 0, let data = result.stdout.data(using: .utf8) {
                status = try? JSONDecoder().decode(ListenerStatus.self, from: data)
                if status == nil {
                    problems.append("The eavesdropper's status could not be read,  so nothing is known about the running eavesdropper.")
                }
            } else {
                problems.append("The eavesdropper's status command failed:  \(result.stderrTail)")
            }
        } catch {
            problems.append("The eavesdropper's status could not be run:  \(error.localizedDescription)")
        }
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

        // The pins,  and the ledgers.
        var pins: [String: [String: PinEvidence]] = [:]
        var usage: [String: WakeUsage] = [:]
        var recent: [String: [WakeLedger.Outcome]] = [:]
        // The builder's own answer decides which seats have them,  so a seat it shows live data for
        // never shows zero usage or no pins for want of a read.
        for tag in tags where !WakeSettingsBuilder.host(of: tag, sources: sources).isElsewhere {
            // The eavesdropper keeps a seat's files under its upper-cased name.
            let directoryName = tag.uppercased()
            var seatPins: [String: PinEvidence] = [:]
            for runtime in ["claude", "muse", "mcode"] {
                if let evidence = readPin(seat: directoryName, runtime: runtime) {
                    seatPins[runtime] = evidence
                }
            }
            pins[tag] = seatPins
            if let ledger = readText(location.ledgerURL(seat: directoryName)) {
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
