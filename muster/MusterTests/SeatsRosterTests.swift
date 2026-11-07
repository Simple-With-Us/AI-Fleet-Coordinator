import XCTest

/// The Seats panel's own roster:  every seat the fleet runs,  wherever its eavesdropper is,
/// each seat once,  and the compliance grid's roster left exactly as it was.
final class SeatsRosterTests: XCTestCase {
    // MARK: The fixtures

    /// A registry in the shape of `fleet-apps.json`:  live seats,  a retired one,  an alias,
    /// the group tag,  the extra seats and the two role families.
    private let registryJSON = """
    {
      "apps": [],
      "seats": [
        {"tag": "CLAUDE"},
        {"tag": "MONET", "retired": true},
        {"tag": "GROK-BUILD", "retired": true, "aliasOf": "GROK"},
        {"tag": "GROK-BOT"},
        {"tag": "MA"},
        {"tag": "MM"}
      ],
      "seatApps": {
        "bySeat": {"CLAUDE": "claude-code", "MA": "muse-assist", "MM": "minimax-code", "GROK-BOT": "grok-bot"},
        "extraSeats": {"AMP": "amp", "JET": "chatgpt", "JET-CHAT": "chatgpt", "OLDBOT": "oldbot", "OPERATOR": "operator"},
        "patterns": [
          {"match": "BF-*", "app": "botfleet", "lane": {"roles": ["COMPILER", "ENGINEER", "BUILDER"], "folderTemplate": "bf-<role>"}},
          {"match": "GB-*", "app": "grok-bot", "lane": {"roles": ["FIXER", "DIRECTOR"], "folderTemplate": "gb-<role>"}}
        ]
      }
    }
    """

    /// The seat partition,  in the shape of `docs/protocols/agent-sync-partition.toml`.
    private let partitionTOML = """
    version = 1

    [seats]
    OPERATOR = "mac"
    CLAUDE = "mac"
    MM = "mac"
    AMP = "mac"
    MA = "server"
    JET = "server"
    WISE = "server"
    GB-FIXER = "server"
    GB-DIRECTOR = "server"
    BF-COMPILER = "server"
    BF-ENGINEER = "server"
    BF-BUILDER = "none"
    GROK-BUILD = "none"
    OLDBOT = "none"
    """

    /// The Mac eavesdropper's state:  AMP is a Mac seat it publishes as connected (wake inbox).
    private let listenerJSON = """
    {
      "updated": 1792821067.0,
      "instance": "mac",
      "disabled": [],
      "skipped_seats": {},
      "pause": {},
      "seats": {
        "CLAUDE": {"bot": "Claude", "connected": true, "jobs": 0, "leases": []},
        "MM": {"bot": "MiniMax", "connected": true, "jobs": 0, "leases": []},
        "AMP": {"bot": "Amp", "connected": true, "jobs": 0, "leases": []}
      }
    }
    """

    /// The grid's roster for the registry above:  the live seats,  in registry order.
    private let gridRoster = ["CLAUDE", "MA", "MM"]

    private func registry() throws -> FleetRegistryDocument {
        try JSONDecoder().decode(FleetRegistryDocument.self, from: Data(registryJSON.utf8))
    }

    private func partition() -> ListenerPartition {
        ListenerPartition.parse(partitionTOML, path: "/fixture/agent-sync-partition.toml")
    }

    private func model() throws -> SeatActivityModel {
        let roster = SeatActivityRoster.seats(gridRoster: gridRoster, registry: try registry(), partition: partition())
        var sources = SeatActivityBuilder.Sources()
        sources.roster = roster
        sources.partition = partition()
        sources.listener = try JSONDecoder().decode(ListenerStateDocument.self, from: Data(listenerJSON.utf8))
        return SeatActivityBuilder.build(sources)
    }

    // MARK: The roster

    /// The grid's seats come first,  as they are,  then the registry's extra seats,  the role
    /// families and the partition's other held seats.  Retired names,  aliases,  the group tag,
    /// the Operator,  a second login the partition does not list and every name the partition
    /// marks `none`,  whichever source names it,  are never rows.
    func test_the_roster_holds_every_seat_once_in_order() throws {
        let roster = SeatActivityRoster.seats(gridRoster: gridRoster, registry: try registry(), partition: partition())
        XCTAssertEqual(roster, [
            "CLAUDE", "MA", "MM",
            "AMP", "JET",
            "BF-COMPILER", "BF-ENGINEER",
            "GB-FIXER", "GB-DIRECTOR",
            "WISE",
        ])
        XCTAssertEqual(Array(roster.prefix(gridRoster.count)), gridRoster)
        for name in ["MONET", "GROK-BUILD", "GROK-BOT", "BF-BUILDER", "OLDBOT", "JET-CHAT", "OPERATOR", "BF-*", "GB-*"] {
            XCTAssertFalse(roster.contains(name), name)
        }
    }

    /// A family role or an extra seat the partition marks `none` is a retired name,  and gets
    /// no row from the registry either.
    func test_a_none_entry_is_never_a_row_whichever_source_names_it() throws {
        let registry = try registry()
        XCTAssertTrue(registry.familySeats.contains("BF-BUILDER"))
        XCTAssertNotNil(registry.seatApps?.extraSeats?["OLDBOT"])
        let roster = SeatActivityRoster.seats(gridRoster: gridRoster, registry: registry, partition: partition())
        XCTAssertFalse(roster.contains("BF-BUILDER"), "\(roster)")
        XCTAssertFalse(roster.contains("OLDBOT"), "\(roster)")
    }

    /// The Operator is a service with no queue,  so it is never a Seats row and never counts
    /// as a Mac seat whose state could not be read.
    func test_the_operator_is_not_a_seat_row() throws {
        let model = try model()
        XCTAssertFalse(model.rows.contains { $0.seat == "OPERATOR" })
    }

    /// No seat is listed twice,  however many sources name it.
    func test_no_seat_is_duplicated() throws {
        let roster = SeatActivityRoster.seats(
            gridRoster: gridRoster + ["CLAUDE"],
            registry: try registry(),
            partition: partition()
        )
        XCTAssertEqual(Set(roster.map { $0.uppercased() }).count, roster.count, "\(roster)")
        let rows = try model().rows.map(\.seat)
        XCTAssertEqual(Set(rows).count, rows.count, "\(rows)")
    }

    /// With nothing but the grid's roster to go on,  the panel's roster is the grid's.
    func test_without_the_registry_or_partition_the_roster_is_the_grids() {
        let roster = SeatActivityRoster.seats(
            gridRoster: gridRoster,
            registry: nil,
            partition: ListenerPartition(unreadable: "It could not be read.")
        )
        XCTAssertEqual(roster, gridRoster)
    }

    /// The grid's own roster is not changed by any of this:  AMP and the role families are
    /// the Seats panel's rows,  never the engine grid's.
    func test_the_grid_roster_is_unchanged() throws {
        let gridRegistry = try JSONDecoder().decode(FleetRegistry.self, from: Data(registryJSON.utf8))
        XCTAssertEqual(FleetStore.roster(from: gridRegistry, manifest: nil), gridRoster)
        let grid = FleetStore.roster(from: gridRegistry, manifest: nil)
        for seat in ["AMP", "JET", "OPERATOR", "GB-FIXER", "BF-ENGINEER", "WISE"] {
            XCTAssertFalse(grid.contains(seat), seat)
        }
    }

    // MARK: The rows

    /// AMP is a Mac seat the eavesdropper publishes as connected,  and it gets a row that
    /// says so.
    func test_amp_gets_a_mac_row() throws {
        let row = try XCTUnwrap(try model().rows.first { $0.seat == "AMP" })
        XCTAssertEqual(row.place, .mac)
        XCTAssertEqual(row.listener, .known(.connected(jobs: 0)))
        XCTAssertEqual(row.openSessions, .known(0))
    }

    /// A Grok Bot and a BotFleet seat are held by the server,  and their rows say plainly
    /// that they are not read here:  never unknown,  never zero,  never healthy.
    func test_a_grok_bot_and_a_botfleet_seat_get_server_rows_that_read_not_read_here() throws {
        let model = try model()
        for seat in ["GB-FIXER", "BF-ENGINEER"] {
            let row = try XCTUnwrap(model.rows.first { $0.seat == seat }, seat)
            XCTAssertEqual(row.place, .server, seat)
            for fact in [row.listener.notHereReason, row.openSessions.notHereReason, row.quota.notHereReason] {
                let reason = try XCTUnwrap(fact, seat)
                XCTAssertTrue(reason.contains("not read here"), reason)
                XCTAssertTrue(reason.contains("server"), reason)
            }
            XCTAssertFalse(row.listener.isKnown, seat)
            XCTAssertFalse(row.openSessions.isKnown, seat)
            XCTAssertNil(row.listener.reason, "\(seat) is not a failed read")
        }
        let server = try XCTUnwrap(model.groups.first { $0.kind == .server })
        XCTAssertTrue(server.rows.contains { $0.seat == "GB-FIXER" })
        XCTAssertTrue(server.rows.contains { $0.seat == "BF-ENGINEER" })
        // Every seat the partition gives the server has a row,  so none is left to a note.
        XCTAssertTrue(model.otherServerSeats.isEmpty, "\(model.otherServerSeats)")
        XCTAssertNil(model.otherServerSeatsNote)
    }

    // MARK: The registry's families

    /// A family pattern names one seat per role,  and a lane block of a shape this app does
    /// not expect costs the roles,  never the registry.
    func test_family_seats_come_from_the_roles_and_a_bad_lane_block_costs_only_the_roles() throws {
        XCTAssertEqual(try registry().familySeats, ["BF-COMPILER", "BF-ENGINEER", "BF-BUILDER", "GB-FIXER", "GB-DIRECTOR"])
        let odd = registryJSON.replacingOccurrences(
            of: #""lane": {"roles": ["COMPILER", "ENGINEER", "BUILDER"], "folderTemplate": "bf-<role>"}"#,
            with: #""lane": "not a block""#
        )
        XCTAssertNotEqual(odd, registryJSON)
        let decoded = try JSONDecoder().decode(FleetRegistryDocument.self, from: Data(odd.utf8))
        XCTAssertEqual(decoded.familySeats, ["GB-FIXER", "GB-DIRECTOR"])
        XCTAssertEqual(decoded.seats.count, 6)
    }

    /// A pattern that is only `*` names no family,  so its bare roles are never seat tags and
    /// never rows.
    func test_a_bare_star_pattern_names_no_seat() throws {
        let json = """
        {
          "apps": [],
          "seats": [{"tag": "CLAUDE"}],
          "seatApps": {
            "patterns": [
              {"match": "*", "app": "anything", "lane": {"roles": ["COMPILER", "FIXER"]}},
              {"match": " *", "app": "spaced", "lane": {"roles": ["DIRECTOR"]}},
              {"match": "BF-*", "app": "botfleet", "lane": {"roles": ["ENGINEER"]}}
            ]
          }
        }
        """
        let registry = try JSONDecoder().decode(FleetRegistryDocument.self, from: Data(json.utf8))
        XCTAssertEqual(registry.familySeats, ["BF-ENGINEER"])
        let roster = SeatActivityRoster.seats(gridRoster: ["CLAUDE"], registry: registry, partition: nil)
        for role in ["COMPILER", "FIXER", "DIRECTOR", " DIRECTOR"] {
            XCTAssertFalse(roster.contains(role), "\(role) is a role,  not a seat:  \(roster)")
        }
        XCTAssertTrue(roster.contains("BF-ENGINEER"), "\(roster)")
    }

    // MARK: Reading the files

    /// A checkout in a temporary folder,  holding a registry and a seat partition.
    private func checkout(registry: String, partition: String) throws -> EngineLocation {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("muster-roster-\(UUID().uuidString)", isDirectory: true)
        let protocols = root.appendingPathComponent("docs/protocols", isDirectory: true)
        try FileManager.default.createDirectory(at: protocols, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        let location = EngineLocation(root: root, interpreter: root.appendingPathComponent("python3"))
        try write(registry, to: location.registryURL, modified: Date(timeIntervalSince1970: 1_000_000))
        try write(partition, to: root.appendingPathComponent(ListenerPartition.relativePath),
                  modified: Date(timeIntervalSince1970: 1_000_000))
        return location
    }

    /// Writes a file and sets its modification time,  so a rewrite always reads as a change.
    private func write(_ text: String, to url: URL, modified: Date) throws {
        try text.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.modificationDate: modified], ofItemAtPath: url.path)
    }

    /// The panel asks for the roster on every refresh:  an unchanged registry or partition is
    /// not read or decoded again,  and a changed one is,  and its change shows in the roster.
    func test_an_unchanged_registry_or_partition_is_not_read_again() throws {
        let firstRegistry = #"{"apps": [], "seats": [{"tag": "CLAUDE"}, {"tag": "MA"}]}"#
        let firstPartition = "version = 1\n\n[seats]\nCLAUDE = \"mac\"\nMA = \"server\"\n"
        let location = try checkout(registry: firstRegistry, partition: firstPartition)
        let cache = SeatRosterFileCache()

        let first = SeatActivityRoster.current(gridRoster: ["CLAUDE"], engine: location, cache: cache)
        XCTAssertEqual(first, ["CLAUDE", "MA"])
        XCTAssertEqual(cache.registryReads, 1)
        XCTAssertEqual(cache.partitionReads, 1)

        // Unchanged:  the same roster,  and neither file is read again.
        let again = SeatActivityRoster.current(gridRoster: ["CLAUDE"], engine: location, cache: cache)
        XCTAssertEqual(again, first)
        XCTAssertEqual(cache.registryReads, 1)
        XCTAssertEqual(cache.partitionReads, 1)

        // The registry changes:  it is read again,  and the partition is not.
        let secondRegistry = #"{"apps": [], "seats": [{"tag": "CLAUDE"}, {"tag": "MA"}, {"tag": "MM"}]}"#
        try write(secondRegistry, to: location.registryURL, modified: Date(timeIntervalSince1970: 2_000_000))
        let withMM = SeatActivityRoster.current(gridRoster: ["CLAUDE"], engine: location, cache: cache)
        XCTAssertEqual(withMM, ["CLAUDE", "MA", "MM"])
        XCTAssertEqual(cache.registryReads, 2)
        XCTAssertEqual(cache.partitionReads, 1)

        // The partition changes:  it is read again,  and the registry is not.
        let secondPartition = firstPartition + "WISE = \"server\"\n"
        try write(secondPartition, to: location.root.appendingPathComponent(ListenerPartition.relativePath),
                  modified: Date(timeIntervalSince1970: 2_000_000))
        let withWise = SeatActivityRoster.current(gridRoster: ["CLAUDE"], engine: location, cache: cache)
        XCTAssertEqual(withWise, ["CLAUDE", "MA", "MM", "WISE"])
        XCTAssertEqual(cache.registryReads, 2)
        XCTAssertEqual(cache.partitionReads, 2)

        // A file that goes away adds nothing,  and the grid's seats are still there.
        try FileManager.default.removeItem(at: location.registryURL)
        let gone = SeatActivityRoster.current(gridRoster: ["CLAUDE"], engine: location, cache: cache)
        XCTAssertEqual(gone, ["CLAUDE", "MA", "WISE"])
    }

    /// The real registry and partition:  AMP,  JET and every Grok Bot and BotFleet seat the
    /// partition holds have a row,  once each.  The counts come from the files themselves,  so
    /// a role the owner adds or retires does not fail this test.  A tag that is never a Seats
    /// row (the Operator,  a service the partition holds on the Mac) is left out of the "has a
    /// row" checks,  and checked to have none.
    func test_the_checkouts_registry_and_partition_give_every_server_seat_a_row() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let registryURL = root.appendingPathComponent("fleet-apps.json")
        guard let data = try? Data(contentsOf: registryURL) else {
            throw XCTSkip("The registry is not in this checkout.")
        }
        let registry = try JSONDecoder().decode(FleetRegistryDocument.self, from: data)
        let partition = ListenerPartition.load(from: root.appendingPathComponent(ListenerPartition.relativePath))
        let held = try XCTUnwrap(partition.seats, partition.problem ?? "")
        let grid = try JSONDecoder().decode(FleetRegistry.self, from: data).liveTags
        let roster = SeatActivityRoster.seats(gridRoster: grid, registry: registry, partition: partition)

        XCTAssertEqual(Set(roster).count, roster.count, "\(roster)")
        XCTAssertTrue(roster.contains("AMP"))
        XCTAssertTrue(roster.contains("JET"))
        for (seat, instance) in held where instance != "none" {
            if SeatActivityRoster.notSeatRows.contains(seat.uppercased()) {
                XCTAssertFalse(roster.contains(seat), "\(seat) is never a Seats row")
                continue
            }
            XCTAssertTrue(roster.contains(seat), "\(seat) is held by \(instance) and has no row")
        }
        for seat in registry.familySeats {
            let holder = held[seat]
            if SeatActivityRoster.notSeatRows.contains(seat.uppercased()) {
                XCTAssertFalse(roster.contains(seat), "\(seat) is never a Seats row")
            } else if holder == "mac" || holder == "server" {
                XCTAssertTrue(roster.contains(seat), "\(seat) is held by \(holder ?? "") and has no row")
            } else {
                XCTAssertFalse(roster.contains(seat), "\(seat) is not held and has a row")
            }
        }
        XCTAssertFalse(roster.contains("GROK-BOT"))
        XCTAssertFalse(roster.contains("OPERATOR"), "the Operator is a service,  not a seat row")
        XCTAssertFalse(roster.contains("JET-CHAT"), "Jet's second login is not a row of its own")
        for (seat, instance) in held where instance == "none" {
            XCTAssertFalse(roster.contains(seat), "\(seat) is marked none and has a row")
        }
    }
}
