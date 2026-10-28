import XCTest

/// The MiniMax panel's agents row:  what the seat's own agent is called there.
///
/// The MiniMax runtime names the seat's own agent `mavis`, while the fleet calls that
/// seat `MM`, so a row that said only "this seat" named a runtime persona and never the
/// fleet's own tag for it.  The row reads `mavis (MM, this seat)`, and every other
/// agent keeps its bare runtime name.
final class MiniMaxDetailViewTests: XCTestCase {
    /// A home directory holding a MiniMax surface, built in a temporary directory.
    ///
    /// The same shape the surface reader tests use:  one agent holding the durable
    /// `memory/` tree, and one agent that does not.
    private func makeHome() throws -> URL {
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-mm-view-\(UUID().uuidString)", isDirectory: true)
        let root = home.appendingPathComponent(".minimax", isDirectory: true)
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("agents/mavis/memory", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent("agents/worker", isDirectory: true),
            withIntermediateDirectories: true
        )
        for name in ["mavis", "worker"] {
            try "the seat prompt\n".write(
                to: root.appendingPathComponent("agents/\(name)/agent.md"),
                atomically: true,
                encoding: .utf8
            )
        }
        return home
    }

    /// The view's own source, read from the checkout the tests run in.
    ///
    /// The row is built in `MiniMaxDetailView`, and that file is a SwiftUI view the
    /// logic test bundle does not compile, so the row is read out of the view's own
    /// source.  The test therefore fails on any change to the string the panel shows.
    private static func viewSource() throws -> String {
        // .../muster/MusterTests/MiniMaxDetailViewTests.swift -> repo root is three levels up
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let url = root.appendingPathComponent("muster/Muster/Views/MiniMaxDetailView.swift")
        try XCTSkipUnless(
            FileManager.default.fileExists(atPath: url.path),
            "no checkout of the MiniMax view"
        )
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// The two branches of the agents row's label, as the view writes them.
    ///
    /// The first branch is the quoted label of the agent that owns the seat's memory,
    /// and the second is what every other agent gets.
    private static func agentRowBranches() throws -> (seated: String, other: String) {
        let source = try viewSource()
        guard let row = source.range(of: "agent.ownsSeatMemory ? ") else {
            XCTFail("the MiniMax view no longer builds the agents row from ownsSeatMemory")
            return ("", "")
        }
        let afterRow = source[row.upperBound...]
        guard let open = afterRow.firstIndex(of: "\""),
              let close = afterRow[afterRow.index(after: open)...].firstIndex(of: "\"") else {
            XCTFail("the MiniMax view's agents row has no quoted label")
            return ("", "")
        }
        let seated = String(afterRow[afterRow.index(after: open)..<close])
        let afterQuote = afterRow[close...]
        guard let colon = afterQuote.range(of: " : ") else {
            XCTFail("the MiniMax view's agents row has no second branch")
            return ("", "")
        }
        let other = String(
            afterQuote[colon.upperBound...].prefix(while: { $0 != "\n" })
        ).trimmingCharacters(in: .whitespaces)
        return (seated, other)
    }

    /// Fill a label's holes with an agent's name and a seat's tag.
    ///
    /// The interpolated forms come first, because the bare name sits inside them.
    private static func render(_ source: String, name: String, seatTag: String) -> String {
        source
            .replacingOccurrences(of: #"\(agent.name)"#, with: name)
            .replacingOccurrences(of: #"\(seat)"#, with: seatTag)
            .replacingOccurrences(of: "agent.name", with: name)
    }

    /// The seat tag the view holds, read from the view's own source.
    private static func seatTagInView() throws -> String {
        let source = try viewSource()
        guard let seat = source.range(of: "private let seat = \"") else {
            XCTFail("the MiniMax view no longer holds a seat tag")
            return ""
        }
        let afterSeat = source[seat.upperBound...]
        guard let close = afterSeat.firstIndex(of: "\"") else {
            XCTFail("the MiniMax view's seat tag is unterminated")
            return ""
        }
        return String(afterSeat[..<close])
    }

    /// The seat's own agent names the fleet seat beside the runtime name, and every
    /// other agent keeps its bare runtime name.
    ///
    /// The row used to say "this seat" on its own, which named a runtime persona and
    /// never the fleet's tag for it, so the row now reads `mavis (MM, this seat)`.
    func test_the_agents_row_names_the_fleet_seat_beside_the_runtime_name() throws {
        let home = try makeHome()
        defer { try? FileManager.default.removeItem(at: home) }
        let surface = MiniMaxReader.read(home: home, liveSeats: ["MM"])
        let seatAgent = try XCTUnwrap(surface.agents.first { $0.ownsSeatMemory })
        let otherAgent = try XCTUnwrap(surface.agents.first { !$0.ownsSeatMemory })
        XCTAssertEqual(seatAgent.name, "mavis")
        XCTAssertEqual(otherAgent.name, "worker")

        // The seat tag is the view's own, so the label and the panel cannot disagree.
        let seatTag = try Self.seatTagInView()
        XCTAssertEqual(seatTag, "MM")

        let branches = try Self.agentRowBranches()

        // The seat's own agent:  the runtime name, the fleet tag, and the seat marker.
        XCTAssertEqual(
            Self.render(branches.seated, name: seatAgent.name, seatTag: seatTag),
            "mavis (MM, this seat)"
        )

        // Every other agent keeps its bare runtime name.
        XCTAssertEqual(
            Self.render(branches.other, name: otherAgent.name, seatTag: seatTag),
            "worker"
        )

        // The tag is the view's own seat and not a letter written into the label.
        XCTAssertEqual(
            Self.render(branches.seated, name: seatAgent.name, seatTag: "ZZ"),
            "mavis (ZZ, this seat)"
        )
    }
}
