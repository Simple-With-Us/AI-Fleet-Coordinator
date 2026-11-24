import XCTest

/// The one-listener cutover of Nov 10:  the server listener holds every seat, and the Mac's
/// listener is retired.  The Wake & Listener panel says so, never shows the retired listener's
/// last status as current, and offers no edit for a seat the server holds.  Every partition here
/// is a fixture, as the other partition tests use.
final class OneListenerCutoverTests: XCTestCase {
    // MARK: Fixtures

    /// Only the Operator's service identity stays on the Mac.
    private let allServer = ListenerPartition(seats: [
        "OPERATOR": "mac", "CLAUDE": "server", "MM": "server", "BF-BUILDER": "none",
    ])

    /// The partition from before the cutover:  MM still on the Mac.
    private let beforeCutover = ListenerPartition(seats: [
        "OPERATOR": "mac", "MM": "mac", "CLAUDE": "server",
    ])

    /// The Mac's file after the cutover:  no `[seat.*]` table.
    private let config = "[daemon]\nhost_wake_load_per_core = 0\n"

    // MARK: The partition

    /// The Operator is a service identity, not an agent seat, so a partition that gives the Mac
    /// only the Operator gives it no agent seat.  A partition that could not be read says nothing.
    func test_the_partition_says_when_the_mac_holds_no_agent_seat() {
        XCTAssertEqual(allServer.macAgentSeats, [])
        XCTAssertTrue(allServer.givesTheMacNoAgentSeat)
        XCTAssertEqual(beforeCutover.macAgentSeats, ["MM"])
        XCTAssertFalse(beforeCutover.givesTheMacNoAgentSeat)
        let unread = ListenerPartition(unreadable: "It is missing.")
        XCTAssertNil(unread.macAgentSeats)
        XCTAssertFalse(unread.givesTheMacNoAgentSeat)
        // The repository's own partition is the one-listener partition.
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let repository = ListenerPartition.load(from: root.appendingPathComponent(ListenerPartition.relativePath))
        XCTAssertNil(repository.problem)
        XCTAssertTrue(repository.givesTheMacNoAgentSeat)
    }

    // MARK: The Seats banner

    /// The retired Mac listener's stale state file is not a problem once the partition gives
    /// the Mac no agent seat, so the Seats panel banners nothing for it.  While the Mac still
    /// held a seat, and when the partition could not be read at all, the same problem shows.
    func test_the_retired_mac_listeners_stale_state_is_not_a_seats_problem() {
        let stale = "The Mac eavesdropper's state was last written 12 days ago, so the eavesdropper may have stopped."
        func problems(_ partition: ListenerPartition?) -> [String] {
            var sources = SeatActivityBuilder.Sources()
            sources.roster = ["CLAUDE"]
            sources.partition = partition
            sources.listenerProblem = stale
            return SeatActivityBuilder.build(sources).problems
        }
        XCTAssertEqual(problems(allServer), [])
        XCTAssertEqual(problems(beforeCutover), [stale])
        XCTAssertTrue(problems(nil).contains(stale))
        XCTAssertTrue(problems(ListenerPartition(unreadable: "It is missing.")).contains(stale))
    }
}
