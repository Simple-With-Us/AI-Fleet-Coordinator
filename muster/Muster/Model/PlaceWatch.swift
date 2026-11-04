import AppKit
import Combine
import Foundation

/// What the menu knows about the server,  kept fresh away from the menu.
///
/// It reads the two local files the Mac already holds,  off the main thread,  every minute
/// and after an action runs.  Opening the menu reads memory:  it never touches a file and
/// never contacts the server.
///
/// It also holds Ask The Server Now,  the one thing here that does reach the server.  That
/// runs only on a click,  one at a time,  through the same read-only ssh route the resource
/// policy uses.  Muster holds no credential for it,  and no Touch ID is needed,  because a
/// plan cannot change anything.  An answer only proves the route answers:  it never turns the
/// bar to OK,  because only a current report does that.
@MainActor
final class PlaceWatch: ObservableObject {
    /// How often the two files are read.
    nonisolated static let readInterval: TimeInterval = 60

    /// The server's own report,  or nil before the first read.
    @Published private(set) var status: ServerStatusReading?
    /// The doctor's report,  or nil before the first read.
    @Published private(set) var tools: FleetToolsReading?
    /// The time of the last tick,  which is what a window is measured against.
    @Published private(set) var now: Date
    /// What came of the last Ask The Server Now.
    @Published private(set) var asked: ServerAskResult?
    /// True while an ask is in flight.
    @Published private(set) var isAsking = false

    private let readStatus: @Sendable () -> ServerStatusReading
    private let readTools: @Sendable () -> FleetToolsReading
    private let runner: OpsRunning
    private let makeBuilder: () -> OpsCommandBuilder
    private let clock: () -> Date
    private let sequencer = FleetSnapshotSequencer()
    private var loop: Task<Void, Never>?
    private var cancellables: Set<AnyCancellable> = []

    /// - Parameters:
    ///   - interval:  How often to read,  or nil for a test that reads by hand.
    ///   - controller:  The app's one controller unless a test gives its own.
    ///   - onWake:  Called when the Mac wakes,  or nil for a test,  which then does not watch.
    init(readStatus: @escaping @Sendable () -> ServerStatusReading = { ServerStatusReportReader.readDefault() },
         readTools: @escaping @Sendable () -> FleetToolsReading = { FleetToolsReportReader.readDefault() },
         runner: OpsRunning = OpsProcessRunner(),
         makeBuilder: @escaping () -> OpsCommandBuilder = {
             OpsCommandBuilder(repoRoot: FleetConfigLocation.resolve().repoRoot)
         },
         interval: TimeInterval? = PlaceWatch.readInterval,
         controller: OpsController? = nil,
         clock: @escaping () -> Date = { Date() },
         onWake: (() -> Void)? = nil) {
        self.readStatus = readStatus
        self.readTools = readTools
        self.runner = runner
        self.makeBuilder = makeBuilder
        self.clock = clock
        self.now = clock()
        (controller ?? OpsController.shared).$completedRuns
            .dropFirst()
            .sink { [weak self] _ in self?.readNow() }
            .store(in: &cancellables)
        if let interval {
            let nanoseconds = UInt64(max(interval, 0.01) * 1_000_000_000)
            loop = Task { [weak self] in
                while !Task.isCancelled {
                    guard self != nil else { return }
                    self?.readNow()
                    try? await Task.sleep(nanoseconds: nanoseconds)
                }
            }
        }
        if let onWake {
            // A timer does not run while the Mac sleeps,  so every window has passed by the
            // time it wakes.
            _ = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in
                    self?.readNow()
                    onWake()
                }
            }
        }
    }

    deinit {
        loop?.cancel()
    }

    /// Reads the two files again,  off the main thread.  Only the newest read may write.
    func readNow() {
        let generation = sequencer.begin()
        let readStatus = self.readStatus
        let readTools = self.readTools
        Task { [weak self] in
            let read = await Task.detached(priority: .utility) { (readStatus(), readTools()) }.value
            guard let self, self.sequencer.isCurrent(generation) else { return }
            self.status = read.0
            self.tools = read.1
            self.now = self.clock()
        }
    }

    /// Asks the server whether it answers.
    func askServer() {
        guard !isAsking else { return }
        isAsking = true
        let runner = self.runner
        let builder = makeBuilder()
        Task { [weak self] in
            let answered = await Self.asks(runner: runner, builder: builder)
            guard let self else { return }
            self.asked = ServerAskResult(answered: answered, at: self.clock())
            self.isAsking = false
        }
    }

    /// Any JSON answer means the server answered,  a refusal included.  An ssh that got no
    /// answer means it did not.
    private nonisolated static func asks(runner: OpsRunning, builder: OpsCommandBuilder) async -> Bool {
        do {
            let invocation = try builder.invocation(for: .resourceStatus, mode: .plan)
            let process = try await runner.run(invocation)
            guard !process.timedOut else { return false }
            return OpsResultParser.decode(process.stdout) != nil
        } catch {
            return false
        }
    }
}

extension MachineMenuInputs {
    /// What the menu reads,  taken from the app's four sources at this moment.
    @MainActor
    init(admin: FleetAdmin, monitor: DriftMonitor, watch: PlaceWatch, router: ServiceActionRouter) {
        self.init(
            services: admin.services,
            lastReadAt: admin.lastReadAt,
            redCells: monitor.redCount == nil ? nil : monitor.redCells,
            driftCells: monitor.driftCells,
            lastRunWasTrusted: monitor.lastRunWasTrusted,
            transitions: monitor.transitions,
            status: watch.status,
            tools: watch.tools,
            asked: watch.asked,
            isAsking: watch.isAsking,
            allowlist: router.allowlist,
            actionState: router.actionState,
            now: watch.now
        )
    }
}
