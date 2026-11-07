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
    init(readStatus: @escaping @Sendable () -> ServerStatusReading = { PlaceWatchReports.readStatus() },
         readTools: @escaping @Sendable () -> FleetToolsReading = { PlaceWatchReports.readTools() },
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

// MARK: - The two report files,  read again only when they changed

/// A report file's last reading,  kept until the file changes.
///
/// The menu reads its two report files every minute for as long as the app runs.  Most
/// minutes neither file has changed,  so the reading is kept with the file's size and
/// modification time,  and the file is read and decoded again only when one of those moves.
/// A kept reading is brought up to the minute by `reuse`,  which may also turn it down:  a
/// reading whose verdict depends on the time (a report that has just gone out of its window)
/// is read again,  so a kept reading never stands in for a different answer.
///
/// A file over the size cap is refused and never read whole,  the way the wake settings reader
/// refuses one.  The reads run off the main thread,  so the cache holds a lock.
final class ReportStampCache<Reading>: @unchecked Sendable {
    /// What identifies one version of a file.
    struct Stamp: Equatable {
        let size: UInt64
        let modified: Date
    }

    private let lock = NSLock()
    private var kept: (stamp: Stamp, reading: Reading)?
    private var decodeCount = 0

    init() {}

    /// How many times the file was read and decoded,  for the tests.
    var decodes: Int {
        lock.lock()
        defer { lock.unlock() }
        return decodeCount
    }

    /// The file's size and modification time,  or nil when they cannot be read.
    static func stamp(of url: URL) -> Stamp? {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = (attributes[.size] as? NSNumber)?.uint64Value,
              let modified = attributes[.modificationDate] as? Date else { return nil }
        return Stamp(size: size, modified: modified)
    }

    /// The reading for the file as it is now.
    ///
    /// - Parameters:
    ///   - decode:  Reads and decodes the file,  which is the work this saves.
    ///   - tooLarge:  The reading for a file over the cap,  given its size in bytes.
    ///   - reuse:  Brings a kept reading up to `now`,  or nil when it has to be read again.
    func read(url: URL,
              now: Date,
              maximumBytes: Int,
              decode: (URL, Date) -> Reading,
              tooLarge: (URL, UInt64) -> Reading,
              reuse: (Reading, Date) -> Reading?) -> Reading {
        // The stamp is taken before the file is read,  so a write that lands during the read
        // leaves a stamp that no longer matches,  and the next read decodes again.
        guard let stamp = Self.stamp(of: url) else {
            // Not there,  or not readable:  the reader says which,  and nothing is kept.
            forget()
            return decode(url, now)
        }
        if stamp.size > UInt64(max(maximumBytes, 0)) {
            forget()
            return tooLarge(url, stamp.size)
        }
        lock.lock()
        let previous = kept
        lock.unlock()
        if let previous, previous.stamp == stamp, let reused = reuse(previous.reading, now) {
            return reused
        }
        let reading = decode(url, now)
        lock.lock()
        kept = (stamp: stamp, reading: reading)
        decodeCount += 1
        lock.unlock()
        return reading
    }

    private func forget() {
        lock.lock()
        kept = nil
        lock.unlock()
    }
}

/// The menu's two report files,  each behind its own cache.
enum PlaceWatchReports {
    /// The largest report file the menu reads.  The collector's reports are far smaller;  a
    /// file this large is not one it wrote,  and it is refused rather than read whole every
    /// minute.
    static let maximumFileBytes = 16 * 1024 * 1024

    static let statusCache = ReportStampCache<ServerStatusReading>()
    static let toolsCache = ReportStampCache<FleetToolsReading>()

    /// The server's own report,  read again only when the file changed.
    static func readStatus(
        at url: URL = ServerStatusReportReader.reportURL(),
        now: Date = Date(),
        cache: ReportStampCache<ServerStatusReading> = PlaceWatchReports.statusCache,
        maximumBytes: Int = PlaceWatchReports.maximumFileBytes
    ) -> ServerStatusReading {
        cache.read(
            url: url,
            now: now,
            maximumBytes: maximumBytes,
            decode: { ServerStatusReportReader.read(at: $0, now: $1) },
            tooLarge: { url, size in
                var reading = ServerStatusReading()
                reading.path = url.path
                reading.problem = "The report could not be read:\(ServerStatusCopy.sentenceGap)"
                    + "the file is \(ServerStatusFormat.bytes(Int(clamping: size))), "
                    + "more than the \(ServerStatusFormat.bytes(maximumBytes)) Muster reads"
                reading.refusal = .unreadable
                return reading
            },
            reuse: { reused(status: $0, now: $1) }
        )
    }

    /// The doctor's report,  read again only when the file changed.
    static func readTools(
        at url: URL = FleetToolsReportReader.reportURL(),
        now: Date = Date(),
        cache: ReportStampCache<FleetToolsReading> = PlaceWatchReports.toolsCache,
        maximumBytes: Int = PlaceWatchReports.maximumFileBytes
    ) -> FleetToolsReading {
        cache.read(
            url: url,
            now: now,
            maximumBytes: maximumBytes,
            decode: { FleetToolsReportReader.read(at: $0, now: $1) },
            tooLarge: { url, size in
                var reading = FleetToolsReading()
                reading.path = url.path
                reading.problem = "The report could not be read:  the file is "
                    + "\(ServerStatusFormat.bytes(Int(clamping: size))), "
                    + "more than the \(ServerStatusFormat.bytes(maximumBytes)) Muster reads"
                return reading
            },
            reuse: { reused(tools: $0, now: $1) }
        )
    }

    /// A kept server reading brought up to `now`,  or nil when the time has changed its verdict.
    ///
    /// The reader uses the time for three things:  the age text,  whether the report is in its
    /// window,  and whether it is dated in the future.  The age text is worked out again from
    /// the kept time;  when either of the other two would come out differently the file is
    /// read again,  because the verdict built on them is the reader's to make.
    static func reused(status reading: ServerStatusReading, now: Date) -> ServerStatusReading? {
        // A reading with no time in it was refused before the time mattered.  A refusal for
        // its version,  tool or role is kept,  but a file that could not be opened or decoded
        // is tried again every minute:  a permission fix moves no modification time,  and a
        // kept failure would never clear.
        guard let generated = reading.generatedAt else {
            return reading.refusal == .unreadable ? nil : reading
        }
        let seconds = Int(now.timeIntervalSince(generated))
        let future = seconds < -60
        guard future == reading.futureDated else { return nil }
        if !future, (seconds <= ServerStatusContract.maxAgeSeconds) != reading.isCurrent { return nil }
        var copy = reading
        copy.age = FleetTime.ageText(since: generated, now: now)
        return copy
    }

    /// A kept tools reading brought up to `now`,  or nil when the time has changed its verdict.
    ///
    /// The host rows carry an age of their own,  which is worked out again the same way.
    static func reused(tools reading: FleetToolsReading, now: Date) -> FleetToolsReading? {
        // Only a refusal for the version or the tool is kept without a time.  Any other
        // problem came from opening or decoding the file,  which is tried again every minute.
        guard let generated = reading.generatedAt else {
            return (reading.refusal == nil && reading.problem != nil) ? nil : reading
        }
        let seconds = Int(now.timeIntervalSince(generated))
        let future = seconds < -60
        // Once the reader has a time,  the only problem it leaves is a report dated in the future.
        let wasFuture = reading.problem != nil
        guard future == wasFuture else { return nil }
        if !future, (seconds <= FleetToolsContract.maxAgeSeconds) != reading.isCurrent { return nil }
        var copy = reading
        copy.age = FleetTime.ageText(since: generated, now: now)
        copy.hosts = reading.hosts.map { host in
            FleetToolsHostVerdict(
                host: host.host,
                reachable: host.reachable,
                state: host.state,
                failing: host.failing,
                unknown: host.unknown,
                stale: host.stale,
                current: host.current,
                absent: host.absent,
                notableRows: host.notableRows,
                allRows: host.allRows,
                reason: host.reason,
                generatedAt: host.generatedAt,
                age: host.generatedAt == nil ? nil : FleetTime.ageText(since: host.generatedAt, now: now),
                expected: host.expected,
                doctorCommit: host.doctorCommit
            )
        }
        return copy
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
            unprovenCells: monitor.unprovenCells,
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
