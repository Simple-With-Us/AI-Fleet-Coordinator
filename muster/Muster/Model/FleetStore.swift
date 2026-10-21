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

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var roster: [String] = []
    @Published private(set) var cells: [GridCell] = []
    @Published private(set) var summary: EnvelopeSummary?
    @Published private(set) var generatedAt: Date?
    @Published private(set) var engineRoot: URL?
    @Published private(set) var commandLine: String = ""
    /// Why the manifest could not be read,  when it is there and cannot be read.
    ///
    /// It is kept for the whole session,  because the manifest is read once and a
    /// refusal does not go away when the next run succeeds.
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
    private let manifest: SeatManifest?
    private let registry: FleetRegistry?
    private var results: [CheckResult] = []
    private var cellsByKey: [String: GridCell] = [:]

    init() {
        let located = EngineLocator.locate()
        location = located
        engineRoot = located?.root
        commandLine = located?.commandLine ?? ""
        // A manifest that is a version this app does not read is refused,  and the
        // refusal is kept.  A refused manifest is not the same as no manifest:  the
        // roster falls back to the registry's tags,  and without the reason the
        // operator sees a grid of unknown rows and no idea why.
        var loadedManifest: SeatManifest?
        var manifestProblem: String?
        if let located, FileManager.default.fileExists(atPath: located.manifestURL.path) {
            do {
                loadedManifest = try FleetDataReader.loadManifest(at: located.manifestURL)
            } catch {
                manifestProblem = Self.describe(error)
            }
        }
        manifest = loadedManifest
        self.manifestProblem = manifestProblem
        // The registry is read the same way the manifest is:  a file that is there
        // and cannot be read is a problem to name,  not a file that is not there.
        var loadedRegistry: FleetRegistry?
        var registryProblem: String?
        if let located, FileManager.default.fileExists(atPath: located.registryURL.path) {
            do {
                loadedRegistry = try FleetDataReader.loadRegistry(at: located.registryURL)
            } catch {
                registryProblem = Self.describe(error, what: "the registry")
            }
        }
        registry = loadedRegistry
        self.registryProblem = registryProblem
        roster = Self.roster(from: registry, manifest: manifest)
        rebuild()
        refresh()
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
        return known.isEmpty ? Array(manifest.seats.keys).sorted() : known
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
    }

    /// The citation each rule carries in the manifest,  by rule id.
    ///
    /// The manifest's `source` field is where the directive,  the date or the message
    /// that asked for a rule is recorded.  It is empty on every rule today,  and the
    /// panel says that rather than showing a blank line.
    func ruleSources(seat: String, kind: ConfigKind) -> [String: String] {
        guard let manifest else { return [:] }
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
    func artifacts(for cell: GridCell) -> CellArtifacts {
        let targets = manifest?.targets(seat: cell.seat, kind: cell.kind) ?? []
        let byRule = Dictionary(
            targets.map { ($0.rule.id, $0) },
            uniquingKeysWith: { first, _ in first }
        )
        let views = cell.rules.map { rule -> ConfigFileView in
            guard let entry = byRule[rule.ruleId] else {
                return .unresolved(reason: "The manifest names no path for \(rule.ruleId).")
            }
            return ConfigFileReader.read(
                target: entry.target,
                checkType: entry.checkType,
                home: FileManager.default.homeDirectoryForCurrentUser
            )
        }
        return CellArtifacts(rules: cell.rules, views: views, coverReason: cell.coverReason)
    }

    /// The engine's proof for every hook of one seat.
    ///
    /// A hook rule proves the hook it registered, by the hook's own name, so the
    /// MiniMax panel can say when that hook was last seen to deny.
    func hookProofs(seat: String) -> [MiniMaxSurface.HookProof] {
        results
            .filter { $0.seat == seat && $0.kind == ConfigKind.hooks.rawValue }
            .compactMap { result in
                guard
                    let names = result.expected["names"]?.stringArray,
                    let name = names.first
                else { return nil }
                return MiniMaxSurface.HookProof(
                    hookName: name,
                    required: result.proofLevelRequired ?? "L0",
                    achieved: result.proofLevelAchieved,
                    checkedAt: FleetTime.parse(result.checkedAt),
                    state: result.state
                )
            }
    }

    /// Run the engine again.
    ///
    /// The old results go before the run starts, so a run that dies halfway leaves the
    /// grid unknown rather than showing the last good answer.
    func refresh() {
        guard let location = location else {
            results = []
            summary = nil
            generatedAt = nil
            phase = .failed(
                "no engine checkout was found;  set FLEET_ENGINE_ROOT to a checkout that "
                    + "carries scripts/fleet_config"
            )
            rebuild()
            return
        }
        results = []
        // The summary and the clock go with the results.  A run in flight must not
        // leave the previous run's counts and timestamp on the screen,  because the
        // count is what the operator reads as "current".
        summary = nil
        generatedAt = nil
        rebuild()
        phase = .running
        let runner = EngineRunner(location: location)
        runner.run { [weak self] run in
            DispatchQueue.main.async {
                self?.apply(run)
            }
        }
    }

    /// Take what the run produced.
    private func apply(_ run: EngineRun) {
        guard let envelope = run.envelope else {
            results = []
            summary = nil
            generatedAt = nil
            commonCheckedAt = nil
            phase = .failed(run.failure ?? "the engine did not answer")
            rebuild()
            return
        }
        results = envelope.results
        summary = envelope.summary
        generatedAt = FleetTime.parse(envelope.generatedAt)
        commonCheckedAt = Self.commonCheckTime(in: results)
        phase = .loaded
        rebuild()
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
        let built = GridBuilder.build(roster: roster, manifest: manifest, results: results)
        cells = built
        cellsByKey = Dictionary(uniqueKeysWithValues: built.map { ($0.id, $0) })
    }
}
