import XCTest
/// The MiniMax surface reader:  what it reads, and what it refuses to read.
final class MiniMaxSurfaceTests: XCTestCase {
    /// A home directory holding a MiniMax surface, built in a temporary directory.
    private func makeHome() throws -> URL {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-mm-\(UUID().uuidString)", isDirectory: true)
        let root = home.appendingPathComponent(".minimax", isDirectory: true)
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("plugins/fleet-guard/hooks", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("agents/mavis/memory", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("agents/worker", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("agents/spawned", isDirectory: true),
            withIntermediateDirectories: true
        )
        try """
        {
         "hooks": {
          "PreToolUse": [
           {
            "matcher": "Bash",
            "hooks": [
             {
              "type": "command",
              "command": "\(root.path)/plugins/fleet-guard/hooks/fleet-guard-hook --format claude",
              "timeout": 10
             }
            ]
           }
          ]
         }
        }
        """.write(
            to: root.appendingPathComponent("plugins/fleet-guard/hooks/hooks.json"),
            atomically: true,
            encoding: .utf8
        )
        try """
        schemaVersion: 3
        bindings: {}
        messageFilters: {}
        connections:
          connection_abc:
            connectionId: connection_abc
            agentName: mavis
            channel: feishu
            projectKey: default
        """.write(
            to: root.appendingPathComponent("channel-bindings.yaml"),
            atomically: true,
            encoding: .utf8
        )
        try """
        schemaVersion: 2
        rules: []
        defaultRoute:
          feishu:
            agentId: mavis
          telegram:
            agentId: gone-bot
          lark:
            agentId: worker
        """.write(
            to: root.appendingPathComponent("channel-routes.yaml"),
            atomically: true,
            encoding: .utf8
        )
        try "schemaVersion: 1\nowners: {}\n".write(
            to: root.appendingPathComponent("channel-owner.yaml"),
            atomically: true,
            encoding: .utf8
        )
        try """
        logLevel: info
        defaultModel: minimax/MiniMax-M3.1-Flash
        permissionMode: bypassPermissions
        apiKey: sk-secret-value
        """.write(
            to: root.appendingPathComponent("config.yaml"),
            atomically: true,
            encoding: .utf8
        )
        try "the seat prompt\n".write(
            to: root.appendingPathComponent("agents/worker/agent.md"),
            atomically: true,
            encoding: .utf8
        )
        try "id\n".write(
            to: root.appendingPathComponent("agents/spawned/.agent-instance-id"),
            atomically: true,
            encoding: .utf8
        )
        return home
    }

    /// The `.minimax` folder of a home built by `makeHome`.
    private func root(_ home: URL) -> URL {
        home.appendingPathComponent(".minimax", isDirectory: true)
    }

    /// Make a path unreadable, and skip when this user can read it anyway (root).
    ///
    /// The teardown gives the permission back and then removes the home, because a
    /// folder nobody can list cannot be removed by the test's own cleanup.
    private func makeUnreadable(_ url: URL, in home: URL) throws {
        try FileManager.default.setAttributes([.posixPermissions: 0], ofItemAtPath: url.path)
        addTeardownBlock {
            try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
            try? FileManager.default.removeItem(at: home)
        }
        try XCTSkipIf(
            FileManager.default.isReadableFile(atPath: url.path),
            "this user can read a mode 000 path, so an unreadable path cannot be made"
        )
    }

    /// A passing engine row for every config kind.
    private func allPassing() -> [(kind: ConfigKind, state: CellState)] {
        ConfigKind.columnOrder.map { (kind: $0, state: CellState.passed) }
    }

    /// One engine result, shaped the way the envelope carries it.
    private func result(
        seat: String = "MM",
        kind: String = "hooks",
        names: [String],
        state: String = "PASS"
    ) -> CheckResult {
        CheckResult(
            ruleId: "rule-\(names.joined(separator: "-"))",
            seat: seat,
            platform: "minimax",
            kind: kind,
            state: state,
            reason: "ok",
            reasonCode: "ok",
            expected: ["names": .array(names.map { .string($0) })],
            observed: [:],
            proofLevelRequired: "L1",
            proofLevelAchieved: state == "PASS" ? "L1" : nil,
            collector: "mac",
            checkType: "hook_registered",
            checkedAt: "2023-11-14T22:13:20Z",
            expiresAt: nil,
            ttlSeconds: 900,
            stale: false,
            failing: state != "PASS"
        )
    }

    // MARK: - what was there before

    /// The child count reads the direct children of a key, and no deeper.
    ///
    /// The connection entry carries four fields of its own, so a count that reads
    /// every nested key would report five connections where there is one.
    func test_the_connection_count_reads_direct_children_only() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let bindings = surface.channels.first { $0.file == "channel-bindings.yaml" }
        XCTAssertEqual(bindings?.detail, "1 connections, 0 message filters")
    }

    /// The panel names the fleet channel files, and no platform file for a platform the
    /// seat is not linked to.
    ///
    /// Lark is the only IM platform this seat answers on (owner, Oct 24, 2026).  A retired
    /// platform is not shown as "not on the Mac" either, because a panel that lists it
    /// invites the reader to go looking for a connection that was never there.
    func test_the_panel_names_no_platform_file_for_a_retired_platform() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let files = surface.channels.map { $0.file }
        XCTAssertEqual(
            files,
            ["channel-bindings.yaml", "channel-routes.yaml", "channel-owner.yaml"]
        )
        XCTAssertFalse(files.contains { $0.contains("telegram") })
        XCTAssertFalse(files.contains { $0.contains("wechat") })
    }

    /// The seat's own runtime agent is the one holding the durable memory.
    func test_the_seat_runtime_agent_is_the_one_holding_the_memory() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        XCTAssertEqual(MiniMaxReader.seatRuntimeAgentName(home: home), "mavis")
    }

    /// The agent roster counts the definition's own files, not the memory tree.
    func test_the_agent_roster_counts_the_definition_files() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let seat = surface.agents.first { $0.name == "mavis" }
        XCTAssertEqual(seat?.ownsSeatMemory, true)
        XCTAssertEqual(seat?.files.count, 0)
        let worker = surface.agents.first { $0.name == "worker" }
        XCTAssertEqual(worker?.hasPrompt, true)
        XCTAssertEqual(worker?.files, ["agent.md"])
        let spawned = surface.agents.first { $0.name == "spawned" }
        XCTAssertEqual(spawned?.ownsSeatMemory, false)
    }

    /// The credential in `config.yaml` never reaches the panel.
    func test_the_credential_key_is_never_read() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let keys = surface.settings.map { $0.key }
        XCTAssertTrue(keys.contains("permissionMode"))
        XCTAssertFalse(keys.contains("apiKey"))
        for setting in surface.settings {
            XCTAssertFalse(setting.value.contains("sk-secret-value"))
        }
        XCTAssertEqual(surface.settingsFile, .read)
    }

    /// The hook panel carries the engine's proof for the hook.
    ///
    /// This is the column the fleet has nowhere else:  a hook that is registered and a
    /// hook that has been seen to deny are different states.
    func test_the_hook_carries_the_engine_proof() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let proof = MiniMaxSurface.HookProof(
            hookName: "fleet-guard-hook",
            required: "L1",
            achieved: "L1",
            checkedAt: Date(timeIntervalSince1970: 1_700_000_000),
            state: "PASS"
        )
        let surface = MiniMaxReader.read(
            home: home,
            rosterSeats: ["MM"],
            proofs: [proof]
        )
        let hook = surface.hooks.first
        XCTAssertEqual(hook?.plugin, "fleet-guard")
        XCTAssertEqual(hook?.event, "PreToolUse")
        XCTAssertEqual(hook?.matcher, "Bash")
        XCTAssertEqual(hook?.hookId, "fleet-guard-hook")
        XCTAssertEqual(hook?.proof?.achieved, "L1")
        XCTAssertEqual(hook?.proof?.state, "PASS")
    }

    /// A hook with no proof is unproved, never fine.
    func test_a_hook_with_no_proof_carries_none() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertNil(surface.hooks.first?.proof)
        XCTAssertTrue(MiniMaxCopy.needsLook(surface.hooks.first?.proof))
        XCTAssertEqual(
            MiniMaxCopy.proofText(nil),
            "Not proved: the engine has no rule for this hook."
        )
    }

    // MARK: - CONTROLS-37:  routes are on the roster, never "live"

    /// A route to the seat's runtime agent names the seat, and says "on the roster".
    func test_a_route_to_the_seat_runtime_agent_names_the_seat_on_the_roster() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(
            home: home,
            rosterSeats: ["MM"],
            seatAliases: ["mavis": "MM"]
        )
        let routes = surface.channels.flatMap { $0.routes }
        let feishu = routes.first { $0.channel == "feishu" }
        XCTAssertEqual(feishu?.agentId, "mavis")
        XCTAssertEqual(feishu?.target, .fleetSeat("MM"))
        let text = MiniMaxCopy.routeTargetText(.fleetSeat("MM"))
        XCTAssertEqual(text, "seat MM, on the roster")
        XCTAssertFalse(text.contains("live"))
    }

    /// A route to an agent nothing on the Mac knows is unknown, and says so.
    func test_a_route_to_an_unknown_agent_is_unknown() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(
            home: home,
            rosterSeats: ["MM"],
            seatAliases: ["mavis": "MM"]
        )
        let routes = surface.channels.flatMap { $0.routes }
        let telegram = routes.first { $0.channel == "telegram" }
        XCTAssertEqual(telegram?.target, .unknown)
        XCTAssertFalse(MiniMaxCopy.routeTargetText(.unknown).contains("live"))
    }

    /// A route to a runtime agent that is not a seat names that agent, rather than
    /// calling it "not a live seat".
    func test_a_route_to_a_runtime_agent_that_is_not_a_seat_names_the_agent() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"], seatAliases: ["mavis": "MM"])
        let lark = surface.channels.flatMap { $0.routes }.first { $0.channel == "lark" }
        XCTAssertEqual(lark?.target, .runtimeAgent("worker"))
        XCTAssertEqual(
            MiniMaxCopy.routeTargetText(.runtimeAgent("worker")),
            "worker, an agent on the Mac that is not a fleet seat"
        )
    }

    // MARK: - CONTROLS-39:  counts, ids and order

    /// Default routes and rule routes are counted apart, a `- agentId:` item counts,
    /// and two routes to the same agent keep distinct ids.
    func test_rule_routes_and_default_routes_are_counted_apart() throws {
        let text = """
        schemaVersion: 2
        rules:
          - match: group-a
            agentId: mavis
          - agentId: worker
            match: group-b
        defaultRoute:
          feishu:
            agentId: mavis
          lark:
            agentId: mavis
        """
        let routes = MiniMaxReader.readRoutes(
            in: text,
            rosterSeats: ["MM"],
            seatAliases: ["mavis": "MM"],
            runtimeAgents: ["mavis", "worker"]
        )
        XCTAssertEqual(routes.map { $0.channel }, ["rule 1", "rule 2", "feishu", "lark"])
        XCTAssertEqual(routes.filter { $0.section == "rules" }.count, 2)
        XCTAssertEqual(routes.filter { $0.section == "defaultRoute" }.count, 2)
        XCTAssertEqual(Set(routes.map { $0.id }).count, routes.count)

        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try text.write(
            to: root(home).appendingPathComponent("channel-routes.yaml"),
            atomically: true,
            encoding: .utf8
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let file = surface.channels.first { $0.file == "channel-routes.yaml" }
        XCTAssertEqual(file?.detail, "2 rules, 2 default routes")
    }

    /// A spawned instance that keeps a memory folder of its own is not "this seat":
    /// the row and the route use the same test.
    func test_an_instance_with_a_memory_folder_is_not_this_seat() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.createDirectory(
            at: root(home).appendingPathComponent("agents/spawned/memory", isDirectory: true),
            withIntermediateDirectories: true
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(surface.agents.first { $0.name == "spawned" }?.ownsSeatMemory, false)
        XCTAssertEqual(surface.agents.filter { $0.ownsSeatMemory }.map { $0.name }, ["mavis"])
        XCTAssertEqual(MiniMaxReader.seatRuntimeAgentName(home: home), "mavis")
    }

    /// Hook rows are sorted and keep distinct ids, even for two identical
    /// registrations, and the top-level hooks.json is read beside the plugins.
    func test_hook_rows_are_sorted_with_distinct_ids_and_read_the_top_level_file() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try """
        {
         "enabled": false,
         "hooks": {
          "Stop": [ { "hooks": [ { "name": "mm-stop", "command": "mm-session-letter hook-stop" } ] } ],
          "SessionStart": [
           { "hooks": [
             { "command": "mm-session-letter hook-session-start" },
             { "command": "mm-session-letter hook-session-start" }
           ] }
          ]
         }
        }
        """.write(to: root(home).appendingPathComponent("hooks.json"), atomically: true, encoding: .utf8)
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(surface.userHooksFile, .read)
        XCTAssertEqual(surface.hooks.count, 4)
        XCTAssertEqual(Set(surface.hooks.map { $0.id }).count, 4)
        let topLevel = surface.hooks.filter { $0.plugin == nil }
        XCTAssertEqual(topLevel.map { $0.event }, ["SessionStart", "SessionStart", "Stop"])
        XCTAssertEqual(topLevel.last?.hookId, "mm-stop")
        XCTAssertEqual(topLevel.first?.hookId, "mm-session-letter")
        XCTAssertTrue(topLevel.allSatisfy { !$0.enabled })
        XCTAssertTrue(topLevel.allSatisfy { $0.script == .noPath })
        XCTAssertEqual(topLevel.first?.origin, "~/.minimax/hooks.json")
        // The order is the same on every read.
        let again = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(again.hooks.map { $0.id }, surface.hooks.map { $0.id })
    }

    // MARK: - CONTROLS-38:  script state and proof matching

    /// A script that is there without the execute bit is "not executable", never
    /// "missing", and an interpreter in front of it needs no execute bit.
    func test_the_script_state_separates_missing_from_not_executable() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let script = root(home).appendingPathComponent("plugins/fleet-guard/hooks/guard.py")
        try "print('hi')\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: script.path)

        let direct = MiniMaxReader.scriptLocation(in: script.path, home: home)
        XCTAssertEqual(MiniMaxReader.scriptState(direct), .notExecutable)

        let viaInterpreter = MiniMaxReader.scriptLocation(in: "/usr/bin/python3 \(script.path)", home: home)
        XCTAssertEqual(viaInterpreter.path, script.path)
        XCTAssertEqual(MiniMaxReader.scriptState(viaInterpreter), .present)

        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        XCTAssertEqual(MiniMaxReader.scriptState(direct), .present)

        let gone = MiniMaxReader.scriptLocation(in: "\(script.path)-gone --x", home: home)
        XCTAssertEqual(MiniMaxReader.scriptState(gone), .missing)
    }

    /// The script path is found behind an interpreter, `~`, `$HOME` and assignments.
    func test_the_script_path_is_found_in_interpreter_and_home_forms() {
        let home = URL(fileURLWithPath: "/tmp/fixture-home", isDirectory: true)
        XCTAssertEqual(
            MiniMaxReader.scriptLocation(in: "python3 ~/apps/x.py --flag", home: home),
            MiniMaxReader.ScriptLocation(path: "/tmp/fixture-home/apps/x.py", viaInterpreter: true)
        )
        XCTAssertEqual(
            MiniMaxReader.scriptLocation(in: "MODE=1 /usr/bin/env bash $HOME/x.sh", home: home).path,
            "/tmp/fixture-home/x.sh"
        )
        XCTAssertEqual(
            MiniMaxReader.scriptLocation(in: "${HOME}/bin/guard --format claude", home: home),
            MiniMaxReader.ScriptLocation(path: "/tmp/fixture-home/bin/guard", viaInterpreter: false)
        )
        XCTAssertNil(MiniMaxReader.scriptLocation(in: "python3 -c 'print(1)'", home: home).path)
        XCTAssertNil(MiniMaxReader.scriptLocation(in: "mm-session-letter hook-stop", home: home).path)
    }

    /// The hook id is derived the way the engine derives it, and a proof matches with
    /// a trailing `-hook` ignored on either side.
    func test_the_hook_id_and_proof_match_follow_the_engine() throws {
        XCTAssertEqual(MiniMaxReader.commandId("python3 ~/.claude/hooks/secret-guard.py"), "secret-guard")
        XCTAssertEqual(MiniMaxReader.commandId("/opt/lane-tools/fleet-guard-hook --format claude"), "fleet-guard-hook")
        XCTAssertEqual(MiniMaxReader.hookId(name: " named ", command: "/x/y.py"), "named")
        XCTAssertEqual(MiniMaxReader.hookKey("fleet-guard-hook"), "fleet-guard")

        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let proof = MiniMaxSurface.HookProof(
            hookName: "fleet-guard",
            required: "L1",
            achieved: "L1",
            checkedAt: nil,
            state: "PASS"
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"], proofs: [proof])
        XCTAssertEqual(surface.hooks.first?.proof, proof)
    }

    /// A rule that names two hooks proves both of them, and a failing proof is never
    /// hidden behind a passing one for the same hook.
    func test_every_name_a_rule_proves_is_kept() {
        let proofs = FleetStore.hookProofs(
            in: [
                result(names: ["fleet-guard-hook", "secret-guard"]),
                result(seat: "CLAUDE", names: ["other"]),
                result(kind: "mcp", names: ["not-a-hook"]),
            ],
            seat: "MM"
        )
        XCTAssertEqual(proofs.map { $0.hookName }, ["fleet-guard-hook", "secret-guard"])

        let hook = MiniMaxSurface.Hook(
            id: "h",
            plugin: "fleet-guard",
            origin: "plugin fleet-guard",
            event: "PreToolUse",
            matcher: "Bash",
            hookId: "secret-guard",
            shownCommand: "",
            scriptPath: nil,
            script: .noPath,
            timeout: nil,
            enabled: true,
            proof: nil
        )
        XCTAssertEqual(MiniMaxReader.attach(proofs, to: [hook]).first?.proof?.hookName, "secret-guard")

        let failing = FleetStore.hookProofs(in: [result(names: ["secret-guard"], state: "DRIFT")], seat: "MM")
        let attached = MiniMaxReader.attach(failing + proofs, to: [hook])
        XCTAssertEqual(attached.first?.proof?.state, "DRIFT")
    }

    // MARK: - CONTROLS-40:  the command is redacted

    /// A command never carries a token onto the screen, and the home folder is `~`.
    func test_the_command_is_redacted_before_it_is_kept() throws {
        let home = URL(fileURLWithPath: "/tmp/fixture-home", isDirectory: true)
        // A made-up value, long and opaque enough to be shaped like a token.
        let token = String(repeating: "aB3", count: 9)
        let shown = MiniMaxReader.shownCommand(
            "API_TOKEN=\(token) /tmp/fixture-home/bin/guard --token \(token) --format claude Bearer \(token) \(token)",
            home: home
        )
        XCTAssertFalse(shown.contains(token))
        XCTAssertTrue(shown.hasPrefix("API_TOKEN=<redacted> ~/bin/guard --token <redacted> --format claude"))
        XCTAssertTrue(shown.contains("Bearer <redacted>"))

        // The reader keeps only the redacted form.
        let built = try makeHome()
        defer { try? FileManager.default.removeItem(at: built) }
        try """
        {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "/bin/guard --api-key=\(token)"}]}]}}
        """.write(
            to: root(built).appendingPathComponent("plugins/fleet-guard/hooks/hooks.json"),
            atomically: true,
            encoding: .utf8
        )
        let surface = MiniMaxReader.read(home: built, rosterSeats: ["MM"])
        XCTAssertEqual(surface.hooks.first?.shownCommand, "/bin/guard --api-key=<redacted>")
    }

    // MARK: - CONTROLS-35, CONTROLS-36, SHELL-25:  absent, unreadable and empty

    /// A home with no `.minimax` folder is "not installed", and nothing in it is a
    /// read problem.
    func test_a_home_without_minimax_is_not_installed() throws {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-mm-empty-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertFalse(surface.installFound)
        XCTAssertEqual(surface.pluginsFolder, .absent)
        XCTAssertEqual(surface.agentsFolder, .absent)
        XCTAssertEqual(surface.settingsFile, .absent)
        XCTAssertEqual(surface.problems, [])
        let items = MiniMaxAttention.items(surface: surface, seat: "MM", engine: allPassing(), engineAnswered: true)
        XCTAssertEqual(items.first, "MiniMax is not installed on the Mac: there is no ~/.minimax folder.")
    }

    /// An unreadable plugins folder is a problem the panel shows, and the empty notes
    /// say the folder could not be listed rather than that nothing is installed.
    func test_an_unreadable_plugins_folder_is_a_problem_not_an_empty_list() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try makeUnreadable(root(home).appendingPathComponent("plugins", isDirectory: true), in: home)
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(surface.pluginsFolder, .unreadable)
        XCTAssertTrue(surface.hooks.isEmpty)
        XCTAssertTrue(surface.problems.contains { $0.contains("~/.minimax/plugins") })
        XCTAssertEqual(
            MiniMaxCopy.pluginsEmptyText(surface.pluginsFolder),
            "~/.minimax/plugins is there but could not be listed."
        )
        XCTAssertFalse(MiniMaxCopy.hooksEmptyText(surface).contains("registers a hook"))
        let items = MiniMaxAttention.items(surface: surface, seat: "MM", engine: allPassing(), engineAnswered: true)
        XCTAssertTrue(items.contains { $0.contains("~/.minimax/plugins") })
    }

    /// An empty plugins folder reads as "no plugin", with no problem.
    func test_an_empty_plugins_folder_is_empty_with_no_problem() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try FileManager.default.removeItem(at: root(home).appendingPathComponent("plugins/fleet-guard"))
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(surface.pluginsFolder, .read)
        XCTAssertTrue(surface.plugins.isEmpty)
        XCTAssertFalse(surface.problems.contains { $0.contains("plugins") })
        XCTAssertEqual(
            MiniMaxCopy.hooksEmptyText(surface),
            "No plugin manifest and no ~/.minimax/hooks.json on the Mac registers a hook."
        )
    }

    /// An unreadable channel file is not "not on the Mac", and a missing one is not a
    /// read problem.
    func test_an_unreadable_channel_file_is_not_called_absent() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let owner = root(home).appendingPathComponent("channel-owner.yaml")
        try makeUnreadable(owner, in: home)
        try FileManager.default.removeItem(at: root(home).appendingPathComponent("channel-bindings.yaml"))
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let ownerRow = surface.channels.first { $0.file == "channel-owner.yaml" }
        XCTAssertEqual(ownerRow?.state, .unreadable)
        XCTAssertEqual(ownerRow?.detail, "there, but could not be read")
        let bindingsRow = surface.channels.first { $0.file == "channel-bindings.yaml" }
        XCTAssertEqual(bindingsRow?.state, .absent)
        XCTAssertEqual(bindingsRow?.detail, "not on the Mac")
        XCTAssertTrue(surface.problems.contains("~/.minimax/channel-owner.yaml is there but could not be read."))
        // A file that is simply not there is shown in its row, and is not a read problem.
        XCTAssertFalse(surface.problems.contains { $0.contains("channel-bindings") })
    }

    /// A hook manifest that is not JSON is listed as a problem for the panel to show.
    func test_an_unreadable_hook_manifest_is_a_problem() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try "not json".write(
            to: root(home).appendingPathComponent("plugins/fleet-guard/hooks/hooks.json"),
            atomically: true,
            encoding: .utf8
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertTrue(surface.problems.contains { $0.contains("plugin fleet-guard") })
        XCTAssertTrue(MiniMaxCopy.hooksEmptyText(surface).hasPrefix("No hook is shown because"))
    }

    // MARK: - COPY-61, CONTROLS-41:  plain words, freshness

    /// The proof reads as words, with the level spelled out and no arrow.
    func test_the_proof_reads_as_plain_words() {
        let now = Date(timeIntervalSince1970: 1_700_003_600)
        let failing = MiniMaxSurface.HookProof(
            hookName: "fleet-guard-hook",
            required: "L1",
            achieved: nil,
            checkedAt: Date(timeIntervalSince1970: 1_700_000_000),
            state: "UNPROVEN"
        )
        XCTAssertEqual(
            MiniMaxCopy.proofText(failing, now: now),
            "Not proved.\u{00A0} Needs a blocked test write (L1), reached nothing.\u{00A0} Checked 1h ago."
        )
        let passing = MiniMaxSurface.HookProof(
            hookName: "fleet-guard-hook",
            required: "L1",
            achieved: "L1",
            checkedAt: Date(timeIntervalSince1970: 1_700_000_000),
            state: "PASS"
        )
        let text = MiniMaxCopy.proofText(passing, now: now)
        XCTAssertEqual(text, "Proved by a blocked test write (L1).\u{00A0} Checked 1h ago.")
        XCTAssertFalse(text.contains("→"))
        XCTAssertFalse(MiniMaxCopy.needsLook(passing))
    }

    /// The panel says when it read the disk and when the engine ran, on the owner's
    /// clock, and that the server's install is not shown.
    func test_the_panel_says_where_and_when_it_read() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let read = Date(timeIntervalSince1970: 1_700_000_000)
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"], now: read)
        XCTAssertEqual(
            MiniMaxCopy.readLine(surface),
            "Read from ~/.minimax on the Mac at 4:13 PM.\u{00A0} The server's MiniMax install is not shown here."
        )
        XCTAssertEqual(
            MiniMaxCopy.engineLine(phase: .loaded, generatedAt: read),
            "The engine's answers are from its run at 4:13 PM."
        )
        XCTAssertTrue(MiniMaxCopy.engineLine(phase: .running, generatedAt: nil).hasPrefix("The engine is checking now."))
        XCTAssertTrue(MiniMaxCopy.engineLine(phase: .idle, generatedAt: nil).contains("nothing here is proved"))
    }

    // MARK: - CONTROLS-34:  a plain answer at the top

    /// "Nothing needs a look" is earned only by a passing engine and a clean read:  an
    /// engine that has not answered, a missing row or a failing kind each need a look.
    func test_nothing_needs_a_look_only_when_everything_is_proved_and_read() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        // A clean install:  every agent has a file, and every route names a known id.
        try "seat\n".write(
            to: root(home).appendingPathComponent("agents/mavis/agent.md"),
            atomically: true,
            encoding: .utf8
        )
        try "id\n".write(
            to: root(home).appendingPathComponent("agents/spawned/agent.md"),
            atomically: true,
            encoding: .utf8
        )
        try "schemaVersion: 2\nrules: []\ndefaultRoute:\n  feishu:\n    agentId: mavis\n".write(
            to: root(home).appendingPathComponent("channel-routes.yaml"),
            atomically: true,
            encoding: .utf8
        )
        let script = root(home).appendingPathComponent("plugins/fleet-guard/hooks/fleet-guard-hook")
        try "#!/bin/sh\n".write(to: script, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
        let proof = MiniMaxSurface.HookProof(
            hookName: "fleet-guard-hook",
            required: "L1",
            achieved: "L1",
            checkedAt: nil,
            state: "PASS"
        )
        let surface = MiniMaxReader.read(
            home: home,
            rosterSeats: ["MM"],
            seatAliases: ["mavis": "MM"],
            proofs: [proof]
        )
        XCTAssertEqual(surface.problems, [])
        XCTAssertEqual(
            MiniMaxAttention.items(surface: surface, seat: "MM", engine: allPassing(), engineAnswered: true),
            []
        )

        let notYet = MiniMaxAttention.items(surface: surface, seat: "MM", engine: allPassing(), engineAnswered: false)
        XCTAssertEqual(notYet, ["The engine has not answered, so nothing on this panel is proved."])

        let noRow = MiniMaxAttention.items(surface: surface, seat: "MM", engine: [], engineAnswered: true)
        XCTAssertEqual(noRow, ["The engine has no row for seat MM, so nothing on this panel is proved."])

        var mixed = allPassing()
        mixed[1] = (kind: .hooks, state: .drift)
        mixed[2] = (kind: .mcp, state: .unknown)
        let failing = MiniMaxAttention.items(surface: surface, seat: "MM", engine: mixed, engineAnswered: true)
        XCTAssertEqual(
            failing,
            [
                "The engine does not pass Hooks for seat MM.",
                "The engine has no answer for MCP for seat MM.",
            ]
        )
        XCTAssertEqual(MiniMaxCopy.headline(count: 2), "2 things need a look.")
    }

    /// A hook whose rule did not pass, and a missing script, each need a look.
    func test_a_failing_hook_and_a_missing_script_need_a_look() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let proof = MiniMaxSurface.HookProof(
            hookName: "fleet-guard-hook",
            required: "L1",
            achieved: nil,
            checkedAt: nil,
            state: "UNPROVEN"
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"], proofs: [proof])
        let items = MiniMaxAttention.items(surface: surface, seat: "MM", engine: allPassing(), engineAnswered: true)
        XCTAssertTrue(items.contains("The PreToolUse hook fleet-guard-hook is not proved."))
        XCTAssertTrue(items.contains("The PreToolUse hook fleet-guard-hook runs a script that is missing."))
        XCTAssertTrue(items.contains { $0.hasPrefix("The telegram route names gone-bot") })
    }

    /// A hook the engine has no proof for is not fine, even when every kind passes.
    func test_a_hook_with_no_proof_needs_a_look() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"], proofs: [])
        let items = MiniMaxAttention.items(surface: surface, seat: "MM", engine: allPassing(), engineAnswered: true)
        XCTAssertTrue(
            items.contains("The PreToolUse hook fleet-guard-hook is not proved: the engine has no rule for it.")
        )
    }

    /// The id used to find a proof is the engine's, assignment and all, and the id on the
    /// screen never carries the value of an assignment.
    func test_a_hook_id_matches_like_the_engine_and_shows_no_assignment_value() {
        // The engine's own answers for these commands (`checks_hooks.command_id`).
        XCTAssertEqual(MiniMaxReader.commandId("MODE=strict /opt/bin/guard"), "MODE=strict")
        XCTAssertEqual(MiniMaxReader.commandId("A=1 B=2 python3 /opt/x/hook.py"), "hook")
        XCTAssertEqual(MiniMaxReader.commandId("MODE=strict"), "MODE=strict")
        XCTAssertEqual(MiniMaxReader.commandId(""), "")

        XCTAssertEqual(MiniMaxReader.shownHookId("MODE=strict"), "MODE=<redacted>")
        XCTAssertEqual(MiniMaxReader.shownHookId("fleet-guard-hook"), "fleet-guard-hook")
    }

    /// A hook whose id is an assignment is still proved by a rule that names that id, and
    /// the attention list and the hook's own line show the id without its value.
    func test_an_assignment_id_binds_its_proof_and_is_not_shown() {
        let hook = MiniMaxSurface.Hook(
            id: "h",
            plugin: nil,
            origin: "~/.minimax/hooks.json",
            event: "PreToolUse",
            matcher: "Bash",
            hookId: "MODE=strict",
            shownCommand: "",
            scriptPath: nil,
            script: .noPath,
            timeout: nil,
            enabled: true,
            proof: nil
        )
        let proof = MiniMaxSurface.HookProof(
            hookName: "MODE=strict",
            required: "L1",
            achieved: "L1",
            checkedAt: nil,
            state: "PASS"
        )
        XCTAssertEqual(MiniMaxReader.attach([proof], to: [hook]).first?.proof, proof)
        XCTAssertEqual(hook.shownHookId, "MODE=<redacted>")
    }

    /// A quoted value with spaces is one word, so its whole value is redacted, and a
    /// secret inside a JSON body or a header never reaches the screen.
    func test_quoted_and_embedded_secrets_are_redacted() {
        let home = URL(fileURLWithPath: "/tmp/fixture-home", isDirectory: true)
        XCTAssertEqual(
            MiniMaxReader.shellTokens("A=\"two words\" /bin/guard 'x y'"),
            ["A=\"two words\"", "/bin/guard", "'x y'"]
        )
        let assignment = MiniMaxReader.shownCommand("API_KEY=\"first second\" /bin/guard", home: home)
        XCTAssertEqual(assignment, "API_KEY=<redacted> /bin/guard")

        let body = MiniMaxReader.shownCommand("/bin/guard --body '{\"apiKey\":\"plainvalue\",\"mode\":\"strict\"}'", home: home)
        XCTAssertFalse(body.contains("plainvalue"))

        let inline = MiniMaxReader.shownCommand("/bin/guard --body={\"token\":\"plainvalue\"}", home: home)
        XCTAssertFalse(inline.contains("plainvalue"))

        let header = MiniMaxReader.shownCommand("/bin/guard -H 'Authorization: Bearer plainvalue'", home: home)
        XCTAssertFalse(header.contains("plainvalue"))

        // A plain path, even a long one, is left alone.
        let path = "/tmp/fixture-home/apps/a-very-long-file-name-with-many-words/guard"
        XCTAssertEqual(MiniMaxReader.shownCommand(path, home: home), "~/apps/a-very-long-file-name-with-many-words/guard")
    }

    /// A relative script path reads as "no absolute file path", never as no path at all.
    func test_a_relative_script_path_is_not_reported_as_having_no_path() {
        let hook = MiniMaxSurface.Hook(
            id: "h",
            plugin: nil,
            origin: "~/.minimax/hooks.json",
            event: "PreToolUse",
            matcher: "Bash",
            hookId: "guard",
            shownCommand: "python3 hooks/guard.py",
            scriptPath: nil,
            script: .noPath,
            timeout: nil,
            enabled: true,
            proof: nil
        )
        XCTAssertEqual(
            MiniMaxCopy.scriptText(hook),
            "no absolute file path in the command, so nothing was checked"
        )
    }

    /// A pass with no recorded level does not claim to be proved by nothing.
    func test_a_pass_with_no_level_does_not_say_proved_by_nothing() {
        let proof = MiniMaxSurface.HookProof(
            hookName: "fleet-guard-hook",
            required: "L1",
            achieved: nil,
            checkedAt: nil,
            state: "PASS"
        )
        let text = MiniMaxCopy.proofText(proof)
        XCTAssertFalse(text.contains("nothing"))
        XCTAssertTrue(text.hasPrefix("Proved, but the engine recorded no level."))
    }

    /// A kind the manifest covers has no engine result, and the all-clear says so.
    func test_the_all_clear_does_not_claim_a_covered_kind_passed() {
        var engine = allPassing()
        XCTAssertTrue(MiniMaxCopy.allClearText(seat: "MM", engine: engine).contains("passes every config kind for seat MM"))
        engine[0] = (kind: engine[0].kind, state: .covered)
        let text = MiniMaxCopy.allClearText(seat: "MM", engine: engine)
        XCTAssertTrue(text.contains("every config kind it checks"))
        XCTAssertTrue(text.contains("the manifest covers \(engine[0].kind.title)"))
    }

    /// A manifest whose `hooks` is the wrong shape is unreadable, and a plugin manifest
    /// with no `hooks` section is a problem, not a quiet empty plugin.
    func test_a_wrong_shaped_or_missing_hooks_section_is_a_problem() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let manifest = root(home).appendingPathComponent("plugins/fleet-guard/hooks/hooks.json")

        try "{\"hooks\": [1]}".write(to: manifest, atomically: true, encoding: .utf8)
        let wrongType = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertTrue(wrongType.problems.contains { $0.contains("plugin fleet-guard") })
        XCTAssertEqual(wrongType.plugins.first { $0.name == "fleet-guard" }?.hookManifestValid, false)

        try "{\"hooks\": {\"PreToolUse\": \"nope\"}}".write(to: manifest, atomically: true, encoding: .utf8)
        let wrongEvent = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(wrongEvent.plugins.first { $0.name == "fleet-guard" }?.hookManifestValid, false)

        try "{}".write(to: manifest, atomically: true, encoding: .utf8)
        let missing = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(missing.plugins.first { $0.name == "fleet-guard" }?.hookManifestValid, true)
        XCTAssertTrue(
            missing.problems.contains("The hook manifest of plugin fleet-guard has no hooks section, so the plugin registers no hook.")
        )
    }

    /// An engine that has not answered is never reported as having no row for the seat:
    /// the store clears its cells while idle, running or failed.
    func test_an_engine_that_has_not_answered_is_not_a_missing_row() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        let items = MiniMaxAttention.items(surface: surface, seat: "MM", engine: [], engineAnswered: false)
        XCTAssertTrue(items.contains("The engine has not answered, so nothing on this panel is proved."))
        XCTAssertFalse(items.contains { $0.contains("no row for seat") })

        // A hook with no proof before the engine answers is not called "no rule" either.
        XCTAssertEqual(
            MiniMaxCopy.proofText(nil, engineAnswered: false),
            "Not proved: the engine has not answered yet."
        )
        XCTAssertTrue(
            items.contains("The PreToolUse hook fleet-guard-hook is not proved: the engine has not answered.")
        )
        XCTAssertFalse(items.contains { $0.contains("has no rule") })

        XCTAssertEqual(
            MiniMaxCopy.verdictsEmptyText(phase: .running, seat: "MM"),
            "The engine has not answered yet, so there is no answer to show."
        )
        XCTAssertEqual(
            MiniMaxCopy.verdictsEmptyText(phase: .failed("down"), seat: "MM"),
            "The engine has not answered yet, so there is no answer to show."
        )
        XCTAssertEqual(
            MiniMaxCopy.verdictsEmptyText(phase: .loaded, seat: "MM"),
            "The engine has no row for seat MM, so there is no answer to show."
        )
    }

    /// Visible strings keep one plain space after a colon or a semicolon.
    func test_copy_has_one_space_after_a_colon() throws {
        XCTAssertFalse(MiniMaxCopy.proofText(nil).contains(":  "))
        XCTAssertFalse(MiniMaxCopy.engineLine(phase: .failed("down"), generatedAt: nil).contains(":  "))
        let empty = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-mm-empty-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: empty, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: empty) }
        let surface = MiniMaxReader.read(home: empty, rosterSeats: ["MM"])
        let items = MiniMaxAttention.items(surface: surface, seat: "MM", engine: [], engineAnswered: true)
        XCTAssertTrue(items.contains("MiniMax is not installed on the Mac: there is no ~/.minimax folder."))
    }

    /// A stray file in the agents folder is not an agent and not a read problem.
    func test_a_stray_file_in_the_agents_folder_is_not_an_agent() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try "notes\n".write(
            to: root(home).appendingPathComponent("agents/README.md"),
            atomically: true,
            encoding: .utf8
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertFalse(surface.agents.contains { $0.name == "README.md" })
        XCTAssertFalse(surface.problems.contains { $0.contains("README.md") })
        XCTAssertTrue(surface.agents.contains { $0.name == "worker" })
    }

    /// Valid JSON with no `hooks` object registers no hooks:  it is empty, not unreadable.
    func test_a_manifest_with_no_hooks_object_is_empty_not_unreadable() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        try "{\"enabled\": false}".write(
            to: root(home).appendingPathComponent("hooks.json"),
            atomically: true,
            encoding: .utf8
        )
        let surface = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(surface.userHooksFile, .read)
        XCTAssertFalse(surface.problems.contains { $0.contains("hooks.json") })

        try "[1, 2]".write(
            to: root(home).appendingPathComponent("hooks.json"),
            atomically: true,
            encoding: .utf8
        )
        let broken = MiniMaxReader.read(home: home, rosterSeats: ["MM"])
        XCTAssertEqual(broken.userHooksFile, .unreadable)
    }
}
