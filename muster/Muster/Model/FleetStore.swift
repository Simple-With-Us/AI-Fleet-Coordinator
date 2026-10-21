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

    private let location: EngineLocation?
    private let manifest: SeatManifest?
    private let registry: FleetRegistry?
    private var results: [CheckResult] = []
    private var cellsByKey: [String: GridCell] = [:]

    init() {
        let located = EngineLocator.locate()
        location = located
        engineRoot = located?.root
        commandLine = located?.commandLine ?? ""
        manifest = located.flatMap { try? FleetDataReader.loadManifest(at: $0.manifestURL) }
        registry = located.flatMap { try? FleetDataReader.loadRegistry(at: $0.registryURL) }
        roster = Self.roster(from: registry, manifest: manifest)
        rebuild()
        refresh()
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
            phase = .failed(run.failure ?? "the engine did not answer")
            rebuild()
            return
        }
        results = envelope.results
        summary = envelope.summary
        generatedAt = FleetTime.parse(envelope.generatedAt)
        phase = .loaded
        rebuild()
    }

    /// Rebuild the grid from what is known.
    private func rebuild() {
        let built = GridBuilder.build(roster: roster, manifest: manifest, results: results)
        cells = built
        cellsByKey = Dictionary(uniqueKeysWithValues: built.map { ($0.id, $0) })
    }
}
