import XCTest

/// The watch list:  the paths the manifest names,  and nothing else.
///
/// The list is derived and never written down,  so a seat that joins the fleet is
/// watched because the manifest says where its config lives.
final class DriftWatchListTests: XCTestCase {
    /// A home directory of the test's own,  so the test never reads Jay's.
    private var home: URL!

    override func setUpWithError() throws {
        home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-drift-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent(".minimax", isDirectory: true),
            withIntermediateDirectories: true
        )
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    /// A manifest with a file,  a directory,  a credential and a path outside the
    /// user's own directories.
    private let manifestJSON = """
    {
     "schemaVersion": 2,
     "seats": {
      "MM": {
       "platforms": ["minimax"],
       "rules": [
        {
         "id": "mm-hooks-lane-guard",
         "kind": "hooks",
         "platforms": ["minimax"],
         "check": {"type": "hook_registered", "target": "~/.minimax/plugins/fleet-guard/hooks/hooks.json"}
        },
        {
         "id": "mm-skills-fleet",
         "kind": "skills",
         "platforms": ["minimax"],
         "check": {"type": "skills_installed", "target": "~/.minimax/skills"}
        },
        {
         "id": "mm-permissions-zulip-key",
         "kind": "permissions",
         "platforms": ["minimax"],
         "check": {"type": "credential_key_present", "target": "/zulip"}
        }
       ],
       "notApplicable": []
      },
      "CLAUDE": {
       "platforms": ["claude"],
       "rules": [
        {
         "id": "claude-hooks-lane-guard",
         "kind": "hooks",
         "platforms": ["claude"],
         "check": {"type": "hook_registered", "target": "~/.claude/settings.json"}
        },
        {
         "id": "claude-permissions-host-file",
         "kind": "permissions",
         "platforms": ["claude"],
         "check": {"type": "permissions_match", "target": "/etc/hosts"}
        }
       ],
       "notApplicable": []
      }
     }
    }
    """

    private func manifest() throws -> SeatManifest {
        try JSONDecoder().decode(SeatManifest.self, from: Data(manifestJSON.utf8))
    }

    /// The paths the manifest names are the paths that are watched.
    func test_the_watch_list_comes_from_the_manifest() throws {
        let list = DriftWatchListBuilder.build(manifest: try manifest(), home: home)
        let paths = list.targetPaths.map { $0.path }
        XCTAssertTrue(paths.contains(home.appendingPathComponent(".minimax/plugins/fleet-guard/hooks/hooks.json").path))
        XCTAssertTrue(paths.contains(home.appendingPathComponent(".minimax/skills").path))
        XCTAssertTrue(paths.contains(home.appendingPathComponent(".claude/settings.json").path))
        XCTAssertTrue(list.seats.contains("MM"))
        XCTAssertTrue(list.seats.contains("CLAUDE"))
    }

    /// A credential rule is not watched:  the panel refuses to read a credential,
    /// and a watcher that reports one would be a way around the refusal.
    func test_a_credential_rule_is_not_watched() throws {
        let list = DriftWatchListBuilder.build(manifest: try manifest(), home: home)
        XCTAssertFalse(list.targetPaths.map { $0.path }.contains("/zulip"))
    }

    /// A path outside the user's own directories is not watched.
    func test_a_path_outside_the_home_is_not_watched() throws {
        let list = DriftWatchListBuilder.build(manifest: try manifest(), home: home)
        XCTAssertFalse(list.targetPaths.map { $0.path }.contains("/etc/hosts"))
    }

    /// A path that is not there yet is still watched,  and the nearest directory
    /// above it is watched with it.
    func test_a_path_that_is_not_there_yet_is_watched() throws {
        let list = DriftWatchListBuilder.build(manifest: try manifest(), home: home)
        let target = home.appendingPathComponent(".minimax/plugins/fleet-guard/hooks/hooks.json")
        let own = list.watches.filter { $0.target.path == target.path }
        XCTAssertFalse(own.isEmpty, "the missing path itself must be watched")
        // The nearest directory that is there is the seat's own directory,  and it
        // is not the home directory,  so it is watched as well.
        let ancestor = own.first { !$0.rootIsTheTarget }
        XCTAssertEqual(ancestor?.root.path, home.appendingPathComponent(".minimax").path)
    }

    /// The home directory is never watched:  FSEvents watches a directory
    /// recursively,  and the home directory is the largest tree on the Mac.
    func test_the_home_directory_is_never_watched() throws {
        let list = DriftWatchListBuilder.build(manifest: try manifest(), home: home)
        XCTAssertFalse(list.watches.map { $0.root.path }.contains(home.path))
    }

    /// A path is watched once,  however many rules name it.
    func test_a_path_named_twice_is_watched_once() throws {
        let json = """
        {
         "schemaVersion": 2,
         "seats": {
          "MM": {
           "platforms": ["minimax"],
           "rules": [
            {
             "id": "mm-hooks-lane-guard",
             "kind": "hooks",
             "platforms": ["minimax"],
             "check": {"type": "hook_registered", "target": "~/.minimax/mcp.json"}
            },
            {
             "id": "mm-mcp-fleet-servers",
             "kind": "mcp",
             "platforms": ["minimax"],
             "check": {"type": "mcp_servers_registered", "target": "~/.minimax/mcp.json"}
            }
           ],
           "notApplicable": []
          }
         }
        }
        """
        let manifest = try JSONDecoder().decode(SeatManifest.self, from: Data(json.utf8))
        let list = DriftWatchListBuilder.build(manifest: manifest, home: home)
        XCTAssertEqual(list.targetPaths.count, 1)
    }

    /// An event on the path itself matters.
    func test_an_event_on_the_path_matters() throws {
        let watch = DriftWatchList.Watch(
            root: home.appendingPathComponent(".minimax/mcp.json"),
            target: home.appendingPathComponent(".minimax/mcp.json"),
            targetIsDirectory: false,
            rootIsTheTarget: true
        )
        let event = home.appendingPathComponent(".minimax/mcp.json")
        XCTAssertTrue(DriftEventFilter.isRelevant(event: event, watch: watch))
    }

    /// An event on a sibling does not matter:  the watcher must stay quiet about
    /// the rest of the seat's directory.
    func test_an_event_on_a_sibling_does_not_matter() throws {
        let watch = DriftWatchList.Watch(
            root: home.appendingPathComponent(".minimax/mcp.json"),
            target: home.appendingPathComponent(".minimax/mcp.json"),
            targetIsDirectory: false,
            rootIsTheTarget: true
        )
        let event = home.appendingPathComponent(".minimax/other.json")
        XCTAssertFalse(DriftEventFilter.isRelevant(event: event, watch: watch))
    }

    /// An event inside a directory target matters,  because a skills directory is
    /// about what is inside it.
    func test_an_event_inside_a_directory_target_matters() throws {
        let target = home.appendingPathComponent(".minimax/skills")
        let watch = DriftWatchList.Watch(
            root: target,
            target: target,
            targetIsDirectory: true,
            rootIsTheTarget: true
        )
        let event = target.appendingPathComponent("fleet/SKILL.md")
        XCTAssertTrue(DriftEventFilter.isRelevant(event: event, watch: watch))
    }

    /// An event on a directory between the watch's root and the target matters,
    /// because the target's own directory was created or removed.
    func test_an_event_on_a_directory_between_the_root_and_the_target_matters() throws {
        let target = home.appendingPathComponent(".minimax/plugins/fleet-guard/hooks/hooks.json")
        let watch = DriftWatchList.Watch(
            root: home.appendingPathComponent(".minimax"),
            target: target,
            targetIsDirectory: false,
            rootIsTheTarget: false
        )
        let event = home.appendingPathComponent(".minimax/plugins")
        XCTAssertTrue(DriftEventFilter.isRelevant(event: event, watch: watch))
    }

    /// An event on the root itself does not matter:  the root is there,  and its
    /// own metadata changing says nothing about the target.
    func test_an_event_on_the_root_does_not_matter() throws {
        let target = home.appendingPathComponent(".minimax/plugins/fleet-guard/hooks/hooks.json")
        let watch = DriftWatchList.Watch(
            root: home.appendingPathComponent(".minimax"),
            target: target,
            targetIsDirectory: false,
            rootIsTheTarget: false
        )
        let event = home.appendingPathComponent(".minimax")
        XCTAssertFalse(DriftEventFilter.isRelevant(event: event, watch: watch))
    }

    /// An event that arrives under a link's real name matters,  because the file
    /// system reports the real name and the manifest names the link.
    func test_an_event_under_a_link_matches_the_watch_through_the_link() throws {
        let real = home.appendingPathComponent("real")
        try FileManager.default.createDirectory(at: real, withIntermediateDirectories: true)
        let link = home.appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: real)
        let watch = DriftWatchList.Watch(
            root: link.appendingPathComponent("probe.json"),
            target: link.appendingPathComponent("probe.json"),
            targetIsDirectory: false,
            rootIsTheTarget: true
        )
        let event = real.appendingPathComponent("probe.json")
        XCTAssertTrue(DriftEventFilter.isRelevant(event: event, watch: watch))
    }
}

/// The debounce:  a burst of changes is one run.
final class DriftDebounceTests: XCTestCase {
    /// A change starts the clock,  and the run happens when the clock runs out.
    func test_the_run_waits_for_the_window() {
        let scheduler = ManualScheduler()
        var runs = 0
        let debouncer = DriftDebouncer(interval: 1.5, scheduler: scheduler)
        debouncer.signal { runs += 1 }
        XCTAssertEqual(runs, 0, "the run must not happen the moment the change is seen")
        XCTAssertEqual(scheduler.liveCount, 1)
        scheduler.fire()
        XCTAssertEqual(runs, 1)
    }

    /// A second change inside the window does not start a second run.
    func test_a_burst_of_changes_is_one_run() {
        let scheduler = ManualScheduler()
        var runs = 0
        let debouncer = DriftDebouncer(interval: 1.5, scheduler: scheduler)
        debouncer.signal { runs += 1 }
        debouncer.signal { runs += 1 }
        debouncer.signal { runs += 1 }
        XCTAssertEqual(scheduler.liveCount, 1, "the waiting run must be taken back")
        scheduler.fire()
        XCTAssertEqual(runs, 1)
    }

    /// A change after the window starts a new run.
    func test_a_change_after_the_window_starts_another_run() {
        let scheduler = ManualScheduler()
        var runs = 0
        let debouncer = DriftDebouncer(interval: 1.5, scheduler: scheduler)
        debouncer.signal { runs += 1 }
        scheduler.fire()
        debouncer.signal { runs += 1 }
        scheduler.fire()
        XCTAssertEqual(runs, 2)
    }

    /// Taking the waiting work back means it does not run.
    func test_the_waiting_run_can_be_taken_back() {
        let scheduler = ManualScheduler()
        var runs = 0
        let debouncer = DriftDebouncer(interval: 1.5, scheduler: scheduler)
        debouncer.signal { runs += 1 }
        debouncer.cancel()
        scheduler.fire()
        XCTAssertEqual(runs, 0)
    }
}

/// The turns:  a cell that turns red once is one turn,  and a cell that stays red
/// is none.
final class DriftTransitionTests: XCTestCase {
    /// A cell that is not red and becomes red is a turn.
    func test_a_cell_that_becomes_red_is_a_turn() throws {
        let previous = ["MM/hooks": CellState.passed]
        let found = DriftTransitionFinder.transitions(
            previous: previous,
            current: [cell(seat: "MM", kind: .hooks, state: .missing)]
        )
        XCTAssertEqual(found.count, 1)
        XCTAssertEqual(found.first?.seat, "MM")
        XCTAssertEqual(found.first?.kind, "hooks")
        XCTAssertEqual(found.first?.fromState, "PASS")
        XCTAssertEqual(found.first?.toState, "MISSING")
    }

    /// A cell that stays red is not a turn:  the owner has been told already.
    func test_a_cell_that_stays_red_is_not_a_turn() throws {
        let previous = ["MM/hooks": CellState.missing]
        let found = DriftTransitionFinder.transitions(
            previous: previous,
            current: [cell(seat: "MM", kind: .hooks, state: .missing)]
        )
        XCTAssertTrue(found.isEmpty)
    }

    /// A red cell that changes from one red state to another is not a turn.
    func test_a_red_cell_that_stays_red_in_another_state_is_not_a_turn() throws {
        let previous = ["MM/hooks": CellState.missing]
        let found = DriftTransitionFinder.transitions(
            previous: previous,
            current: [cell(seat: "MM", kind: .hooks, state: .unknown)]
        )
        XCTAssertTrue(found.isEmpty)
    }

    /// A cell that recovers and turns red again is a turn again.
    func test_a_cell_that_recovers_and_turns_red_again_is_a_turn() throws {
        let cell = cell(seat: "MM", kind: .hooks, state: .missing)
        let first = DriftTransitionFinder.transitions(
            previous: ["MM/hooks": CellState.passed],
            current: [cell]
        )
        let second = DriftTransitionFinder.transitions(
            previous: ["MM/hooks": CellState.passed],
            current: [cell]
        )
        XCTAssertEqual(first.count, 1)
        XCTAssertEqual(second.count, 1, "a recovery and a second turn is a second turn")
    }

    /// A cell that is not red is not a turn.
    func test_a_cell_that_stays_green_is_not_a_turn() throws {
        let previous = ["MM/hooks": CellState.passed]
        let found = DriftTransitionFinder.transitions(
            previous: previous,
            current: [cell(seat: "MM", kind: .hooks, state: .passed)]
        )
        XCTAssertTrue(found.isEmpty)
    }

    /// The turn names the check that turned red.
    func test_the_turn_names_the_check() throws {
        let rule = try result(ruleID: "mm-hooks-lane-guard", state: "MISSING")
        let cell = GridCell(
            seat: "MM",
            kind: .hooks,
            state: .missing,
            rules: [rule],
            coverReason: nil
        )
        let found = DriftTransitionFinder.transitions(
            previous: ["MM/hooks": CellState.passed],
            current: [cell]
        )
        XCTAssertEqual(found.first?.ruleID, "mm-hooks-lane-guard")
        XCTAssertEqual(found.first?.checkText, "mm-hooks-lane-guard")
    }

    /// A cell with no rule behind it is still red,  and it says so.
    func test_a_cell_with_no_rule_is_red_and_says_so() throws {
        let cell = GridCell(
            seat: "MM",
            kind: .hooks,
            state: .unknown,
            rules: [],
            coverReason: nil
        )
        let found = DriftTransitionFinder.transitions(
            previous: ["MM/hooks": CellState.passed],
            current: [cell]
        )
        XCTAssertEqual(found.count, 1)
        XCTAssertEqual(found.first?.checkText, "no rule covers this pair")
    }

    /// The two states the grid paints red are the two the count counts.
    func test_the_red_states_are_the_ones_the_grid_paints_red() {
        XCTAssertTrue(CellState.missing.isRed)
        XCTAssertTrue(CellState.unknown.isRed)
        XCTAssertFalse(CellState.drift.isRed)
        XCTAssertFalse(CellState.passed.isRed)
        XCTAssertFalse(CellState.unproven.isRed)
        XCTAssertFalse(CellState.unsupported.isRed)
        XCTAssertFalse(CellState.covered.isRed)
    }
}

/// The monitor:  the count,  the notice,  the history and the quiet.
@MainActor
final class DriftMonitorTests: XCTestCase {
    private var home: URL!
    private var historyURL: URL!

    override func setUpWithError() throws {
        home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-monitor-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        historyURL = home.appendingPathComponent("drift-history.json")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: home)
    }

    /// A monitor with a grid,  a watcher and a scheduler the test drives.
    private func makeMonitor(
        grid: FakeGrid = FakeGrid(),
        watcher: FakeWatcher = FakeWatcher(),
        notifier: ApplyNotifying = DriftRecordingNotifier(),
        scheduler: ManualScheduler = ManualScheduler()
    ) -> DriftMonitor {
        DriftMonitor(
            grid: grid,
            notifier: notifier,
            history: DriftHistoryStore(url: historyURL),
            watcher: watcher,
            scheduler: scheduler,
            ticker: ManualTicker(),
            home: home
        )
    }

    /// The first run is a reading and not a change:  the app has nothing to
    /// compare it with,  and a notice for every cell would be a notice storm.
    func test_the_first_run_is_a_reading_and_not_a_change() async throws {
        let grid = FakeGrid()
        let notifier = DriftRecordingNotifier()
        let monitor = makeMonitor(grid: grid, notifier: notifier)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertTrue(monitor.transitions.isEmpty)
        XCTAssertEqual(notifier.posts.count, 0)
        XCTAssertEqual(monitor.redCount, 1)
    }

    /// A cell that turns red is one notice and one record.
    func test_a_cell_that_turns_red_is_one_notice_and_one_record() async throws {
        let grid = FakeGrid()
        let notifier = DriftRecordingNotifier()
        let monitor = makeMonitor(grid: grid, notifier: notifier)
        monitor.start()
        grid.finish(cells: [cellWithRule(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cellWithRule(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertEqual(monitor.transitions.count, 1)
        XCTAssertEqual(notifier.posts.count, 1)
        let body = notifier.bodies.first ?? ""
        XCTAssertTrue(body.contains("MM"), "the notice must name the seat")
        XCTAssertTrue(body.contains("mm-hooks-lane-guard"), "the notice must name the check")
    }

    /// A cell that stays red says nothing again.
    func test_a_cell_that_stays_red_does_not_say_again() async throws {
        let grid = FakeGrid()
        let notifier = DriftRecordingNotifier()
        let monitor = makeMonitor(grid: grid, notifier: notifier)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertEqual(monitor.transitions.count, 1)
        XCTAssertEqual(notifier.posts.count, 1)
    }

    /// A cell that recovers and turns red again says so again.
    func test_a_cell_that_recovers_and_turns_red_again_says_again() async throws {
        let grid = FakeGrid()
        let notifier = DriftRecordingNotifier()
        let monitor = makeMonitor(grid: grid, notifier: notifier)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertEqual(monitor.transitions.count, 2)
        XCTAssertEqual(notifier.posts.count, 2)
    }

    /// A run that could not be trusted says nothing,  and the count is unknown
    /// rather than zero:  a count of zero would be a claim the app cannot make.
    func test_a_run_that_could_not_be_trusted_says_nothing() async throws {
        let grid = FakeGrid()
        let notifier = DriftRecordingNotifier()
        let monitor = makeMonitor(grid: grid, notifier: notifier)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [], trusted: false)
        await settle()
        XCTAssertTrue(monitor.transitions.isEmpty)
        XCTAssertEqual(notifier.posts.count, 0)
        XCTAssertNil(monitor.redCount)
        XCTAssertFalse(monitor.lastRunWasTrusted ?? true)
    }

    /// A run that could not be trusted does not lower the count to zero,  and the
    /// baseline stands,  so the next trusted run still knows what was red.
    func test_a_failed_run_keeps_the_baseline() async throws {
        let grid = FakeGrid()
        let monitor = makeMonitor(grid: grid)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        grid.finish(cells: [], trusted: false)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertTrue(monitor.transitions.isEmpty, "the cell was already red before the failed run")
    }

    /// The count is the number of red cells the grid paints red.
    func test_the_count_is_the_number_of_red_cells() async throws {
        let grid = FakeGrid()
        let monitor = makeMonitor(grid: grid)
        monitor.start()
        grid.finish(
            cells: [
                cell(seat: "MM", kind: .hooks, state: .missing),
                cell(seat: "MM", kind: .mcp, state: .unknown),
                cell(seat: "CODEX", kind: .mcp, state: .passed),
                cell(seat: "CODEX", kind: .skills, state: .drift),
            ],
            trusted: true
        )
        await settle()
        XCTAssertEqual(monitor.redCount, 2)
        XCTAssertEqual(monitor.redCells.count, 2)
    }

    /// The count is unknown before any run,  and never zero.
    func test_the_count_is_unknown_before_the_first_run() {
        let monitor = makeMonitor()
        XCTAssertNil(monitor.redCount)
    }

    /// The quiet path:  nothing changed,  so nothing runs and nothing is read.
    func test_the_quiet_path_runs_nothing_and_reads_nothing() async throws {
        let grid = FakeGrid()
        let watcher = FakeWatcher()
        let scheduler = ManualScheduler()
        var derivations = 0
        let monitor = DriftMonitor(
            grid: grid,
            notifier: DriftRecordingNotifier(),
            history: DriftHistoryStore(url: historyURL),
            watcher: watcher,
            scheduler: scheduler,
            ticker: ManualTicker(),
            home: home,
            buildWatchList: { _, _ in
                derivations += 1
                return DriftWatchList(watches: [], targetPaths: [], seats: [])
            }
        )
        monitor.start()
        XCTAssertEqual(derivations, 1, "the watch list is built once,  when the app starts")
        XCTAssertEqual(watcher.startCount, 1)
        // Nothing happens:  no change,  no run,  and no reading of any file.
        await settle()
        scheduler.fireAll()
        await settle()
        XCTAssertEqual(grid.refreshCount, 0, "the quiet path must not run the engine")
        XCTAssertEqual(derivations, 1, "the quiet path must not look at the file system")
        XCTAssertEqual(watcher.startCount, 1)
    }

    /// A change starts the debounce,  and the run happens when the window ends.
    func test_a_change_starts_a_run_after_the_window() async throws {
        let grid = FakeGrid()
        let watcher = FakeWatcher()
        let scheduler = ManualScheduler()
        let monitor = makeMonitor(grid: grid, watcher: watcher, scheduler: scheduler)
        monitor.start()
        watcher.fire()
        await settle()
        XCTAssertEqual(grid.refreshCount, 0, "the run waits for the window")
        scheduler.fireAll()
        await settle()
        XCTAssertEqual(grid.refreshCount, 1)
    }

    /// The safety net is minutes,  not seconds.
    func test_the_safety_net_is_slow() {
        let ticker = ManualTicker()
        let monitor = DriftMonitor(
            grid: FakeGrid(),
            notifier: DriftRecordingNotifier(),
            history: DriftHistoryStore(url: historyURL),
            watcher: FakeWatcher(),
            scheduler: ManualScheduler(),
            ticker: ticker,
            home: home
        )
        monitor.start()
        XCTAssertEqual(ticker.interval, DriftMonitor.safetyNetInterval)
        XCTAssertGreaterThanOrEqual(ticker.interval ?? 0, 60)
    }

    /// The history is kept on disk,  and it survives a restart.
    func test_the_history_survives_a_restart() async throws {
        let grid = FakeGrid()
        let monitor = makeMonitor(grid: grid)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertEqual(monitor.transitions.count, 1)

        // A second monitor reads what the first one wrote.
        let second = makeMonitor(grid: FakeGrid())
        XCTAssertEqual(second.transitions.count, 1)
        XCTAssertEqual(second.transitions.first?.seat, "MM")
    }

    /// The history is cleared when the owner clears it.
    func test_the_history_can_be_cleared() async throws {
        let grid = FakeGrid()
        let monitor = makeMonitor(grid: grid)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        monitor.clearHistory()
        XCTAssertTrue(monitor.transitions.isEmpty)
        XCTAssertTrue(DriftHistoryStore(url: historyURL).load().isEmpty)
    }

    /// The history is written even when the notice cannot be sent:  a notice the
    /// system refuses must not cost the record.
    func test_the_record_survives_a_notice_that_cannot_be_sent() async throws {
        let grid = FakeGrid()
        let monitor = makeMonitor(grid: grid, notifier: RefusingNotifier())
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertEqual(monitor.transitions.count, 1)
        XCTAssertEqual(DriftHistoryStore(url: historyURL).load().count, 1)
    }

    /// The turns are newest first.
    func test_the_turns_are_newest_first() async throws {
        let grid = FakeGrid()
        let monitor = makeMonitor(grid: grid)
        monitor.start()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .passed)], trusted: true)
        await settle()
        grid.finish(cells: [cell(seat: "MM", kind: .hooks, state: .missing)], trusted: true)
        await settle()
        XCTAssertEqual(monitor.transitions.count, 2)
        let first = monitor.transitions[0].at
        let second = monitor.transitions[1].at
        XCTAssertGreaterThanOrEqual(first, second)
    }
}

/// The history on disk.
final class DriftHistoryTests: XCTestCase {
    private var url: URL!

    override func setUpWithError() throws {
        url = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-history-\(UUID().uuidString).json")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: url)
    }

    /// A history that is written and read back is the same history.
    func test_the_history_round_trips() throws {
        let store = DriftHistoryStore(url: url)
        let transitions = [
            DriftTransition(
                id: UUID(),
                at: Date(timeIntervalSince1970: 1_700_000_000),
                seat: "MM",
                kind: "hooks",
                ruleID: "mm-hooks-lane-guard",
                fromState: "PASS",
                toState: "MISSING"
            ),
        ]
        XCTAssertTrue(store.save(transitions))
        let read = store.load()
        XCTAssertEqual(read.count, 1)
        XCTAssertEqual(read.first?.seat, "MM")
        XCTAssertEqual(read.first?.kind, "hooks")
        XCTAssertEqual(read.first?.ruleID, "mm-hooks-lane-guard")
        XCTAssertEqual(read.first?.fromState, "PASS")
        XCTAssertEqual(read.first?.toState, "MISSING")
        XCTAssertEqual(
            read.first?.at.timeIntervalSince1970 ?? 0,
            transitions[0].at.timeIntervalSince1970,
            accuracy: 1
        )
    }

    /// A history that is not there reads as no history,  never as a wrong one.
    func test_a_missing_history_reads_as_empty() {
        XCTAssertTrue(DriftHistoryStore(url: url).load().isEmpty)
    }

    /// A history this app cannot read reads as no history.
    func test_a_history_that_cannot_be_read_reads_as_empty() throws {
        try "{not json".write(to: url, atomically: true, encoding: .utf8)
        XCTAssertTrue(DriftHistoryStore(url: url).load().isEmpty)
    }

    /// A history of another version reads as no history.
    func test_a_history_of_another_version_reads_as_empty() throws {
        let json = """
        {"schemaVersion": 99, "transitions": []}
        """
        try json.write(to: url, atomically: true, encoding: .utf8)
        XCTAssertTrue(DriftHistoryStore(url: url).load().isEmpty)
    }

    /// Clearing the history empties the file.
    func test_clearing_the_history_empties_the_file() throws {
        let store = DriftHistoryStore(url: url)
        store.save([
            DriftTransition(
                id: UUID(),
                at: Date(),
                seat: "MM",
                kind: "hooks",
                ruleID: nil,
                fromState: nil,
                toState: "MISSING"
            ),
        ])
        XCTAssertEqual(store.load().count, 1)
        XCTAssertTrue(store.clear())
        XCTAssertTrue(store.load().isEmpty)
    }
}

// MARK: - The doubles

/// The live check:  the real watcher,  the real file system events and the real
/// debounce,  end to end.
///
/// It waits on the file system,  so it is skipped unless `MUSTER_DRIFT_LIVE` is
/// set:  a build machine under load can be slow,  and a test that is slow is a
/// test nobody runs.  The run that proves it is:
///
/// ```sh
/// MUSTER_DRIFT_LIVE=1 xcodebuild -project Muster.xcodeproj -scheme Muster \
///   -destination 'platform=macOS' test
/// ```
@MainActor
final class DriftLiveWatcherTests: XCTestCase {
    func test_a_change_under_a_watched_path_reaches_the_monitor() async throws {
        guard ProcessInfo.processInfo.environment["MUSTER_DRIFT_LIVE"] == "1" else {
            throw XCTSkip("set MUSTER_DRIFT_LIVE=1 to run the live watcher check")
        }
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-live-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let probe = directory.appendingPathComponent("probe.json")
        try "{}".write(to: probe, atomically: true, encoding: .utf8)

        let watch = DriftWatchList.Watch(
            root: probe,
            target: probe,
            targetIsDirectory: false,
            rootIsTheTarget: true
        )
        let list = DriftWatchList(watches: [watch], targetPaths: [probe], seats: ["PROBE"])

        let grid = LiveGrid(probe: probe)
        let notifier = DriftRecordingNotifier()
        let historyURL = directory.appendingPathComponent("drift-history.json")
        let monitor = DriftMonitor(
            grid: grid,
            notifier: notifier,
            history: DriftHistoryStore(url: historyURL),
            watcher: FSEventsDriftWatcher(),
            scheduler: DispatchDriftScheduler(),
            ticker: ManualTicker(),
            home: directory,
            buildWatchList: { _, _ in list }
        )
        monitor.start()

        // The first run is the reading:  the file is there,  so the cell is green.
        grid.refresh()
        await settle()
        XCTAssertEqual(grid.refreshCount, 1)
        XCTAssertTrue(monitor.transitions.isEmpty)

        // The file goes away,  and the watcher must hear it.
        try FileManager.default.removeItem(at: probe)
        await fulfillment(of: [grid.refreshed, notifier.notified], timeout: 20)

        XCTAssertGreaterThanOrEqual(grid.refreshCount, 2, "the engine must run again")
        XCTAssertEqual(monitor.transitions.count, 1, "the cell that turned red must be recorded")
        XCTAssertEqual(monitor.transitions.first?.seat, "PROBE")
        XCTAssertEqual(monitor.transitions.first?.toState, "MISSING")
        XCTAssertEqual(notifier.posts.count, 1, "the notice must be posted once")
        XCTAssertEqual(DriftHistoryStore(url: historyURL).load().count, 1)
    }
}

/// A grid that answers a refresh the way the engine would:  the cell is red when
/// the watched file is gone.
@MainActor
final class LiveGrid: DriftGridReading {
    var cells: [GridCell] = []
    var manifest: SeatManifest? = nil
    var isRunning: Bool = false
    var onRunFinished: ((Bool) -> Void)?
    /// How many times the engine was asked to run.
    private(set) var refreshCount: Int = 0
    /// The file the cell is about.
    let probe: URL
    /// Fulfilled when the engine has run again after the change.
    let refreshed = XCTestExpectation(description: "the engine ran again")

    init(probe: URL) {
        self.probe = probe
    }

    func refresh() {
        refreshCount += 1
        let exists = FileManager.default.fileExists(atPath: probe.path)
        cells = [
            GridCell(
                seat: "PROBE",
                kind: .hooks,
                state: exists ? .passed : .missing,
                rules: [],
                coverReason: nil
            )
        ]
        onRunFinished?(true)
        if refreshCount >= 2 { refreshed.fulfill() }
    }
}

// MARK: - The doubles

/// A grid the test drives by hand.
final class FakeGrid: DriftGridReading {
    var cells: [GridCell] = []
    var manifest: SeatManifest? = nil
    var isRunning: Bool = false
    var onRunFinished: ((Bool) -> Void)?
    /// How many times the engine was asked to run.
    private(set) var refreshCount: Int = 0

    func refresh() {
        refreshCount += 1
    }

    /// A run landed.
    func finish(cells: [GridCell], trusted: Bool) {
        self.cells = cells
        onRunFinished?(trusted)
    }
}

/// A watcher that remembers what it was asked to watch,  and can report a change.
final class FakeWatcher: DriftWatching {
    /// How many times the watches were armed.
    private(set) var startCount: Int = 0
    /// The list the watcher was given.
    private(set) var list: DriftWatchList?
    private var onChange: ([URL]) -> Void = { _ in }

    func start(_ list: DriftWatchList, onChange: @escaping ([URL]) -> Void) {
        startCount += 1
        self.list = list
        self.onChange = onChange
    }

    func stop() {}

    /// A change happened.
    func fire() {
        onChange([URL(fileURLWithPath: "/tmp/whatever")])
    }
}

/// A scheduler the test drives by hand.
final class ManualScheduler: DriftScheduling {
    /// The work that is waiting.
    private(set) var pending: [ManualScheduledWork] = []

    /// How many jobs are waiting and have not been taken back.
    var liveCount: Int { pending.filter { !$0.isCancelled }.count }

    func schedule(after delay: TimeInterval, work: @escaping () -> Void) -> DriftScheduledWork {
        let item = ManualScheduledWork(work: work)
        pending.append(item)
        return item
    }

    /// Run everything that is waiting and has not been taken back.
    func fireAll() {
        let work = pending
        pending.removeAll()
        for item in work where !item.isCancelled { item.work() }
    }

    /// Run everything that is waiting.
    func fire() {
        fireAll()
    }
}

/// A waiting job on the manual scheduler.
final class ManualScheduledWork: DriftScheduledWork {
    /// The work itself.
    let work: () -> Void
    /// Whether the work was taken back.
    private(set) var isCancelled = false

    init(work: @escaping () -> Void) {
        self.work = work
    }

    func cancel() {
        isCancelled = true
    }
}

/// A ticker the test drives by hand.
final class ManualTicker: DriftTicking {
    /// The interval the ticker was given.
    private(set) var interval: TimeInterval?
    private var work: (() -> Void)?

    func start(interval: TimeInterval, work: @escaping () -> Void) {
        self.interval = interval
        self.work = work
    }

    func stop() {
        work = nil
    }

    /// The timer goes off.
    func fire() {
        work?()
    }
}

/// A notifier that remembers what it was asked to post.
final class DriftRecordingNotifier: ApplyNotifying, @unchecked Sendable {
    private let lock = NSLock()
    private var posted: [(title: String, body: String)] = []
    /// Whether the expectation has been fulfilled.
    private var hasNotified = false

    /// Fulfilled when a notice is posted.
    let notified = XCTestExpectation(description: "the notice was posted")

    /// The bodies that were posted.
    var bodies: [String] {
        lock.lock()
        defer { lock.unlock() }
        return posted.map { $0.body }
    }

    /// How many notices were posted.
    var posts: [(title: String, body: String)] {
        lock.lock()
        defer { lock.unlock() }
        return posted
    }

    func post(title: String, body: String) async {
        lock.lock()
        posted.append((title: title, body: body))
        let first = !hasNotified
        hasNotified = true
        lock.unlock()
        // The expectation is only fulfilled once:  a test that posts twice must not
        // break the framework's own bookkeeping.
        if first { notified.fulfill() }
    }
}

/// A notifier that cannot post,  the way an app without permission cannot.
final class RefusingNotifier: ApplyNotifying, @unchecked Sendable {
    func post(title: String, body: String) async {}
}

// MARK: - The builders

/// One cell,  for the tests to place on the grid.
private func cell(seat: String, kind: ConfigKind, state: CellState) -> GridCell {
    GridCell(seat: seat, kind: kind, state: state, rules: [], coverReason: nil)
}

/// One cell with the rule behind it,  so the notice can name the check.
private func cellWithRule(
    seat: String,
    kind: ConfigKind,
    state: CellState
) -> GridCell {
    let rule = try? result(ruleID: "mm-hooks-lane-guard", state: state.rawValue)
    return GridCell(
        seat: seat,
        kind: kind,
        state: state,
        rules: rule.map { [$0] } ?? [],
        coverReason: nil
    )
}

/// One rule,  read from the envelope's own shape.
private func result(ruleID: String, state: String) throws -> CheckResult {
    let json = """
    {
     "ruleId": "\(ruleID)",
     "seat": "MM",
     "platform": "minimax",
     "kind": "hooks",
     "state": "\(state)",
     "reason": "",
     "reasonCode": "ok",
     "expected": {},
     "observed": {},
     "proofLevelRequired": "L0",
     "proofLevelAchieved": null,
     "collector": "mac",
     "checkType": "hook_registered",
     "checkedAt": "2026-10-19T14:30:00Z",
     "expiresAt": null,
     "ttlSeconds": 900,
     "stale": false,
     "failing": true
    }
    """
    return try JSONDecoder().decode(CheckResult.self, from: Data(json.utf8))
}

/// Lets the monitor's own hop to the main actor finish.
@MainActor
private func settle() async {
    for _ in 0..<4 {
        await Task.yield()
    }
}
