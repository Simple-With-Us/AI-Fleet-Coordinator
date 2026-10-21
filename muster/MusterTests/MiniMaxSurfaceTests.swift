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

    /// The child count reads the direct children of a key, and no deeper.
    ///
    /// The connection entry carries four fields of its own, so a count that reads
    /// every nested key would report five connections where there is one.
    func test_the_connection_count_reads_direct_children_only() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, liveSeats: ["MM"])
        let bindings = surface.channels.first { $0.file == "channel-bindings.yaml" }
        XCTAssertEqual(bindings?.detail, "1 connections, 0 message filters")
    }

    /// A route that names the runtime agent of a live seat is answered.
    func test_a_route_to_the_seat_runtime_agent_is_answered() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(
            home: home,
            liveSeats: ["MM"],
            seatAliases: ["mavis": "MM"]
        )
        let routes = surface.channels.flatMap { $0.routes }
        let feishu = routes.first { $0.channel == "feishu" }
        XCTAssertEqual(feishu?.agentId, "mavis")
        XCTAssertEqual(feishu?.target, .fleetSeat("MM"))
        XCTAssertEqual(feishu?.answered, true)
    }

    /// A route to an agent nothing on this Mac knows is not answered.
    func test_a_route_to_an_unknown_agent_is_not_answered() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(
            home: home,
            liveSeats: ["MM"],
            seatAliases: ["mavis": "MM"]
        )
        let routes = surface.channels.flatMap { $0.routes }
        let telegram = routes.first { $0.channel == "telegram" }
        XCTAssertEqual(telegram?.target, .unknown)
        XCTAssertEqual(telegram?.answered, false)
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
        let surface = MiniMaxReader.read(home: home, liveSeats: ["MM"])
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
        let surface = MiniMaxReader.read(home: home, liveSeats: ["MM"])
        let keys = surface.settings.map { $0.key }
        XCTAssertTrue(keys.contains("permissionMode"))
        XCTAssertFalse(keys.contains("apiKey"))
        for setting in surface.settings {
            XCTAssertFalse(setting.value.contains("sk-secret-value"))
        }
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
            liveSeats: ["MM"],
            proofs: [proof]
        )
        let hook = surface.hooks.first
        XCTAssertEqual(hook?.plugin, "fleet-guard")
        XCTAssertEqual(hook?.event, "PreToolUse")
        XCTAssertEqual(hook?.matcher, "Bash")
        XCTAssertEqual(hook?.proof?.achieved, "L1")
        XCTAssertEqual(hook?.proof?.state, "PASS")
    }

    /// A hook with no proof is unproved, never fine.
    func test_a_hook_with_no_proof_carries_none() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, liveSeats: ["MM"])
        XCTAssertNil(surface.hooks.first?.proof)
    }
}
