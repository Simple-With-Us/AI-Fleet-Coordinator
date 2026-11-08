import AppKit
import Combine
import Foundation
import OSLog

/// What the monitor reads from the grid.
///
/// The seam is here so the monitor can be tested without an engine:  a test gives
/// it a grid of its own and watches what it does.
protocol DriftGridReading: AnyObject {
    /// The cells of the last run.
    var cells: [GridCell] { get }
    /// The manifest,  which names the paths to watch.
    var manifest: SeatManifest? { get }
    /// Whether a run is in flight.
    var isRunning: Bool { get }
    /// When the run in flight started,  or nil when none is in flight or the grid cannot say.
    var runStartedAt: Date? { get }
    /// When the run that landed last started,  or nil when the grid cannot say.
    ///
    /// It is kept after the run lands,  because `runStartedAt` is already nil by then.  The
    /// monitor reads it when a run finishes,  to tell a run that started after a wake from one
    /// that started before the sleep and only landed after the wake.
    var lastLandedRunStartedAt: Date? { get }
    /// The fleet-wide compliance results of the last run,  which reach no cell (GRID-18).
    var complianceResults: [CheckResult] { get }
    /// Set by the monitor:  called when a run finishes,  with whether it could be
    /// trusted.
    var onRunFinished: ((Bool) -> Void)? { get set }
    /// Run the engine again.
    func refresh()
}

extension DriftGridReading {
    /// A grid that cannot say when its run started says nothing,  and the monitor then treats
    /// a run in flight at a wake as one from before the sleep.
    var runStartedAt: Date? { nil }
    /// A grid that cannot say when its last run started says nothing,  and the monitor then
    /// never counts that run as covering a wake.
    var lastLandedRunStartedAt: Date? { nil }
    /// A grid with no compliance results has none to alert on.
    var complianceResults: [CheckResult] { [] }
}

/// Says when the Mac wakes from sleep.
protocol DriftWakeObserving: AnyObject {
    /// Call back on the main queue each time the Mac wakes,  until stopped.
    func start(onWake: @escaping () -> Void)
    /// Stop calling back.
    func stop()
}

/// The wake observer the app uses:  the workspace's own wake notice.
final class WorkspaceWakeObserver: DriftWakeObserving {
    private var token: NSObjectProtocol?

    init() {}

    deinit {
        stop()
    }

    func start(onWake: @escaping () -> Void) {
        stop()
        token = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { _ in
            onWake()
        }
    }

    func stop() {
        if let token {
            NSWorkspace.shared.notificationCenter.removeObserver(token)
        }
        token = nil
    }
}

/// A piece of work that is waiting,  and can be taken back.
protocol DriftScheduledWork: AnyObject {
    /// Take the work back,  so it does not run.
    func cancel()
}

/// Something that can run a piece of work later.
protocol DriftScheduling: AnyObject {
    /// Run the work after the delay,  and hand back the handle that takes it back.
    func schedule(after delay: TimeInterval, work: @escaping () -> Void) -> DriftScheduledWork
}

/// The scheduler the app uses:  a background queue,  so a waiting change never
/// touches the interface.
final class DispatchDriftScheduler: DriftScheduling {
    private let queue: DispatchQueue

    init(queue: DispatchQueue = DispatchQueue(label: "muster.drift.debounce", qos: .utility)) {
        self.queue = queue
    }

    func schedule(after delay: TimeInterval, work: @escaping () -> Void) -> DriftScheduledWork {
        let item = DispatchWorkItem(block: work)
        queue.asyncAfter(deadline: .now() + delay, execute: item)
        return DispatchScheduledWork(item: item)
    }
}

/// A waiting job on the dispatch queue.
final class DispatchScheduledWork: DriftScheduledWork {
    private let item: DispatchWorkItem

    init(item: DispatchWorkItem) {
        self.item = item
    }

    func cancel() {
        item.cancel()
    }
}

/// The debounce:  a change starts the clock,  and the run happens when the clock
/// runs out.
///
/// A seat's config is written in pieces,  so one change is usually several writes.
/// The debounce gathers them,  so a run is one run and not one per write.
final class DriftDebouncer {
    /// The window the changes are gathered in.
    ///
    /// A second and a half is long enough for a write that lands in two pieces and
    /// short enough that the owner is not waiting on the app.
    static let interval: TimeInterval = 1.5

    private let interval: TimeInterval
    private let scheduler: DriftScheduling
    private let lock = NSLock()
    private var pending: DriftScheduledWork?

    init(
        interval: TimeInterval = DriftDebouncer.interval,
        scheduler: DriftScheduling = DispatchDriftScheduler()
    ) {
        self.interval = interval
        self.scheduler = scheduler
    }

    /// A change came in,  and the work is due when the window ends.
    ///
    /// The waiting work is taken back first,  so a burst of changes is one run.
    func signal(work: @escaping () -> Void) {
        lock.lock()
        pending?.cancel()
        pending = scheduler.schedule(after: interval, work: work)
        lock.unlock()
    }

    /// Take the waiting work back.
    func cancel() {
        lock.lock()
        pending?.cancel()
        pending = nil
        lock.unlock()
    }
}

/// A timer that fires every interval.
protocol DriftTicking: AnyObject {
    /// Fire the work every interval,  until the ticker is stopped.
    func start(interval: TimeInterval, work: @escaping () -> Void)
    /// Stop firing.
    func stop()
}

/// The safety-net timer:  a source on a background queue.
///
/// The leeway lets the system put the wake-up wherever it likes,  up to a quarter of the
/// interval and never more than a minute.  A tick only reads the clock;  the monitor decides
/// whether a run is due.
final class DispatchDriftTicker: DriftTicking {
    private var timer: DispatchSourceTimer?
    private let queue: DispatchQueue

    init(queue: DispatchQueue = DispatchQueue(label: "muster.drift.safety-net", qos: .utility)) {
        self.queue = queue
    }

    func start(interval: TimeInterval, work: @escaping () -> Void) {
        stop()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        // Wall-clock time,  not uptime:  uptime stops while the Mac sleeps,  so a timer on it
        // would wait out the rest of its interval after a long sleep.  On the wall clock the
        // deadline has passed by then,  and it fires as soon as the Mac wakes.
        timer.schedule(
            wallDeadline: .now() + interval,
            repeating: interval,
            leeway: .seconds(Self.leewaySeconds(for: interval))
        )
        timer.setEventHandler(handler: work)
        timer.resume()
        self.timer = timer
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    /// The leeway for an interval:  a quarter of it,  at least a second and at most a minute.
    static func leewaySeconds(for interval: TimeInterval) -> Int {
        max(1, min(60, Int(interval / 4)))
    }
}

/// The app's drift monitor:  it watches the manifest's paths,  runs the engine when
/// one of them changes,  and says when a cell turns red.
///
/// The three things it keeps fit together like this:
///
///   * the **count** is how many cells the grid paints red right now
///   * the **notice** is posted once for each cell that turned red since the last
///     run that could be trusted
///   * the **history** is every one of those turns,  kept on disk
///
/// A cell that stays red says nothing again:  the owner has been told,  and a
/// notice that repeats itself is noise.  A cell that recovers and turns red again
/// says so again,  because that is a new event.  A fleet-wide compliance result that
/// starts failing goes the same way,  by rule and place,  though it reaches no cell.
///
/// The engine runs when a watched path changes,  when the Mac wakes,  and from the safety
/// net when no trusted run has landed for a whole interval on the wall clock.  It never
/// runs twice at once:  the store shares a run in flight,  the safety net leaves a run in
/// flight alone,  and the wake queues one more run only behind a run from before the sleep.
@MainActor
final class DriftMonitor: ObservableObject {
    /// How old the last trusted run may be before the safety net runs the engine (GRID-33).
    ///
    /// Minutes,  not seconds:  the watcher does the work,  and the safety net is
    /// only there for a change the watcher could not see.  It is measured on the wall clock,
    /// so a sleep counts:  a Mac that slept all night is past it when it wakes.
    nonisolated static let safetyNetInterval: TimeInterval = 10 * 60

    /// How often the safety net looks at the clock.
    ///
    /// A look reads the clock and nothing else;  the engine runs only when the last trusted
    /// run is older than `safetyNetInterval`.  Looking every minute is what keeps a run that
    /// landed just before a look from pushing the next one out to nearly twice the interval.
    nonisolated static let safetyNetCheckInterval: TimeInterval = 60

    /// A gap between two looks longer than this means the Mac slept.
    ///
    /// Two look intervals is well above one interval plus the timer's leeway (a quarter of
    /// it),  so an ordinary look never reads as a sleep,  and a sleep of a few minutes does.
    nonisolated static let sleepGapBetweenLooks: TimeInterval = 2 * DriftMonitor.safetyNetCheckInterval

    /// How long after a wake the engine runs again:  long enough for the network to return
    /// for the checks that read the secret store,  short enough that the counts are not left
    /// unknown for long.
    nonisolated static let wakeRunDelay: TimeInterval = 10

    /// The shortest gap between two runs a watched path asked for.
    ///
    /// Some watched files are written all the time as a matter of course:  Claude Code
    /// rewrites `~/.claude.json` while its sessions run.  Without a floor every such write
    /// was a whole engine run,  back to back.  A change inside the gap is not lost:  it asks
    /// for one run at the end of the gap,  and any further change in the same gap joins it.
    /// A run the owner asks for and the safety net are not held by the floor.
    nonisolated static let watcherRunFloor: TimeInterval = 60

    /// Where the monitor says what it is doing.
    ///
    /// The log is how the app is watched from the outside:  what it watches,  when
    /// it runs and what it found are all things the owner may want to see without
    /// opening the window.
    static let log = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "services.jays.Muster",
        category: "drift"
    )

    /// How many cells the grid paints red,  when the last run could be trusted.
    ///
    /// It is nil when it is not known,  and never zero in that case:  a run that
    /// could not be trusted says nothing about the seats,  and a count of zero
    /// would be a claim the app cannot make.
    @Published private(set) var redCount: Int?
    /// The red cells,  for the menu and the panel.
    @Published private(set) var redCells: [DriftRedCell] = []
    /// The cells that drifted,  which the grid does not paint red and the menu still has
    /// to count:  a calm bar over drifted cells would not be true.
    @Published private(set) var driftCells: [DriftRedCell] = []
    /// The cells that read not proven,  which are neither red nor drifted and which the
    /// menu still has to count:  a calm bar over unproven cells would claim a proof
    /// nobody reached.
    @Published private(set) var unprovenCells: [DriftRedCell] = []
    /// The turns,  newest first.
    @Published private(set) var transitions: [DriftTransition] = []
    /// How many paths the manifest named,  which is what the watcher is asked to watch.
    ///
    /// This is the count asked for,  not the count watched:  `watchCoverage` says how many
    /// streams started and which paths they cover.
    @Published private(set) var watchedPathCount: Int = 0
    /// The seats whose paths are watched.
    @Published private(set) var watchedSeats: [String] = []
    /// What the watcher actually holds,  set against what it was asked for (GRID-37).
    ///
    /// It is worked out again every time the watcher is armed,  from the watches whose
    /// `FSEventStreamStart` returned true,  so a stream that failed to start is counted as
    /// failed and never as watching.
    @Published private(set) var watchCoverage: DriftWatchCoverage = .empty
    /// Whether the monitor is started.
    ///
    /// This says the monitor is running,  not that any stream is:  `watchCoverage` says what is
    /// actually watched.
    @Published private(set) var isWatching: Bool = false
    /// The compliance results that are failing in the last trusted run (GRID-18).
    ///
    /// Empty when the last run could not be trusted,  like the red cells.
    @Published private(set) var complianceFailures: [DriftComplianceFailure] = []
    /// When the last run landed.
    @Published private(set) var lastRunAt: Date?
    /// When the last run that could be trusted landed,  on the monitor's wall clock.
    ///
    /// The safety net is measured from it (GRID-33).
    @Published private(set) var lastTrustedRunAt: Date?
    /// When the last run that could be trusted started,  or nil when the grid could not say.
    ///
    /// A wake is covered only by a trusted run that started after it:  a run that was in
    /// flight when the Mac slept read the Mac as it was then,  and may have read the secret
    /// store while the network was still down,  even when it lands after the wake.
    @Published private(set) var lastTrustedRunStartedAt: Date?
    /// Whether the last run could be trusted.
    @Published private(set) var lastRunWasTrusted: Bool?
    /// Why the history could not be written,  when it could not.
    @Published private(set) var historyProblem: String?

    private let grid: DriftGridReading
    private let notifier: ApplyNotifying
    private let history: DriftHistoryStore
    private let watcher: DriftWatching
    private let ticker: DriftTicking
    private let wake: DriftWakeObserving
    private let debouncer: DriftDebouncer
    private let home: URL
    /// How the watch list is built,  which is the only place the app looks at the
    /// file system while it waits.
    private let buildWatchList: (SeatManifest?, URL) -> DriftWatchList

    /// The states of the last run that could be trusted.
    private var baseline: [String: CellState] = [:]
    /// What the last run that could be trusted said about each compliance result,  by rule
    /// and place.  It is read with the same rules as the cells' baseline.
    private var complianceBaseline: [String: ComplianceAlertFinder.Mark] = [:]
    /// Whether there is a baseline to compare with.
    private var hasBaseline: Bool = false
    /// When the last run that could not be trusted landed,  on the monitor's clock.  The
    /// safety net waits a whole interval after one,  so an engine that cannot run is not
    /// started again every minute.
    private var lastFailedRunAt: Date?
    /// When the safety net last looked at the clock.  A gap far longer than its look interval
    /// means the Mac slept.
    private var lastTickAt: Date?
    /// When the Mac last woke,  while the run the wake asked for has not been decided.
    private var wokeAt: Date?
    /// Until when the safety net leaves the engine alone after a wake,  so the run after a
    /// wake waits for the network instead of failing the secret store checks.
    private var wakeHoldUntil: Date?
    /// The run waiting for the network after a wake.
    private var wakeWork: DriftScheduledWork?
    /// Whether a watched change arrived while a run was in flight.
    private var watcherRunPending: Bool = false
    /// Whether a wake asked for a run while a run from before the sleep was in flight.
    private var otherRunPending: Bool = false

    /// Where the floor's waiting run is scheduled.
    private let scheduler: DriftScheduling
    /// The shortest gap between two runs a watched path asked for.
    private let watcherRunFloor: TimeInterval
    /// The time,  which a test sets by hand.
    private let clock: () -> Date
    /// When the last run a watched path asked for started.
    private var lastWatcherRunAt: Date?
    /// The one run waiting for the end of the gap,  when there is one.
    private var floorWork: DriftScheduledWork?
    /// The watches that hold a stream now,  in the list's order,  so an unchanged list is
    /// not armed again.  Nil until the first arming after a start.
    private var armedWatches: [DriftWatchList.Watch]?

    init(
        grid: DriftGridReading,
        notifier: ApplyNotifying = SystemApplyNotifier(),
        history: DriftHistoryStore = DriftHistoryStore(),
        watcher: DriftWatching = FSEventsDriftWatcher(),
        scheduler: DriftScheduling = DispatchDriftScheduler(),
        ticker: DriftTicking = DispatchDriftTicker(),
        wake: DriftWakeObserving = WorkspaceWakeObserver(),
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        buildWatchList: @escaping (SeatManifest?, URL) -> DriftWatchList = {
            DriftWatchListBuilder.build(manifest: $0, home: $1)
        },
        watcherRunFloor: TimeInterval = DriftMonitor.watcherRunFloor,
        clock: @escaping () -> Date = { Date() }
    ) {
        self.grid = grid
        self.notifier = notifier
        self.history = history
        self.watcher = watcher
        self.ticker = ticker
        self.wake = wake
        self.scheduler = scheduler
        self.debouncer = DriftDebouncer(scheduler: scheduler)
        self.home = home
        self.buildWatchList = buildWatchList
        self.watcherRunFloor = watcherRunFloor
        self.clock = clock
        self.transitions = history.load()
    }

    // MARK: Life

    /// Arm the watcher and the safety net.
    ///
    /// It is safe to call this again:  the monitor starts once and stays started
    /// for as long as the app runs,  whether or not the window is open.
    func start() {
        guard !isWatching else { return }
        isWatching = true
        grid.onRunFinished = { [weak self] trusted in
            Task { @MainActor in self?.runFinished(trusted: trusted) }
        }
        armWatcher()
        ticker.start(interval: Self.safetyNetCheckInterval) { [weak self] in
            Task { @MainActor in self?.safetyNetTick() }
        }
        wake.start { [weak self] in
            Task { @MainActor in self?.handleWake() }
        }
    }

    /// Put the watcher away and stop the safety net.
    func stop() {
        isWatching = false
        grid.onRunFinished = nil
        watcher.stop()
        armedWatches = nil
        watchCoverage = .empty
        ticker.stop()
        wake.stop()
        wakeWork?.cancel()
        wakeWork = nil
        wokeAt = nil
        wakeHoldUntil = nil
        lastTickAt = nil
        debouncer.cancel()
        floorWork?.cancel()
        floorWork = nil
        // The gap belongs to the session that is ending:  the first watched change after a
        // start runs once its window ends,  and never waits out a gap from before the stop.
        lastWatcherRunAt = nil
        watcherRunPending = false
        otherRunPending = false
    }

    // MARK: Watching

    /// Arm the watches the manifest's paths ask for.
    ///
    /// The list is built again after every run,  because a path that was not there may
    /// be there now and a path that was there may be gone,  and a watch on a path that
    /// has moved is a watch on nothing.  The watcher is touched only where the list and the
    /// streams it holds differ:  the comparison takes in each watch's root and whether its
    /// target is a directory,  so a path that appeared or moved gets a stream of its own,  a
    /// path that went away loses its stream,  and every other stream keeps running,  which
    /// also keeps the events a restart would have dropped.  A watch whose stream could not
    /// be made is asked for again after the next run,  and only that watch:  the streams that
    /// did arm are kept.
    private func armWatcher() {
        let list = buildWatchList(grid.manifest, home)
        watchedPathCount = list.targetPaths.count
        watchedSeats = list.seats
        // The coverage is read from the watcher after any arming below,  so it counts the
        // streams that started and never the ones that were only asked for.
        defer { watchCoverage = DriftWatchCoverage.of(list: list, started: watcher.startedWatches) }
        if let armedWatches, armedWatches == list.watches { return }
        let held = armedWatches ?? []
        let gone = held.filter { !list.watches.contains($0) }
        if !gone.isEmpty {
            watcher.disarm(gone)
        }
        let kept = held.filter { list.watches.contains($0) }
        let missing = list.watches.filter { !kept.contains($0) }
        Self.log.info("Watching \(list.targetPaths.count, privacy: .public) paths for \(list.seats.count, privacy: .public) seats; \(missing.count, privacy: .public) watches to arm, \(gone.count, privacy: .public) let go")
        let newlyArmed = watcher.arm(missing) { [weak self] _ in
            self?.changeDetected()
        }
        let armed = list.watches.filter { kept.contains($0) || newlyArmed.contains($0) }
        armedWatches = armed
        if armed.count < list.watches.count {
            Self.log.error("Only \(armed.count, privacy: .public) of \(list.watches.count, privacy: .public) watches were armed; the next run tries the rest again")
        }
    }

    /// A change was seen,  and the run is due when the window ends.
    ///
    /// This runs on the watcher's queue,  so it does nothing but hand the change to
    /// the main actor.
    nonisolated private func changeDetected() {
        Task { @MainActor [weak self] in self?.handleChange() }
    }

    /// The debounced part of a change.
    private func handleChange() {
        Self.log.info("A watched path changed; a run is due")
        debouncer.signal { [weak self] in
            Task { @MainActor in self?.watcherRunDue() }
        }
    }

    // MARK: Running

    /// A watched change asked for a run,  which the floor may hold back.
    ///
    /// Inside the gap after the last such run,  one run is scheduled for the end of the
    /// gap and every later change in the gap joins it,  so a file that is written all the
    /// time costs one run a gap and not one run a write.
    private func watcherRunDue() {
        // One run already waits for the end of the gap,  and it will read this change too.
        guard floorWork == nil else { return }
        if let lastWatcherRunAt {
            let wait = watcherRunFloor - clock().timeIntervalSince(lastWatcherRunAt)
            if wait > 0 {
                Self.log.info("A watched path changed inside the gap; one run waits \(Int(wait.rounded(.up)), privacy: .public) seconds")
                floorWork = scheduler.schedule(after: wait) { [weak self] in
                    Task { @MainActor in self?.floorGapEnded() }
                }
                return
            }
        }
        refresh(reason: "a watched path changed", fromWatcher: true)
    }

    /// The gap ended,  and the run that waited for it starts.
    private func floorGapEnded() {
        floorWork = nil
        guard isWatching else { return }
        refresh(reason: "the gap after a watched change ended", fromWatcher: true)
    }

    // MARK: The safety net and the wake (GRID-33)

    /// Whether the safety net should run the engine now.
    ///
    /// It is due when the last trusted run is at least one interval old on the wall clock,
    /// or there has been none.  A run that could not be trusted holds it for one interval as
    /// well,  so an engine that cannot run is tried every interval and not every look.  A time
    /// in the future (the clock was set back) is not trusted to hold the net.
    nonisolated static func safetyNetIsDue(
        now: Date,
        lastTrustedRunAt: Date?,
        lastFailedRunAt: Date?,
        interval: TimeInterval = DriftMonitor.safetyNetInterval
    ) -> Bool {
        for last in [lastTrustedRunAt, lastFailedRunAt] {
            guard let last else { continue }
            let age = now.timeIntervalSince(last)
            if age >= 0 && age < interval { return false }
        }
        return true
    }

    /// The safety net looks at the clock.
    ///
    /// A run in flight is the run the net would start,  so the net leaves it alone and asks
    /// for nothing after it:  that run lands and moves the clock the net reads.  Inside the
    /// hold after a wake the run the wake asked for is due,  so the net leaves that alone too.
    private func safetyNetTick() {
        guard isWatching else { return }
        let now = clock()
        let previous = lastTickAt
        lastTickAt = now
        if let previous,
           now.timeIntervalSince(previous) > Self.sleepGapBetweenLooks,
           Self.safetyNetIsDue(now: now, lastTrustedRunAt: lastTrustedRunAt, lastFailedRunAt: lastFailedRunAt) {
            // The looks stop while the Mac sleeps,  so a gap this long means it slept,  and the
            // wake notice may not have arrived yet.  The run the net would start now waits for
            // the network,  as the wake's own run does,  rather than starting at once and
            // failing the secret store.  When the net is not due nothing would run now anyway,
            // and the wake notice asks for its own run,  so a look that was only held up (the
            // system may delay a timer) never adds a run of its own.
            Self.log.info("The safety net found a long gap since its last look; the Mac slept")
            handleWake()
            return
        }
        if let wakeHoldUntil, now < wakeHoldUntil { return }
        guard !grid.isRunning else { return }
        guard Self.safetyNetIsDue(
            now: now,
            lastTrustedRunAt: lastTrustedRunAt,
            lastFailedRunAt: lastFailedRunAt
        ) else { return }
        refresh(reason: "the safety net ran:  no trusted run for \(Int(Self.safetyNetInterval / 60)) minutes")
    }

    /// The Mac woke:  the engine runs again once the network has had a moment to return.
    ///
    /// A second wake,  or a wake the safety net already noticed,  moves the one waiting run
    /// rather than adding another.
    private func handleWake() {
        guard isWatching else { return }
        let now = clock()
        wokeAt = now
        wakeHoldUntil = now.addingTimeInterval(Self.wakeRunDelay)
        lastTickAt = now
        wakeWork?.cancel()
        wakeWork = scheduler.schedule(after: Self.wakeRunDelay) { [weak self] in
            Task { @MainActor in self?.wakeRunDue() }
        }
    }

    /// The network has had its moment after a wake,  and the engine runs,  unless a trusted
    /// run that started after the wake already covers it.
    ///
    /// Cover is judged by when the run started,  not when it landed:  a run that was in flight
    /// when the Mac slept and lands just after the wake read the Mac as it was before,  and
    /// perhaps the secret store while the network was down,  so it never covers the wake.  A
    /// run whose start the grid cannot say covers nothing.
    ///
    /// Never a second run at once:  a run in flight that started after the wake is the run the
    /// wake asks for,  and one that started before the sleep read the Mac as it was then,  so
    /// one more run follows it when it lands.
    private func wakeRunDue() {
        wakeWork = nil
        wakeHoldUntil = nil
        guard isWatching, let wokeAt else { return }
        self.wokeAt = nil
        if let lastTrustedRunStartedAt, lastTrustedRunStartedAt >= wokeAt { return }
        if grid.isRunning {
            if let started = grid.runStartedAt, started >= wokeAt { return }
            otherRunPending = true
            return
        }
        refresh(reason: "the Mac woke")
    }

    /// Run the engine again.
    private func refresh(reason: String, fromWatcher: Bool = false) {
        if grid.isRunning {
            // A run is already in flight and it may have read the files before the
            // change,  so another run is asked for when it ends.
            if fromWatcher {
                watcherRunPending = true
            } else {
                otherRunPending = true
            }
            return
        }
        if fromWatcher {
            lastWatcherRunAt = clock()
        }
        Self.log.info("The engine is asked to run again: \(reason, privacy: .public)")
        grid.refresh()
    }

    /// A run landed.
    private func runFinished(trusted: Bool) {
        lastRunAt = Date()
        lastRunWasTrusted = trusted
        if trusted {
            lastTrustedRunAt = clock()
            lastTrustedRunStartedAt = grid.lastLandedRunStartedAt
        } else {
            lastFailedRunAt = clock()
        }
        let watcherPending = watcherRunPending
        let otherPending = otherRunPending
        watcherRunPending = false
        otherRunPending = false

        if trusted {
            evaluate(cells: grid.cells, compliance: grid.complianceResults)
        } else {
            forgetTheCount()
        }
        let countText = redCount.map { "\($0)" } ?? "unknown"
        Self.log.info("A run landed; trusted: \(trusted, privacy: .public); red cells: \(countText, privacy: .public)")

        // The watch list is built again,  because the paths may have moved.
        armWatcher()

        if otherPending {
            // The run that just landed started before the Mac slept,  and the wake asked for
            // one more.  It is not held by the floor.  A watched change that also came in is
            // read by this same run,  so it starts the gap as well.
            refresh(reason: "the Mac woke during a run from before the sleep", fromWatcher: watcherPending)
        } else if watcherPending {
            // A watched change during the run waits for the floor like any other,  so a
            // file written all the time cannot keep the engine running back to back.
            watcherRunDue()
        }
    }

    /// Read the run and say what changed.
    ///
    /// The compliance results reach no cell,  so they are read beside the cells with the same
    /// rules (GRID-18):  the first trusted run is a reading,  a result that keeps failing says
    /// nothing again,  and one that recovers and fails again says so again.
    private func evaluate(cells: [GridCell], compliance: [CheckResult]) {
        complianceFailures = ComplianceAlertFinder.failures(in: compliance)
        let reds = cells.filter { $0.state.isRed }
        redCells = reds.map { cell in
            DriftRedCell(
                seat: cell.seat,
                kind: cell.kind.rawValue,
                ruleID: cell.rules.first?.ruleId,
                state: cell.state
            )
        }
        redCount = reds.count
        driftCells = cells.filter { $0.state == .drift }.map { cell in
            DriftRedCell(
                seat: cell.seat,
                kind: cell.kind.rawValue,
                ruleID: cell.rules.first?.ruleId,
                state: cell.state
            )
        }
        unprovenCells = cells.filter { $0.state == .unproven }.map { cell in
            DriftRedCell(
                seat: cell.seat,
                kind: cell.kind.rawValue,
                ruleID: cell.rules.first?.ruleId,
                state: cell.state
            )
        }

        guard hasBaseline else {
            // The first run that could be trusted is a reading and not a change:
            // there is no earlier answer to compare it with,  and a notice for
            // every cell on the first run is a notice storm.
            baseline = DriftTransitionFinder.states(of: cells)
            complianceBaseline = ComplianceAlertFinder.marks(of: compliance)
            hasBaseline = true
            return
        }

        let found = DriftTransitionFinder.transitions(previous: baseline, current: cells)
            + ComplianceAlertFinder.transitions(previous: complianceBaseline, current: compliance)
        baseline = DriftTransitionFinder.states(of: cells)
        complianceBaseline = ComplianceAlertFinder.marks(of: compliance)
        for transition in found {
            keep(transition)
        }
        if !found.isEmpty {
            saveHistory()
            for transition in found {
                notify(transition)
            }
        }
    }

    /// A run that could not be trusted says nothing about the seats.
    private func forgetTheCount() {
        redCount = nil
        redCells = []
        driftCells = []
        unprovenCells = []
        complianceFailures = []
    }

    // MARK: Recording

    /// Keep a turn in memory. The disk write is batched by the caller, so a
    /// run that turns many cells red writes the history once, not per cell.
    private func keep(_ transition: DriftTransition) {
        transitions.insert(transition, at: 0)
        if transitions.count > DriftHistoryStore.limit {
            transitions.removeLast(transitions.count - DriftHistoryStore.limit)
        }
    }

    /// Consecutive history-write failures and when the next attempt is due.
    ///
    /// A disk that stays unwritable must not be retried on every run: the
    /// delay doubles from a second to a five-minute cap, and any success
    /// resets it. The in-memory history and `historyProblem` still update
    /// on every turn, so nothing is hidden while writes are backed off.
    private var historyFailureCount = 0
    private var nextHistorySaveAt: Date?

    /// Write the history once, before any notice goes out, so a notice that
    /// the system refuses never costs the record.
    private func saveHistory() {
        if let nextHistorySaveAt, Date() < nextHistorySaveAt { return }
        if history.save(transitions) {
            historyProblem = nil
            historyFailureCount = 0
            nextHistorySaveAt = nil
        } else {
            historyProblem = "The drift history could not be written to \(history.url.path)."
            historyFailureCount += 1
            let delay = min(300.0, pow(2.0, Double(min(historyFailureCount - 1, 8))))
            nextHistorySaveAt = Date().addingTimeInterval(delay)
        }
    }

    /// Post the notice for a turn.
    ///
    /// The notice is posted off the run's path,  so a notification that waits on
    /// the system can never hold the grid hostage.  The notice says which seat and
    /// which check turned red,  and the history keeps the rest.
    private func notify(_ transition: DriftTransition) {
        let words = Self.noticeWords(for: transition)
        let title = words.title
        let body = words.body
        Self.log.info("A check started failing: \(transition.seat, privacy: .public) / \(transition.kind, privacy: .public), check \(transition.checkText, privacy: .public); the notice goes to the system")
        Task {
            await notifier.post(title: title, body: body)
        }
    }

    /// The notice's title and body for a turn.
    ///
    /// A cell's notice says the cell turned red.  A compliance rule is about a host and not a
    /// seat's config,  and its failure is often DRIFT,  which the grid does not paint red,  so
    /// its notice says the rule failed and where.
    nonisolated static func noticeWords(for transition: DriftTransition) -> (title: String, body: String) {
        if transition.isCompliance {
            return (
                title: "Compliance Alert",
                body: "\(transition.seat) / \(transition.kindTitle):  "
                    + "\(transition.checkText) failed "
                    + "(\(transition.fromText) to \(transition.toState))."
            )
        }
        return (
            title: "Drift Alert \(FleetHost.mac.onTitle)",
            body: "\(transition.seat) / \(transition.kindTitle):  "
                + "\(transition.checkText) turned red "
                + "(\(transition.fromText) to \(transition.toState))."
        )
    }

    /// Empty the history,  on disk and in the panel.
    func clearHistory() {
        transitions = []
        if history.clear() {
            historyProblem = nil
        } else {
            historyProblem = "The drift history could not be cleared at \(history.url.path)."
        }
    }
}
