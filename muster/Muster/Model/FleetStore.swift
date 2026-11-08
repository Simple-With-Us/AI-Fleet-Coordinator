import Foundation
import Combine

/// The app's single source of truth for the grid.
///
/// The store owns the run, not the view:  a view asks for cells and renders them, and
/// the store decides what a cell says.  That is what keeps the "installed is not
/// proved" lesson in one place, because the rule lives in the store:  a run that cannot
/// be trusted clears the results, and a cell with no result is unknown, never the last
/// green that happened to be on screen.
final class FleetStore: ObservableObject {
    /// Where the app is in its run.
    enum Phase: Equatable {
        case idle
        case running
        case loaded
        case failed(String)
    }

    /// How the store runs the engine:  the location,  and the completion to call with
    /// what the run produced.  The app runs `EngineRunner`;  a test hands in its own.
    typealias EngineRunning = (EngineLocation, @escaping (EngineRun) -> Void) -> Void

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var roster: [String] = []
    @Published private(set) var cells: [GridCell] = []
    @Published private(set) var summary: EnvelopeSummary?
    @Published private(set) var generatedAt: Date?
    @Published private(set) var engineRoot: URL?
    @Published private(set) var commandLine: String = ""
    /// Whether a run is in flight while the grid shows the last answer.
    ///
    /// A run no longer clears the grid when it starts:  the cells,  the summary and the
    /// clock stay as the last run left them,  each cell is marked as being checked
    /// (`GridCell.isBeingChecked`),  and the view says so.  A run that fails clears them
    /// all,  so nothing stale stays on screen as current.  A cell with no answer yet keeps
    /// the state `unknown`,  and `GridCell.isCheckingWithNoAnswer` lets a view draw it as
    /// being checked rather than as a red UNKNOWN.
    @Published private(set) var isChecking: Bool = false
    /// The surfaces the engine names and does not check,  from the last trusted run.
    @Published private(set) var uncovered: [UncoveredSurface] = []
    /// The fleet-wide rules the engine cannot run yet,  from the last trusted run.
    @Published private(set) var notImplemented: [NotImplementedRule] = []
    /// Why the manifest could not be read,  when it is there and cannot be read.
    ///
    /// The manifest is read again before each run when the file changed,  so the
    /// problem clears when a later read succeeds.  A changed manifest that cannot be read
    /// keeps the last copy that could,  with this problem beside it.
    @Published private(set) var manifestProblem: String?
    /// Why the registry could not be read,  when it is there and cannot be read.
    @Published private(set) var registryProblem: String?
    /// The check time the run shares,  which is one timestamp for nearly every rule.
    ///
    /// The engine checks every rule in one pass,  so every cell would otherwise carry
    /// the same age and the grid would read as ninety copies of one number.  The shared
    /// time goes beside the table once,  and a cell shows an age only when it is the
    /// exception:  a rule served from the cache and checked earlier than the rest.
    @Published private(set) var commonCheckedAt: Date?

    private let location: EngineLocation?

    /// Where the engine lives,  for a surface that runs it.
    var engineLocation: EngineLocation? { location }

    /// The engine's own answer about which Infisical project its copy belongs to.
    ///
    /// The answer is read once and kept,  because the engine's project does not
    /// change while the app runs and every visit to a surface that needs it should
    /// not start another interpreter.  The read runs off the main actor,  and the
    /// task is the store's,  so a surface that goes away leaves the answer cached
    /// rather than the work in flight.
    ///
    /// A read that fails is not kept.  The engine can fail for a moment  (it is
    /// another program,  on another machine's answer),  and a tab that could never
    /// recover until the app was relaunched would be worse than a second read.
    func engineAnswer() async -> EngineIdentity.Answer {
        let box: AnswerBox
        answerLock.lock()
        if let existing = answerBox {
            box = existing
            answerLock.unlock()
        } else if let location = self.location {
            let made = AnswerBox(
                task: Task<EngineIdentity.Answer, Never>.detached(priority: .userInitiated) {
                    EngineIdentity.read(
                        location: location,
                        home: FileManager.default.homeDirectoryForCurrentUser
                    )
                }
            )
            answerBox = made
            box = made
            answerLock.unlock()
        } else {
            answerLock.unlock()
            return EngineIdentity.Answer(identity: nil, reason: "the engine was not found")
        }
        let answer = await box.task.value
        if answer.identity == nil {
            answerLock.lock()
            // Only the task that failed is dropped:  a newer one may already be
            // running,  and dropping it would start a second interpreter.
            if answerBox === box { answerBox = nil }
            answerLock.unlock()
        }
        return answer
    }

    /// The one read of the engine's answer,  and the lock that guards it.
    private final class AnswerBox {
        let task: Task<EngineIdentity.Answer, Never>
        init(task: Task<EngineIdentity.Answer, Never>) { self.task = task }
    }

    private let answerLock = NSLock()
    private var answerBox: AnswerBox?
    /// The manifest,  which names the paths the seats' rules check.
    ///
    /// It is exposed because it is the only place that knows where a seat's config
    /// lives:  the panel reads the artifacts from it and the drift watcher watches the
    /// paths it names.  It is read at launch and again before a run whenever
    /// `fleet-seats.json` changed size or modification time (SHELL-29),  so a seat added
    /// while the app runs gets its row,  and the watcher,  which re-arms from it after
    /// every run,  watches its paths.
    ///
    /// It is written on the main thread only.  A reader off the main thread (the panel
    /// reads a cell's artifacts in a detached task) reads `manifestSnapshot` under
    /// `inputsLock` instead,  so a re-read during that task is never a data race.
    @Published private(set) var manifest: SeatManifest?
    /// The manifest as readers off the main thread see it,  guarded by `inputsLock`.
    private var manifestSnapshot: SeatManifest?
    private let inputsLock = NSLock()
    private var registry: FleetRegistry?
    /// The size and modification time each file had when it was last read.
    private var manifestStamp: LedgerParseCache.Stamp?
    private var registryStamp: LedgerParseCache.Stamp?
    /// Whether the two files have been read at all.
    private var inputsRead = false
    /// Whether the results came from an engine answer,  rather than from nothing.
    private var answered = false
    private let runEngine: EngineRunning
    private var results: [CheckResult] = []
    private var cellsByKey: [String: GridCell] = [:]

    /// Called when a run finishes,  with whether the run could be trusted.
    ///
    /// The grid only changes when a run lands,  so this is how a watcher learns
    /// that the cells are current.  It is set by the drift monitor.
    var onRunFinished: ((Bool) -> Void)?

    /// Whether a run is in flight.
    var isRunning: Bool { phase == .running }

    /// When the run in flight started,  on the wall clock,  or nil when none is in flight.
    ///
    /// The drift monitor reads it after a wake:  a run that started before the Mac slept read
    /// the Mac as it was then,  and one that started after the wake is the run the wake asks for.
    private(set) var runStartedAt: Date?

    /// When the run that landed last started,  on the wall clock,  or nil when none has landed
    /// or the last refresh found no engine to run.
    ///
    /// It is set as the run lands and kept until the next one lands,  because `runStartedAt` is
    /// already nil by the time the drift monitor hears the run finished.  The monitor reads it
    /// to tell a run that started after a wake from one that only landed after it.
    private(set) var lastLandedRunStartedAt: Date?

    // MARK: Compliance rules (GRID-18)

    /// The engine's results for the fleet-wide compliance rules,  from the last answer.
    ///
    /// The grid's cells are one seat against one config kind,  so these results reach no
    /// cell.  They are kept here instead of being dropped.  They are empty before the first
    /// answer and after a failed run,  and while a run is in flight they are the last answer's
    /// (`isShowingLastAnswer`).
    var complianceResults: [CheckResult] {
        answered ? results.filter { $0.isCompliance } : []
    }

    /// Every compliance rule the manifest names or the engine answered for,  each with its
    /// real state,  in manifest order.
    ///
    /// A rule the engine did not run,  or cannot run yet,  never reads as passing:  it reads as
    /// not built,  not in the answer or not answered.
    var complianceStatuses: [ComplianceRuleStatus] {
        ComplianceStatusBuilder.build(
            rules: manifest?.allComplianceRules ?? [],
            results: results,
            notImplemented: notImplemented,
            answered: answered
        )
    }

    /// One compliance rule's state,  or nil when neither the manifest nor the engine names it.
    func complianceStatus(ruleID: String) -> ComplianceRuleStatus? {
        complianceStatuses.first { $0.ruleID == ruleID }
    }

    /// The app's store:  the engine this Mac has,  run by `EngineRunner`.
    convenience init() {
        self.init(
            location: EngineLocator.locate(),
            runEngine: { location, completion in
                EngineRunner(location: location).run(completion: completion)
            }
        )
    }

    /// A store for an engine location and a way to run it.
    ///
    /// `refreshOnStart` runs the engine at once,  as the app does;  a test passes false and
    /// starts the run itself.
    init(location located: EngineLocation?, runEngine: @escaping EngineRunning, refreshOnStart: Bool = true) {
        location = located
        self.runEngine = runEngine
        engineRoot = located?.root
        commandLine = located?.commandLine ?? ""
        reloadInputsIfChanged()
        rebuild()
        if refreshOnStart { refresh() }
    }

    /// Read the manifest and the registry again,  when either file changed since the last
    /// read,  and rebuild the roster from them.
    ///
    /// The engine reads both files on every run,  so the grid reads them before the run
    /// that will use them:  a seat added to `fleet-seats.json` gets a row on the next run,
    /// not on the next launch.  A file is judged changed by its size and modification
    /// time,  the stamp `LedgerParseCache` keeps for the wake ledger.
    ///
    /// A manifest that is a version this app does not read is refused,  and the refusal is
    /// named in `manifestProblem`.  A refused manifest is not the same as no manifest:
    /// without the reason the operator sees a grid of unknown rows and no idea why.  When a
    /// copy was read before,  that copy is kept beside the problem,  so a bad write does
    /// not take the roster away;  a file that is gone is no manifest,  as at launch.
    ///
    /// Returns whether either file was read.
    @discardableResult
    func reloadInputsIfChanged() -> Bool {
        guard let location else { return false }
        let manifestNow = LedgerParseCache.stamp(of: location.manifestURL)
        let registryNow = LedgerParseCache.stamp(of: location.registryURL)
        let manifestChanged = !inputsRead || manifestNow != manifestStamp
        let registryChanged = !inputsRead || registryNow != registryStamp
        guard manifestChanged || registryChanged else { return false }
        inputsRead = true
        if manifestChanged {
            manifestStamp = manifestNow
            let read = Self.readManifest(at: location.manifestURL)
            // A copy that could not be read leaves the last good copy in place.
            if read.problem == nil || manifest == nil {
                inputsLock.lock()
                manifestSnapshot = read.manifest
                inputsLock.unlock()
                manifest = read.manifest
            }
            manifestProblem = read.problem
        }
        if registryChanged {
            registryStamp = registryNow
            let read = Self.readRegistry(at: location.registryURL)
            if read.problem == nil || registry == nil {
                registry = read.registry
            }
            registryProblem = read.problem
        }
        roster = Self.roster(from: registry, manifest: manifest)
        return true
    }

    /// The manifest at a path,  or why it could not be read.  A file that is not there is
    /// neither:  no manifest and no problem.
    private static func readManifest(at url: URL) -> (manifest: SeatManifest?, problem: String?) {
        guard FileManager.default.fileExists(atPath: url.path) else { return (nil, nil) }
        do {
            let loaded = try FleetDataReader.loadManifest(at: url)
            return (loaded, nil)
        } catch {
            return (nil, describe(error))
        }
    }

    /// The registry at a path,  read the same way the manifest is:  a file that is there
    /// and cannot be read is a problem to name,  not a file that is not there.
    private static func readRegistry(at url: URL) -> (registry: FleetRegistry?, problem: String?) {
        guard FileManager.default.fileExists(atPath: url.path) else { return (nil, nil) }
        do {
            let loaded = try FleetDataReader.loadRegistry(at: url)
            return (loaded, nil)
        } catch {
            return (nil, describe(error, what: "the registry"))
        }
    }

    /// Why a file could not be read,  in the app's own words.
    private static func describe(_ error: Error, what: String = "the manifest") -> String {
        if let version = error as? SeatManifest.ManifestVersionError {
            return version.errorDescription ?? "\(what) is a version this app does not read"
        }
        return "\(what) could not be read:  \(error.localizedDescription)"
    }

    /// The rows of the grid:  the live roster, in registry order.
    ///
    /// The registry holds the order and the manifest holds the membership, so the rows
    /// are the seats the engine can actually check.
    static func roster(from registry: FleetRegistry?, manifest: SeatManifest?) -> [String] {
        let live = registry?.liveTags ?? []
        guard let manifest = manifest else { return live }
        let known = live.filter { manifest.seats[$0] != nil }
        // The manifest still holds a row for the Grok Bot group, which is not a seat, so the
        // fallback leaves it out as the registry's list does.
        return known.isEmpty ? manifest.seats.keys.filter { !NotASeat.contains($0) }.sorted() : known
    }

    /// The cell for one seat and kind.
    func cell(seat: String, kind: ConfigKind) -> GridCell? {
        cellsByKey["\(seat)/\(kind.rawValue)"]
    }

    /// What a cell is about,  for the panel to show.
    ///
    /// Every cell names something:  a rule, and the artifact the rule checks.  The
    /// artifact is a file for most kinds,  and for a credential rule it is a path the
    /// panel refuses to read,  so the panel shows the rule's own facts instead.
    struct CellArtifacts {
        /// The rules behind the cell,  worst first.
        let rules: [CheckResult]
        /// The artifact each rule checks,  in the same order as the rules.
        let views: [ConfigFileView]
        /// Why the pair does not apply,  when a cover says so.
        let coverReason: String?
        /// What the reader found for each rule,  in the same order as the rules,  with the
        /// states `views` folds together (not text,  unreadable,  missing) kept apart and
        /// the words for each.
        var reads: [ConfigFileRead] = []
        /// Why the cell has no rule,  when it has none and no cover.
        var gap: CellGap? = nil

        /// The sentence the panel shows when the cell has no rule to show.
        ///
        /// It is the cover's reason when a cover applies,  and otherwise the true reason
        /// the cell is empty:  "No rule covers this seat and kind." only when the manifest
        /// was read and names none.
        var emptyText: String {
            coverReason ?? gap?.sentence ?? "There is no result for this seat and kind."
        }
    }

    /// The manifest,  read under the lock,  for a caller that may be off the main thread.
    private var currentManifest: SeatManifest? {
        inputsLock.lock()
        defer { inputsLock.unlock() }
        return manifestSnapshot
    }

    /// The citation each rule carries in the manifest,  by rule id.
    ///
    /// The manifest's `source` field is where the directive,  the date or the message
    /// that asked for a rule is recorded.  It is empty on every rule today,  and the
    /// panel says that rather than showing a blank line.
    func ruleSources(seat: String, kind: ConfigKind) -> [String: String] {
        guard let manifest = currentManifest else { return [:] }
        var sources: [String: String] = [:]
        for rule in manifest.rules(seat: seat, kind: kind) {
            if let source = rule.source { sources[rule.id] = source }
        }
        return sources
    }

    /// The artifacts a cell is about,  read for the panel to show.
    ///
    /// The envelope carries no path,  so the manifest is the only place that knows
    /// which file a seat's rule checks,  and the panel reads the file itself.
    ///
    /// It is safe to call off the main thread,  as the panels do from a detached task:  it
    /// reads the manifest under the lock that its re-read takes.
    func artifacts(for cell: GridCell) -> CellArtifacts {
        let targets = currentManifest?.targets(seat: cell.seat, kind: cell.kind) ?? []
        let byRule = Dictionary(
            targets.map { ($0.rule.id, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let reads = cell.rules.map { rule -> ConfigFileRead in
            guard let entry = byRule[rule.ruleId] else {
                return ConfigFileRead(
                    state: .unresolved,
                    view: .unresolved(reason: "The manifest names no path for \(rule.ruleId).")
                )
            }
            return ConfigFileReader.inspect(
                target: entry.target,
                checkType: entry.checkType,
                home: FileManager.default.homeDirectoryForCurrentUser
            )
        }
        return CellArtifacts(
            rules: cell.rules,
            views: reads.map { $0.view },
            coverReason: cell.coverReason,
            reads: reads,
            gap: cell.gap
        )
    }

    /// The engine's proof for every hook of one seat.
    ///
    /// A hook rule proves the hook it registered, by the hook's own name, so the
    /// MiniMax panel can say when that hook was last seen to deny.
    ///
    /// While a run is in flight the results are the last run's,  and the MiniMax panel
    /// says the engine has not answered until it passes `isShowingLastAnswer` along,  so by
    /// default no proof is handed out during a run:  a hook must not read "Proved by"
    /// beside "the engine has not answered".  A caller whose words say the answer is the
    /// last run's passes `includingLastAnswer: true`.
    func hookProofs(seat: String, includingLastAnswer: Bool = false) -> [MiniMaxSurface.HookProof] {
        if isChecking && !includingLastAnswer { return [] }
        return Self.hookProofs(in: results, seat: seat)
    }

    /// The proofs a run's results carry for one seat's hooks, one per hook a rule names.
    ///
    /// A rule may name more than one hook, and each of them is proved by that rule:
    /// taking only the first name would leave every other hook the rule covers reading
    /// "not proved" while the engine had proved it.
    static func hookProofs(in results: [CheckResult], seat: String) -> [MiniMaxSurface.HookProof] {
        results
            .filter { $0.seat == seat && $0.kind == ConfigKind.hooks.rawValue }
            .flatMap { result -> [MiniMaxSurface.HookProof] in
                let names = result.expected["names"]?.stringArray ?? []
                return names.map { name in
                    MiniMaxSurface.HookProof(
                        hookName: name,
                        required: result.proofLevelRequired ?? "L0",
                        achieved: result.proofLevelAchieved,
                        checkedAt: FleetTime.parse(result.checkedAt),
                        state: result.state
                    )
                }
            }
    }

    /// The one engine run in flight, and the lock that guards it.
    private final class RefreshBox {}

    private let refreshLock = NSLock()
    private var refreshBox: RefreshBox?

    /// Whether a refresh is already in flight (single-flight).
    private var refreshInFlight: Bool {
        refreshLock.lock()
        defer { refreshLock.unlock() }
        return refreshBox != nil
    }

    /// Run the engine again. Single-flight: a call while a run is in flight
    /// shares that run instead of starting a second engine, like
    /// `engineAnswer()` above.
    ///
    /// The last answer stays on screen while the run is in flight,  marked as being
    /// checked (`isChecking`,  `GridCell.isBeingChecked`),  because clearing it at the
    /// start painted every cell a red UNKNOWN for the whole run and protected nothing:
    /// `apply` clears every result,  the summary and the clock when a run fails or times
    /// out,  so a run that dies halfway still leaves the grid unknown and never shows the
    /// last good answer as current.
    ///
    /// The manifest and the registry are read again first when either file changed,  so
    /// the roster matches what the engine is about to read.
    func refresh() {
        guard !refreshInFlight else { return }
        reloadInputsIfChanged()
        guard let location = location else {
            results = []
            summary = nil
            generatedAt = nil
            commonCheckedAt = nil
            uncovered = []
            notImplemented = []
            answered = false
            isChecking = false
            lastLandedRunStartedAt = nil
            phase = .failed(
                "no engine checkout was found;  set FLEET_ENGINE_ROOT to a checkout that "
                    + "carries scripts/fleet_config"
            )
            rebuild()
            // No engine is a run that could not be trusted,  and a watcher has to hear that
            // like any other failure:  the drift monitor then clears its count and its alerts,
            // and its safety net waits a whole interval before it asks again.
            onRunFinished?(false)
            return
        }
        let box = RefreshBox()
        refreshLock.lock()
        // A caller on another thread may have started a run since the guard.
        if refreshBox != nil {
            refreshLock.unlock()
            return
        }
        refreshBox = box
        refreshLock.unlock()
        runStartedAt = Date()
        phase = .running
        isChecking = true
        rebuild()
        runEngine(location) { [weak self] run in
            DispatchQueue.main.async {
                guard let self else { return }
                self.refreshLock.lock()
                if self.refreshBox === box { self.refreshBox = nil }
                self.refreshLock.unlock()
                self.apply(run)
            }
        }
    }

    /// Take what the run produced.
    private func apply(_ run: EngineRun) {
        isChecking = false
        lastLandedRunStartedAt = runStartedAt
        runStartedAt = nil
        guard let envelope = run.envelope else {
            results = []
            summary = nil
            generatedAt = nil
            commonCheckedAt = nil
            uncovered = []
            notImplemented = []
            answered = false
            phase = .failed(run.failure ?? "the engine did not answer")
            rebuild()
            // The run could not be trusted,  and a watcher has to hear that:  a run
            // that failed says nothing about the seats.
            onRunFinished?(false)
            return
        }
        results = envelope.results
        summary = envelope.summary
        generatedAt = FleetTime.parse(envelope.generatedAt)
        commonCheckedAt = Self.commonCheckTime(in: results)
        uncovered = envelope.uncoveredSurfaces
        notImplemented = envelope.notImplementedRules
        answered = true
        phase = .loaded
        rebuild()
        onRunFinished?(true)
    }

    /// Whether the cells on screen are the last run's answer while a new run checks them.
    var isShowingLastAnswer: Bool { isChecking && answered }

    /// The line a view shows while a run is in flight,  or nil when none is.
    ///
    /// It says the grid is being checked and,  when the cells hold the last answer,  the
    /// time that answer is from,  on Jay's clock.
    var checkingNote: String? {
        guard isChecking else { return nil }
        guard answered else { return "Checking now.\u{00A0} The engine has not answered yet." }
        guard let generatedAt else {
            return "Checking again now.\u{00A0} These are the answers from the last run."
        }
        return "Checking again now.\u{00A0} These are the answers from the run at \(FleetTime.clock(generatedAt))."
    }

    /// The surfaces the engine names and does not check for one seat,  from the last
    /// trusted run.
    func uncoveredSurfaces(seat: String) -> [UncoveredSurface] {
        uncovered.filter { $0.seat == seat }
    }

    /// The check time the run shares,  which is the one most rules carry.
    ///
    /// The engine checks every rule in one pass,  so the shared time is the time the
    /// run happened.  A rule served from the cache was checked earlier,  and its cell
    /// is the exception the grid calls out.
    static func commonCheckTime(in results: [CheckResult]) -> Date? {
        var counts: [Date: Int] = [:]
        for result in results {
            guard let checkedAt = FleetTime.parse(result.checkedAt) else { continue }
            counts[checkedAt, default: 0] += 1
        }
        return counts.max { lhs, rhs in
            lhs.value == rhs.value ? lhs.key < rhs.key : lhs.value < rhs.value
        }?.key
    }

    /// Rebuild the grid from what is known.
    private func rebuild() {
        let built = GridBuilder.build(
            roster: roster,
            manifest: manifest,
            results: results,
            checking: isChecking,
            answered: answered,
            // A manifest kept beside a problem is the last good copy of a changed file.
            manifestCurrent: manifestProblem == nil
        )
        cells = built
        cellsByKey = Dictionary(uniqueKeysWithValues: built.map { ($0.id, $0) })
    }
}

extension FleetStore: DriftGridReading {}
