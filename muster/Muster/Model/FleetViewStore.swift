import Combine
import Foundation

/// The Apps & Work page's reading,  held for the whole app.
///
/// The page's view comes and goes as the owner moves around the sidebar,  and a view's own
/// state goes with it,  so each return to the page used to run `fleet-view status` again from
/// nothing.  This store keeps the reading for as long as the app runs,  and at most one read
/// of a checkout is in flight:
///
///   * a second refresh of the same checkout while a read runs waits for that read and starts
///     nothing;  a refresh of another checkout starts its own read,  and only the newest read
///     writes the page
///   * a return to the page shows the last reading at once,  and reads again only when the
///     last good read is older than the window the caller names (`refreshIfOlder(than:)`)
///
/// The read is the page's own,  rule for rule.  A failed read fails closed:  it clears the
/// envelope and the registry rows and sets the failure,  so the page never keeps the last
/// green report on screen as if it were fresh.
@MainActor
final class FleetViewStore: ObservableObject {
    /// The one store the page uses.
    static let shared = FleetViewStore()

    /// How the page is read.  The defaults read the real sources;  a test gives its own.
    struct Loader {
        /// Runs `fleet-view status --json` and decodes its answer.
        var loadView: @Sendable () async throws -> FleetViewEnvelope
        /// Reads the doctor's tools report.
        var readTools: @Sendable () -> FleetToolsReading
        /// Builds the registry rows from `fleet-apps.json` at a checkout,  the run's answer
        /// and the tools report.
        var readRegistry: @Sendable (URL, FleetViewEnvelope, FleetToolsReading) -> FleetRegistrySummary
        /// Finds the checkout when the caller did not name one.
        var locateRoot: @Sendable () -> URL?

        init(
            loadView: @escaping @Sendable () async throws -> FleetViewEnvelope = {
                try await FleetViewService.load()
            },
            readTools: @escaping @Sendable () -> FleetToolsReading = {
                FleetToolsReportReader.readDefault()
            },
            readRegistry: @escaping @Sendable (URL, FleetViewEnvelope, FleetToolsReading) -> FleetRegistrySummary = {
                FleetRegistryReader.read(repoRoot: $0, viewOutput: $1, toolsReading: $2)
            },
            locateRoot: @escaping @Sendable () -> URL? = { EngineLocator.locate()?.root }
        ) {
            self.loadView = loadView
            self.readTools = readTools
            self.readRegistry = readRegistry
            self.locateRoot = locateRoot
        }
    }

    /// The registry's problem when no checkout could be found.
    nonisolated static let missingCheckoutProblem =
        "Muster could not find the AI-Fleet-Coordinator checkout, so apps and coding apps cannot be listed.\u{00A0} The pull requests, claims and lanes on this page are from this refresh."

    /// The last good run's answer,  or nil before one and after a failed read.
    @Published private(set) var envelope: FleetViewEnvelope?
    /// The apps and coding apps,  built from the last good run.  Empty after a failed read.
    @Published private(set) var registrySummary = FleetRegistrySummary()
    /// Why the last read failed,  or nil when it did not.
    @Published private(set) var failure: FleetViewFailure?
    /// Whether a read is running now.
    @Published private(set) var loading = false
    /// The header's times:  when the data was collected and when a read last failed.
    @Published private(set) var refreshClock = FleetViewRefreshClock()
    /// When the last good read landed,  by this store's clock.  Nil before one,  and after a
    /// failed read,  so a page that comes back to a failure reads again once
    /// `failureRetryAfter` has passed.
    @Published private(set) var lastSuccessAt: Date?
    /// When the last read failed,  by this store's clock.  Nil after a good read.
    @Published private(set) var lastFailureAt: Date?
    /// After a failed read,  `refreshIfOlder` tries again no sooner than this,  so a page or a
    /// phone that keeps asking while fleet-view keeps failing does not rerun its GitHub and
    /// Linear reads every time.  A plain `refresh()` (the Refresh button) is never held back.
    static let failureRetryAfter: TimeInterval = 60
    /// How many reads this store started.
    private(set) var readsStarted = 0

    /// A read that is running,  with the checkout it was asked for and its place in line.
    private struct Flight {
        /// The checkout the caller named,  or nil when the loader looks for it.
        let engineRoot: URL?
        /// Which read this is.  Only the newest read may write the page.
        let generation: Int
        let task: Task<Void, Never>
    }

    /// The newest read,  while it runs.
    private var inFlight: Flight?
    /// The number of the newest read started.  A read whose number is lower has been
    /// overtaken,  and drops its answer.
    private var latestGeneration = 0
    private let loader: Loader
    private let now: () -> Date

    init(loader: Loader = Loader(), now: @escaping () -> Date = { Date() }) {
        self.loader = loader
        self.now = now
    }

    /// Whether the last read failed.
    var failed: Bool { failure != nil }

    /// Read now,  unless a read of the same checkout is already running,  in which case wait
    /// for that one.
    ///
    /// A refresh that names another checkout than the running read does not join it,  since
    /// that read's registry rows are another checkout's:  it starts a read of its own,  and
    /// the older read drops its answer when it lands,  so it can never write over the newer
    /// checkout's rows.
    ///
    /// - Parameter engineRoot:  The checkout the app's own store found,  when it found one.
    ///   Without it the loader looks for the checkout itself,  as the page always has.
    func refresh(engineRoot: URL? = nil) async {
        if let running = inFlight, running.engineRoot == engineRoot {
            await running.task.value
            return
        }
        readsStarted += 1
        latestGeneration += 1
        let generation = latestGeneration
        loading = true
        // The read runs in a task of its own,  so the page's own task ending (the owner moving
        // to another page) does not call it off:  the reading is there when the owner comes back.
        let task = Task { [weak self] in
            guard let self else { return }
            await self.read(engineRoot: engineRoot, generation: generation)
            // An overtaken read leaves the newer one's flight and spinner alone.
            if self.inFlight?.generation == generation {
                self.inFlight = nil
                self.loading = false
            }
        }
        inFlight = Flight(engineRoot: engineRoot, generation: generation, task: task)
        await task.value
    }

    /// Read unless the last good read is younger than `maximumAge`,  or the last read failed
    /// less than `failureRetryAfter` (or `maximumAge`,  if shorter) ago.
    func refreshIfOlder(than maximumAge: TimeInterval, engineRoot: URL? = nil) async {
        if let lastSuccessAt, now().timeIntervalSince(lastSuccessAt) < maximumAge {
            return
        }
        if let lastFailureAt,
           now().timeIntervalSince(lastFailureAt) < min(maximumAge, Self.failureRetryAfter) {
            return
        }
        await refresh(engineRoot: engineRoot)
    }

    /// One read,  as the page has always made it.
    ///
    /// The answer is worked out in full first and written to the page in one step,  and only
    /// when this is still the newest read:  a read another checkout's refresh overtook drops
    /// its answer.
    private func read(engineRoot: URL?, generation: Int) async {
        let readTools = loader.readTools
        let readRegistry = loader.readRegistry
        let loadView = loader.loadView
        let root = engineRoot ?? loader.locateRoot()
        let tools = await Task.detached(priority: .userInitiated) {
            readTools()
        }.value
        do {
            let loaded = try await loadView()
            let summary: FleetRegistrySummary
            if let root {
                summary = await Task.detached(priority: .userInitiated) {
                    readRegistry(root, loaded, tools)
                }.value
            } else {
                // Never leave the last run's rows beside a fresh one, and never let a checkout
                // Muster cannot find read as a filter that hides every app (FLEETVIEW-30).
                summary = FleetRegistrySummary(problem: Self.missingCheckoutProblem)
            }
            guard generation == latestGeneration else { return }
            envelope = loaded
            failure = nil
            refreshClock.recordSuccess(generatedAt: loaded.generatedAt, at: now())
            lastSuccessAt = now()
            lastFailureAt = nil
            registrySummary = summary
        } catch {
            // Fail closed:  clear the lists, never show the last green.  The registry
            // rows go with them:  read back without the run's answer they carry lane,
            // pull request and claim counts of zero, which reads as a fleet measured
            // and found idle rather than one this refresh could not see.  The banner
            // above the list says the counts are unknown;  nothing under it may spell
            // a number of its own.  The words are worked out off the main thread, because
            // stderr can be 4 MB, and the lists clear after that, so the page is never empty
            // with no banner.
            let described = await Task.detached(priority: .userInitiated) {
                FleetViewFailure(error: error)
            }.value
            guard generation == latestGeneration else { return }
            envelope = nil
            failure = described
            refreshClock.recordFailure(at: now())
            lastSuccessAt = nil
            lastFailureAt = now()
            registrySummary = FleetRegistrySummary()
        }
    }
}
