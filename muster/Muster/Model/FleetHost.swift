import Foundation

// The hosts Muster administers.
//
// Muster runs on the Mac, but the fleet it administers is two hosts:  the Mac and the
// Hetzner server.  Both are first class here.  A host has an id, a label, a kind and a
// transport, and the transport is how the app reaches that host's own state:  the Mac's
// transport runs the engine here, the server's transport asks the server, over a
// read-only route, for the same engine's answer.
//
// The rule that matters:  a host that cannot be reached reads as UNKNOWN with the
// reason.  It never reads as a healthy host, and it never reads as a host with nothing
// wrong.  A host that answers is judged by the rules that belong to IT:  a rule written
// for another host is not applicable here, not a failure (see `FleetHostVerdict`).
//
// Nothing here holds a credential.  The server's route is an ssh alias whose key may
// run one fixed read-only command and nothing else, and the app never sees the key.

/// The kinds of host the fleet runs on.
///
/// The names match the engine's collector vocabulary (`mac`, `vm`, `hetzner`, `api`),
/// so a rule's collector and a host's kind are the same word for the same thing.
enum FleetHostKind: String, CaseIterable {
    case mac = "mac"
    case hetzner = "hetzner"
    case vm = "vm"
    case api = "api"

    /// The name the app shows.
    var title: String {
        switch self {
        case .mac: return "Mac"
        case .hetzner: return "Hetzner Server"
        case .vm: return "VM"
        case .api: return "API"
        }
    }
}

// MARK: - The host

/// One host the app administers.
struct FleetHost: Identifiable {
    /// The host's stable id, and the word its rules name.
    let id: String
    /// The name the app shows.
    let label: String
    /// The kind of host, which says where its rules run.
    let kind: FleetHostKind
    /// How the app reaches this host.
    let transport: FleetHostTransport

    init(id: String, label: String, kind: FleetHostKind, transport: FleetHostTransport) {
        self.id = id
        self.label = label
        self.kind = kind
        self.transport = transport
    }
}

/// Two hosts are the same host when they say the same thing.  The transport is a means,
/// not an identity:  two transports may reach the same host in different ways.
extension FleetHost: Equatable {
    static func == (lhs: FleetHost, rhs: FleetHost) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.kind == rhs.kind
    }
}

// MARK: - What a host says

/// The answer from one host:  either its own envelope, or why there is none.
enum FleetHostReading: Equatable {
    /// The host answered with an envelope this app reads.
    case envelope(StatusEnvelope)
    /// The host could not be read, and this is why.
    case unreachable(String)

    /// The envelope, when there is one.
    var envelope: StatusEnvelope? {
        if case .envelope(let envelope) = self { return envelope }
        return nil
    }

    /// The reason, when the host could not be read.
    var reason: String? {
        if case .unreachable(let reason) = self { return reason }
        return nil
    }
}

// MARK: - The transport

/// How the app reaches one host's own state.
///
/// The protocol is small on purpose:  one call, one answer, no hidden retries.  A host
/// that is slow is a host that is unreachable, and the app says so rather than waiting.
protocol FleetHostTransport {
    /// The host this transport speaks for.
    var hostID: String { get }

    /// Ask the host for its own status envelope.
    func status() async -> FleetHostReading
}

// MARK: - The Mac

/// The Mac this app runs on:  the engine is here, so the engine is run here.
struct LocalHostTransport: FleetHostTransport {
    let hostID: String
    private let runner: FleetConfigRunning

    init(hostID: String = FleetHostKind.mac.rawValue, runner: FleetConfigRunning) {
        self.hostID = hostID
        self.runner = runner
    }

    func status() async -> FleetHostReading {
        let invocation: FleetConfigInvocation
        do {
            invocation = try FleetConfigInvocation.status()
        } catch {
            return .unreachable("the engine could not be asked:  \(error.localizedDescription)")
        }
        let result: FleetConfigCLIResult
        do {
            result = try await runner.run(invocation)
        } catch {
            return .unreachable("the engine could not be run:  \(error.localizedDescription)")
        }
        if result.timedOut {
            return .unreachable("the engine did not answer in time")
        }
        guard let data = result.stdout.data(using: .utf8) else {
            return .unreachable("the engine's answer was not readable text")
        }
        do {
            return .envelope(try EnvelopeDecoder.decode(data))
        } catch {
            return .unreachable("the engine's answer was not an envelope this app reads:  \(error.localizedDescription)")
        }
    }
}

// MARK: - The server

/// A host reached over ssh, through an alias whose key may run one fixed command.
///
/// The alias is a plain token and is refused when it could be read as anything else, so
/// the app can never turn a host name into a flag.  The route is read-only by
/// construction:  the key on the far side has a fixed command, so asking for something
/// else returns the same thing.
struct SSHHostTransport: FleetHostTransport {
    let hostID: String
    /// The ssh alias, for example `muster-hetzner`.
    let alias: String
    private let process: HostProcessRunning
    private let timeout: TimeInterval

    init(
        hostID: String,
        alias: String,
        process: HostProcessRunning = HostProcess(),
        timeout: TimeInterval = FleetHostTransportDefaults.timeout
    ) {
        self.hostID = hostID
        self.alias = alias
        self.process = process
        self.timeout = timeout
    }

    /// The argv, as an array and never a shell string.
    static func arguments(alias: String) -> [String] {
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=yes",
            alias,
        ]
    }

    func status() async -> FleetHostReading {
        guard FleetConfigInvocation.isSafeToken(alias) else {
            return .unreachable("the host's ssh alias is not a plain name")
        }
        let result: FleetConfigCLIResult
        do {
            result = try await process.run(Self.arguments(alias: alias), timeout: timeout)
        } catch {
            return .unreachable("the host could not be reached:  \(error.localizedDescription)")
        }
        if result.timedOut {
            return .unreachable("the host did not answer in time")
        }
        // The engine exits 1 when the verdict is FAIL, and that is still an answer.
        guard result.exitCode == 0 || result.exitCode == 1 else {
            // ssh's own text can carry host names and addresses, so it is never copied.
            return .unreachable("the host's answer was refused (exit \(result.exitCode))")
        }
        guard let data = result.stdout.data(using: .utf8) else {
            return .unreachable("the host's answer was not readable text")
        }
        do {
            return .envelope(try EnvelopeDecoder.decode(data))
        } catch {
            return .unreachable("the host's answer was not an envelope this app reads:  \(error.localizedDescription)")
        }
    }
}

/// The defaults the transports share.
enum FleetHostTransportDefaults {
    /// How long a host has to answer.  The server's engine run takes about eight
    /// seconds today, so the ceiling leaves room without waiting forever.
    static let timeout: TimeInterval = 120
    /// The largest answer the app reads, so a runaway host cannot fill memory.
    static let maximumOutputBytes = 8 * 1024 * 1024
}

// MARK: - The verdict

/// What the app can say about one host.
///
/// The rule that keeps the fleet honest:  a row that belongs to another host is not
/// applicable here, so it never counts as a failure.  The registry's rules are Mac
/// rules today, so a run on the server reports the Mac's paths as MISSING, and those
/// rows say nothing at all about the server.  A host whose own rules are all green
/// reads as passing;  a host with no rules of its own reads as unproven, because
/// nothing was proved and nothing failed.
struct FleetHostVerdict: Identifiable, Equatable {
    let host: FleetHost
    let reading: FleetHostReading

    var id: String { host.id }

    /// The rows that belong to this host.
    var ownResults: [CheckResult] {
        (reading.envelope?.results ?? []).filter { $0.collector == host.kind.rawValue }
    }

    /// The rows that belong to another host.  They are not this host's business.
    var foreignResults: [CheckResult] {
        (reading.envelope?.results ?? []).filter { $0.collector != host.kind.rawValue }
    }

    /// The rows that failed, by the engine's own judgement.
    var failingResults: [CheckResult] {
        ownResults.filter { $0.failing }
    }

    /// The rows that drifted, that are not failures.
    var driftedResults: [CheckResult] {
        ownResults.filter { !$0.failing && $0.checkState == .drift }
    }

    /// The rows the engine could not tell.
    var unknownResults: [CheckResult] {
        ownResults.filter { !$0.failing && $0.checkState == .unknown }
    }

    /// Why the host could not be read, when it could not be.
    var reason: String? {
        if let reason = reading.reason { return reason }
        if ownResults.isEmpty {
            return "this host has no rules of its own yet, so nothing was proved and nothing failed"
        }
        return nil
    }

    /// The state the host reads as, in the app's own vocabulary.
    var state: CellState {
        if reading.envelope == nil { return .unproven }
        if ownResults.isEmpty { return .unproven }
        if !failingResults.isEmpty { return .missing }
        if !driftedResults.isEmpty { return .drift }
        if !unknownResults.isEmpty { return .unproven }
        return .passed
    }

    /// The host's own summary, when it answered.
    var summary: EnvelopeSummary? { reading.envelope?.summary }

    /// When the host's answer was written.
    var generatedAt: String? { reading.envelope?.generatedAt }
}

// MARK: - The hosts

/// The hosts this app administers.
///
/// The Mac is always here.  The server is here when a read-only route is configured for
/// it, and it is never faked:  no route means no server host, not a server host that
/// reads as healthy.
enum FleetHostSet {
    /// The alias the server's route uses, and the key it is allowed to run.
    static let serverAlias = "muster-hetzner"
    static let serverKeyPath = "~/.ssh/muster_ro"

    /// The hosts, in the order the app shows them.
    static func hosts(
        localRunner: FleetConfigRunning,
        process: HostProcessRunning = HostProcess()
    ) -> [FleetHost] {
        var hosts: [FleetHost] = [
            FleetHost(
                id: FleetHostKind.mac.rawValue,
                label: "This Mac",
                kind: .mac,
                transport: LocalHostTransport(runner: localRunner)
            ),
        ]
        if FileManager.default.fileExists(atPath: (NSString(string: serverKeyPath).expandingTildeInPath)) {
            hosts.append(
                FleetHost(
                    id: FleetHostKind.hetzner.rawValue,
                    label: FleetHostKind.hetzner.title,
                    kind: .hetzner,
                    transport: SSHHostTransport(
                        hostID: FleetHostKind.hetzner.rawValue,
                        alias: serverAlias,
                        process: process
                    )
                )
            )
        }
        return hosts
    }
}

// MARK: - Running a process

/// The seam the transports run through, so a test can answer without a network.
protocol HostProcessRunning {
    func run(_ arguments: [String], timeout: TimeInterval) async throws -> FleetConfigCLIResult
}

/// The real process runner.
struct HostProcess: HostProcessRunning {
    func run(_ arguments: [String], timeout: TimeInterval) async throws -> FleetConfigCLIResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        process.arguments = arguments
        let out = Pipe()
        let err = Pipe()
        process.standardOutput = out
        process.standardError = err
        try process.run()
        // The pipes are read before the wait, so a large answer cannot fill the pipe
        // buffer and deadlock the child.
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < deadline {
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        if process.isRunning {
            process.terminate()
            return FleetConfigCLIResult(exitCode: -1, stdout: "", stderr: "", timedOut: true)
        }
        let stdout = String(data: outData, encoding: .utf8) ?? ""
        let stderr = String(data: errData, encoding: .utf8) ?? ""
        return FleetConfigCLIResult(
            exitCode: process.terminationStatus,
            stdout: stdout,
            stderr: stderr,
            timedOut: false
        )
    }
}
