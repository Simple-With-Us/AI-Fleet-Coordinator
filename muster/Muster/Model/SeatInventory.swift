import Foundation

/// What is installed for one platform on one host, beyond what the engine checks:  its MCP
/// servers, hooks, plugins, skills, sub-agents and commands, as names and counts.
///
/// The answer comes from `scripts/fleet_seat_inventory.py`, one implementation for both hosts.
/// The Mac runs it directly (`MacSeatInventoryReader`).  The names and counts are all it
/// returns:  no value from a config file is ever in the answer, and a file that is absent,
/// too large or unreadable says so in the section's note and never reads as empty.
struct SeatInventory: Decodable, Equatable {
    struct Item: Decodable, Equatable, Identifiable {
        let name: String
        let detail: String?
        var id: String { name }
    }

    struct Section: Decodable, Equatable, Identifiable {
        let title: String
        /// Where the names came from, with the home folder shortened to `~`.
        let source: String
        let items: [Item]
        /// Why the section is empty or cut, in words, when it is.
        let note: String?
        var id: String { "\(title)|\(source)" }
    }

    let schemaVersion: Int
    let tool: String
    let ok: Bool
    /// `mac` or `server`.
    let host: String
    let platform: String
    let generatedAt: String
    let sections: [Section]
    let detail: String
}

/// One reading:  the inventory, or why there is none.
enum SeatInventoryReading: Equatable {
    case loaded(SeatInventory)
    case unavailable(String)
}

enum SeatInventoryDecoder {
    static let tool = "fleet-seat-inventory"
    static let schemaVersion = 1

    /// The inventory in an answer, or the reason it cannot be trusted.
    ///
    /// An answer from another tool, another schema version or one that says it is not ok is
    /// refused, so a format this app does not know is never drawn as if it were understood.
    static func decode(_ data: Data) -> SeatInventoryReading {
        guard let inventory = try? JSONDecoder().decode(SeatInventory.self, from: data) else {
            return .unavailable("The answer could not be read as a seat inventory.")
        }
        return validated(inventory)
    }

    /// The inventory when it is this tool's, this schema's and ok;  the reason it cannot be
    /// trusted otherwise.
    static func validated(_ inventory: SeatInventory) -> SeatInventoryReading {
        guard inventory.tool == tool else {
            return .unavailable("The answer is from \(inventory.tool), not the seat inventory.")
        }
        guard inventory.schemaVersion == schemaVersion else {
            return .unavailable("The answer is schema version \(inventory.schemaVersion), which this app does not read.")
        }
        guard inventory.ok else {
            return .unavailable(inventory.detail.isEmpty ? "The reader could not answer for this platform." : inventory.detail)
        }
        return .loaded(inventory)
    }

    /// The helper's own envelope around the server's inventory:  `ok`, `result`, `detail`, and
    /// the `inventory` document when it answered.
    private struct ServerEnvelope: Decodable {
        let ok: Bool
        let result: String
        let detail: String?
        let inventory: SeatInventory?
    }

    /// The server's answer to `server seat-inventory PLATFORM --plan --json`.
    ///
    /// A refusal gives the helper's own sentence.  An answer for another host or another
    /// platform than the one asked about is never drawn, so the server's panel can never show
    /// the Mac's files or another platform's.
    static func decodeServerAnswer(_ text: String, platform: String, exitCode: Int32) -> SeatInventoryReading {
        guard let envelope = try? JSONDecoder().decode(ServerEnvelope.self, from: Data(text.utf8)) else {
            if exitCode != 0 {
                return .unavailable("The server could not be reached over the fleet-ops-ctl route (exit \(exitCode)).")
            }
            return .unavailable("The server's answer could not be read.")
        }
        guard envelope.ok else {
            let said = envelope.detail ?? ""
            return .unavailable(said.isEmpty ? "The server refused the request." : said)
        }
        guard let inventory = envelope.inventory else {
            return .unavailable("The server answered without an inventory.")
        }
        guard inventory.host == "server" else {
            return .unavailable("The answer is for the \(inventory.host), not the server.")
        }
        guard inventory.platform == platform else {
            return .unavailable("The answer is for \(inventory.platform), not \(platform).")
        }
        return validated(inventory)
    }

    /// A platform id as the engine spells it:  lower case, digits and dashes.
    static func isPlatformId(_ text: String) -> Bool {
        text.range(of: "^[a-z][a-z0-9-]{0,24}$", options: .regularExpression) != nil
    }
}

/// Reads one platform's inventory on this Mac, by running the checkout's own script.
struct MacSeatInventoryReader {
    var repoRoot: URL
    var home: URL = FileManager.default.homeDirectoryForCurrentUser
    var runner: OpsRunning = OpsProcessRunner()
    static let timeout: TimeInterval = 20
    static let script = "scripts/fleet_seat_inventory.py"

    func invocation(platform: String, fileManager: FileManager = .default) throws -> OpsInvocation {
        guard SeatInventoryDecoder.isPlatformId(platform) else {
            throw OpsControlError.invalidArgument("The platform name is not one this reader accepts.")
        }
        switch TrustedCheckout.lookup(repoRoot: repoRoot, script: Self.script, fileManager: fileManager) {
        case .missing(let path):
            throw OpsControlError.helperNotFound(path)
        case .unsafe(let path):
            throw OpsControlError.launchFailed(
                "The seat inventory reader is missing or unsafe at \(path).\(ServerStatusCopy.sentenceGap)Choose a repository containing \(Self.script) in Settings."
            )
        case .found(let root, let script):
            return OpsInvocation(
                executable: OpsControlRoute.interpreter,
                arguments: ["-I", script.path, "--platform", platform, "--home", home.path, "--json"],
                workingDirectory: root.path,
                environment: ["HOME": home.path, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"],
                timeout: Self.timeout
            )
        }
    }

    func read(platform: String) async -> SeatInventoryReading {
        do {
            let result = try await runner.run(invocation(platform: platform))
            if result.timedOut {
                return .unavailable("The seat inventory reader timed out.")
            }
            guard result.exitCode == 0 else {
                // A platform the reader has no sources for exits 2 with its own sentence.
                if let said = SeatInventoryDecoder.refusal(in: result.stdout) { return .unavailable(said) }
                return .unavailable("The seat inventory reader failed (exit \(result.exitCode)).")
            }
            return SeatInventoryDecoder.decode(Data(result.stdout.utf8))
        } catch {
            return .unavailable(error.localizedDescription)
        }
    }
}

/// Reads one platform's inventory on the server, over the existing restricted, read-only
/// route (`fleet-ops-ctl`, verb `server seat-inventory`).  It changes nothing, so it needs no
/// confirmation and no Touch ID.
struct ServerSeatInventoryReader {
    var runner: OpsRunning = OpsProcessRunner()
    var makeBuilder: () -> OpsCommandBuilder = {
        OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
    }

    func read(platform: String) async -> SeatInventoryReading {
        do {
            let invocation = try makeBuilder().seatInventoryInvocation(platform: platform)
            let process = try await runner.run(invocation)
            if process.timedOut {
                return .unavailable("The server did not answer in time.")
            }
            return SeatInventoryDecoder.decodeServerAnswer(process.stdout, platform: platform, exitCode: process.exitCode)
        } catch {
            return .unavailable(error.localizedDescription)
        }
    }
}

extension SeatInventoryDecoder {
    /// The sentence the reader gave when it refused, from its `detail` field.
    static func refusal(in output: String) -> String? {
        struct Refusal: Decodable { let detail: String? }
        guard let refusal = try? JSONDecoder().decode(Refusal.self, from: Data(output.utf8)),
              let detail = refusal.detail, !detail.isEmpty else { return nil }
        return detail
    }
}

extension SeatInventory.Section {
    /// The one line a section's folded panel shows under its title.
    var summary: String {
        if items.isEmpty { return note ?? "Nothing listed." }
        let count = items.count == 1 ? "1 listed" : "\(items.count) listed"
        guard let note, !note.isEmpty else { return count }
        return "\(count).\(ServerStatusCopy.sentenceGap)\(note)"
    }
}
