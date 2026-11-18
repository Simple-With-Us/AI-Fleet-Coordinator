import XCTest

/// The seat partition:  which host holds each seat, and what a partition that cannot be read
/// holds.
final class WakeSettingsTests: XCTestCase {
    /// A partition like the eavesdropper's own.
    private let partitionText = """
    version = 1

    [seats]
    # Mac seats
    MM = "mac"
    MA = "server"
    BF-BUILDER = "none"
    """

    // MARK: The seat partition

    /// The partition reads the way the eavesdropper reads it,  and every kind of seat has its host.
    func test_the_partition_gives_each_seat_a_host() {
        let partition = ListenerPartition.parse(partitionText, path: "/p")
        XCTAssertNil(partition.problem)
        XCTAssertEqual(partition.host(of: "MM"), .mac)
        XCTAssertEqual(partition.host(of: "MA"), .server)
        XCTAssertEqual(partition.host(of: "BF-BUILDER"), .noListener)
        XCTAssertEqual(partition.host(of: "KIMI"), .unlisted)
        // The eavesdropper upper-cases a section's name before it looks the seat up.
        XCTAssertEqual(partition.host(of: "ma"), .server)
        XCTAssertTrue(partition.host(of: "MM").isTheMac)
        for seat in ["MA", "BF-BUILDER", "KIMI"] {
            XCTAssertTrue(partition.host(of: seat).isElsewhere, seat)
            XCTAssertFalse(partition.host(of: seat).isTheMac, seat)
            XCTAssertNotNil(partition.host(of: seat).readOnlyReason, seat)
        }
    }

    /// A partition that is not read cleanly holds no seat,  as `load_partition` fails closed.
    func test_a_partition_that_is_not_read_cleanly_holds_no_seat() throws {
        let broken = [
            "version = 1\n",
            "[seats]\nMM = \"laptop\"\n",
            "[seats]\nMM = 3\n",
            "[seats]\nMM = \"mac\"\nMM = \"server\"\n",
            "[seats]\nnot a line\nMM = \"mac\"\n",
            "[seats]\nMM = \"mac\"\n[seats]\nMA = \"server\"\n",
            "[seats]\nbad.seat = \"mac\"\n",
        ]
        for text in broken {
            let partition = ListenerPartition.parse(text, path: "/p")
            XCTAssertNotNil(partition.problem, text)
            guard case .unknown = partition.host(of: "MM") else {
                XCTFail("expected unknown for: \(text)")
                continue
            }
            XCTAssertFalse(partition.host(of: "MM").isTheMac, text)
            XCTAssertFalse(partition.host(of: "MM").isElsewhere, text)
        }
        XCTAssertNotNil(ListenerPartition.load(from: nil).problem)
        let missing = URL(fileURLWithPath: "/nonexistent/partition.toml")
        XCTAssertNotNil(ListenerPartition.load(from: missing).problem)
    }

    /// The repository's own partition parses here,  and says what the eavesdropper's tests say.
    func test_the_repository_partition_reads_as_documented() {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let partition = ListenerPartition.load(from: root.appendingPathComponent(ListenerPartition.relativePath))
        XCTAssertNil(partition.problem)
        // One listener, on the server, since Nov 10:  only the Operator's service identity stays on the Mac.
        XCTAssertEqual(partition.host(of: "OPERATOR"), .mac)
        for seat in ["CLAUDE", "CODEX", "AG", "CURSOR", "GROK", "CLUTCH", "FX", "MM", "MC", "OPENCODE", "ZC", "KIMI", "AMP",
                     "MA", "ECHO", "WISE", "GROK-WEB", "JET", "GB-FIXER", "BF-DIRECTOR", "BF-COMPILER", "BF-ENGINEER", "BF-TRADER"] {
            XCTAssertEqual(partition.host(of: seat), .server, seat)
        }
        XCTAssertEqual(partition.host(of: "BF-BUILDER"), .noListener)
    }

    /// A tag is one the eavesdropper accepts,  or it never reaches a `[seat.X]` header.
    func test_only_a_real_seat_name_counts() {
        for good in ["MM", "GROK-WEB", "BF_X", "A1"] {
            XCTAssertTrue(ListenerPartition.isSeatName(good), good)
        }
        for bad in ["", "mm", "-MM", "MM.budget", "MM]", "MM\n", "M M", String(repeating: "A", count: 33)] {
            XCTAssertFalse(ListenerPartition.isSeatName(bad), bad)
        }
    }

    // MARK: A seat the Mac does not hold

    /// A section that names this instance,  or a value the eavesdropper does not take as an instance,
    /// is not that refusal,  so the partition still decides.
    func test_an_instance_key_that_is_not_the_other_instance_changes_nothing() {
        let partition = ListenerPartition(seats: ["MM": "mac", "MA": "server"])
        let mine = ListenerConfigDocument(text: "[seat.MM]\ninstance = \"mac\"\n")
        XCTAssertEqual(partition.host(of: "MM", in: mine), .mac)
        let odd = ListenerConfigDocument(text: "[seat.MM]\ninstance = \"laptop\"\n")
        XCTAssertEqual(partition.host(of: "MM", in: odd), .mac)
        let server = ListenerConfigDocument(text: "[seat.MA]\ninstance = \"server\"\n")
        XCTAssertEqual(partition.host(of: "MA", in: server), .server)
        let unread = ListenerPartition(unreadable: "It is missing.")
        guard case .unknown = unread.host(of: "MM", in: ListenerConfigDocument(text: "[seat.MM]\ninstance = \"server\"\n")) else {
            XCTFail("a partition that cannot be read still holds no seat")
            return
        }
    }

    // MARK: Why the panel will not change a seat

    /// Every host but the Mac says why the panel leaves its seat alone, and the server's reason
    /// points at the server's own settings.  The exact words are #2179's to change, so this
    /// asserts that the reason exists and names the server, not the sentence.
    func test_a_seat_the_mac_does_not_hold_says_why_it_is_read_only() {
        XCTAssertNil(ListenerSeatHost.mac.readOnlyReason)
        let hosts: [ListenerSeatHost] = [.server, .noListener, .unlisted, .markedForTheServer, .unknown("It is missing.")]
        for host in hosts {
            XCTAssertFalse((host.readOnlyReason ?? "").isEmpty, "\(host)")
        }
        XCTAssertTrue(ListenerSeatHost.server.readOnlyReason?.lowercased().contains("server") == true)
    }
}
