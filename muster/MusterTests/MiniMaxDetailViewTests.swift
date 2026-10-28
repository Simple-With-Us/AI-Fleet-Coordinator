import XCTest

/// The MiniMax panel's agents row:  what the seat's own agent is called there.
///
/// The MiniMax runtime names the seat's own agent `mavis`, while the fleet calls that
/// seat `MM`, so a row that said only "this seat" named a runtime persona and never the
/// fleet's own tag for it.  The row reads `mavis (MM, this seat)`, and every other
/// agent keeps its bare runtime name.
///
/// The row is built in `MiniMaxDetailView`, and that file is a SwiftUI view the logic
/// test bundle does not compile, so the row is read out of the view's own source and
/// rendered here.  The read is anchored on the agents table and takes that table's own
/// first cell, so a copy of the expression left in a comment is never mistaken for it.
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
    /// A missing view is a failure and not a skip:  the view moved or was renamed, and
    /// a skipped test would leave the panel's copy unguarded while CI stays green.
    private static func viewSource() throws -> String {
        // .../muster/MusterTests/MiniMaxDetailViewTests.swift -> repo root is three levels up
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let url = root.appendingPathComponent("muster/Muster/Views/MiniMaxDetailView.swift")
        try XCTUnwrap(
            FileManager.default.fileExists(atPath: url.path) ? url : nil,
            "the MiniMax view moved:  muster/Muster/Views/MiniMaxDetailView.swift is missing"
        )
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// The view's source with its comments removed.
    ///
    /// A commented-out copy of the label must never be mistaken for the label the
    /// panel renders, so the comments go before the row is looked for.
    private static func strippingComments(_ source: String) -> String {
        var out = ""
        var characters = Array(source)
        var index = 0
        var inString = false
        var inLineComment = false
        var inBlockComment = false
        while index < characters.count {
            let character = characters[index]
            let next = index + 1 < characters.count ? characters[index + 1] : "\0"
            if inLineComment {
                if character == "\n" { inLineComment = false; out.append(character) }
                index += 1
                continue
            }
            if inBlockComment {
                if character == "*" && next == "/" { inBlockComment = false; index += 2; continue }
                index += 1
                continue
            }
            if inString {
                out.append(character)
                if character == "\\" {
                    // An escape, or the start of an interpolated expression.
                    if next == "(" {
                        var depth = 1
                        out.append(next)
                        index += 2
                        while index < characters.count && depth > 0 {
                            let inner = characters[index]
                            if inner == "(" { depth += 1 }
                            if inner == ")" { depth -= 1 }
                            out.append(inner)
                            index += 1
                        }
                        continue
                    }
                    out.append(next)
                    index += 2
                    continue
                }
                if character == "\"" { inString = false }
                index += 1
                continue
            }
            if character == "/" && next == "/" {
                inLineComment = true
                index += 2
                continue
            }
            if character == "/" && next == "*" {
                inBlockComment = true
                index += 2
                continue
            }
            if character == "\"" { inString = true }
            out.append(character)
            index += 1
        }
        return out
    }

    /// The index just past the string literal that opens at `index`.
    ///
    /// An interpolated expression inside the literal is skipped whole, so a parenthesis
    /// or a colon in one of them is never read as the end of the literal.
    private static func endOfStringLiteral(_ characters: [Character], _ index: Int) -> Int {
        var cursor = index + 1
        while cursor < characters.count {
            let character = characters[cursor]
            if character == "\\" {
                if cursor + 1 < characters.count, characters[cursor + 1] == "(" {
                    var depth = 1
                    cursor += 2
                    while cursor < characters.count, depth > 0 {
                        if characters[cursor] == "(" { depth += 1 }
                        if characters[cursor] == ")" { depth -= 1 }
                        cursor += 1
                    }
                    continue
                }
                cursor += 2
                continue
            }
            if character == "\"" { return cursor + 1 }
            cursor += 1
        }
        return characters.count
    }

    /// The row's own label expression, as the view writes it.
    ///
    /// The agents table is the one headed `Agent`, and its first cell is the agent's
    /// name, so the expression is taken from that cell and from nowhere else.
    private static func agentRowExpression() throws -> String {
        let source = strippingComments(try viewSource())
        guard let table = source.range(of: #"GridRowHeader(labels: ["Agent""#) else {
            XCTFail("the MiniMax view no longer has an agents table")
            return ""
        }
        let agentsTable = source[table.upperBound...]
        guard let cell = agentsTable.range(of: "GridCellText(") else {
            XCTFail("the MiniMax view's agents table has no agent cell")
            return ""
        }
        let cellBody = agentsTable[cell.upperBound...]
        guard let value = cellBody.range(of: "value:") else {
            XCTFail("the MiniMax view's agent cell has no value")
            return ""
        }
        let afterValue = Array(cellBody[value.upperBound...])
        // The cell's own closing parenthesis ends the expression.
        var depth = 1
        var index = 0
        var end = afterValue.count
        while index < afterValue.count {
            let character = afterValue[index]
            if character == "\"" {
                index = endOfStringLiteral(afterValue, index)
                continue
            }
            if character == "(" { depth += 1 }
            if character == ")" {
                depth -= 1
                if depth == 0 { end = index; break }
            }
            index += 1
        }
        return String(afterValue[..<end]).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The two branches of the agents row's label, and the condition that picks one.
    private struct RowLabel {
        let condition: String
        let seated: String
        let other: String
    }

    /// The row's label, split into its condition and its two branches.
    ///
    /// The split looks only outside the string literals, so a `?` or a `:` inside one
    /// of them does not cut the expression in half.
    private static func rowLabel() throws -> RowLabel {
        let expression = try agentRowExpression()
        let characters = Array(expression)
        var question = -1
        var colon = -1
        var index = 0
        while index < characters.count {
            let character = characters[index]
            if character == "\"" {
                index = endOfStringLiteral(characters, index)
                continue
            }
            if character == "?", question < 0 { question = index }
            if character == ":", question >= 0, colon < 0 { colon = index; break }
            index += 1
        }
        guard question >= 0, colon > question else {
            XCTFail("the MiniMax view's agent cell does not choose between two labels")
            return RowLabel(condition: "", seated: "", other: "")
        }
        return RowLabel(
            condition: String(characters[..<question]).trimmingCharacters(in: .whitespacesAndNewlines),
            seated: String(characters[(question + 1)..<colon]).trimmingCharacters(in: .whitespacesAndNewlines),
            other: String(characters[(colon + 1)...]).trimmingCharacters(in: .whitespacesAndNewlines)
        )
    }

    /// Fill a label's holes with an agent's name and a seat's tag.
    ///
    /// The interpolated forms come first, because the bare name sits inside them.
    private static func render(_ source: String, name: String, seatTag: String) -> String {
        var text = source
        if text.hasPrefix("\"") && text.hasSuffix("\"") && text.count >= 2 {
            text = String(text.dropFirst().dropLast())
        }
        return text
            .replacingOccurrences(of: #"\(agent.name)"#, with: name)
            .replacingOccurrences(of: #"\(seat)"#, with: seatTag)
            .replacingOccurrences(of: "agent.name", with: name)
    }

    /// The seat tag the view holds, read from the view's own source.
    private static func seatTagInView() throws -> String {
        let source = strippingComments(try viewSource())
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

        let label = try Self.rowLabel()
        XCTAssertEqual(label.condition, "agent.ownsSeatMemory")

        // The seat's own agent:  the runtime name, the fleet tag, and the seat marker.
        XCTAssertEqual(
            Self.render(label.seated, name: seatAgent.name, seatTag: seatTag),
            "mavis (MM, this seat)"
        )

        // Every other agent keeps its bare runtime name.
        XCTAssertEqual(
            Self.render(label.other, name: otherAgent.name, seatTag: seatTag),
            "worker"
        )

        // The tag is the view's own seat and not a letter written into the label.
        XCTAssertEqual(
            Self.render(label.seated, name: seatAgent.name, seatTag: "ZZ"),
            "mavis (ZZ, this seat)"
        )
    }
}
