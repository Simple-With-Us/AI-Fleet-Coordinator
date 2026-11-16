import Foundation

/// Keeps the panel snapshots inert (AFC-707):  no panel can start a process or reach the network.
///
/// `MusterPanelTests` compiles the app's own sources into this bundle, so this module-level
/// `Process` shadows `Foundation.Process` for every `Process()` in them, in this bundle only.
/// The `Muster` app target never compiles this file and keeps the real `Process`.  It cannot
/// subclass `Foundation.Process`, which is a class cluster, so it stands in with the part of
/// the API the app uses.  `run()` never starts anything:  it records the executable and throws,
/// so every reader takes its own launch-failure path and its panel shows that state.
///
/// That covers each live reader a panel starts from `.task`:  `fleet-secrets names` and
/// `doctor` (Secrets), the recall token read that comes before the recall request (Recall
/// Settings), `ssh` for `eavesdropper-status --plan` (`ServerEavesdropperStatusReader`), the
/// Seats panel's sources (`SeatActivityReader`), the listener, engine, identity, config and
/// Fleet View readers, and `StartupPolicy`'s commands.
final class Process: @unchecked Sendable {
    enum TerminationReason: Int { case exit = 1, uncaughtSignal = 2 }

    struct Blocked: LocalizedError {
        let executable: String
        var errorDescription: String? { "Panel snapshot:  \(executable) was not started." }
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var attempts: [String] = []

    /// Every executable a panel tried to start since the last call, in order.
    static func takeAttempts() -> [String] {
        lock.lock(); defer { lock.unlock() }
        let taken = attempts
        attempts = []
        return taken
    }

    var executableURL: URL?
    var arguments: [String]?
    var environment: [String: String]?
    var currentDirectoryURL: URL?
    var standardInput: Any?
    var standardOutput: Any?
    var standardError: Any?
    var terminationHandler: (@Sendable (Process) -> Void)?

    private(set) var isRunning = false
    let processIdentifier: Int32 = 0
    let terminationStatus: Int32 = 127
    let terminationReason: TerminationReason = .exit

    func run() throws {
        let executable = executableURL?.path ?? "(none)"
        Self.lock.lock()
        Self.attempts.append(executable)
        Self.lock.unlock()
        throw Blocked(executable: executable)
    }

    func terminate() {}
    func waitUntilExit() {}
}

/// Fails every request made through a session that consults the registered protocols,
/// `URLSession.shared` among them (`ControlAPI`, `McpServerControl`), before it leaves the Mac.
final class InertURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
    }
    override func stopLoading() {}
}
