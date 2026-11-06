import XCTest

/// Grok Bots are a group of seats, each named `GB-<NAME>`.  No seat is ever called
/// `GROK-BOT`, so no list of seats in the app may show it as one.
final class NotASeatTests: XCTestCase {
    private func manifestRegistry(_ tags: [(String, Bool)]) throws -> FleetRegistry {
        let rows = tags
            .map { "{\"tag\": \"\($0.0)\"\($0.1 ? ", \"retired\": true" : "")}" }
            .joined(separator: ",")
        let json = "{\"seats\": [\(rows)]}"
        return try JSONDecoder().decode(FleetRegistry.self, from: Data(json.utf8))
    }

    func test_the_group_tag_is_never_on_the_roster() throws {
        let registry = try manifestRegistry([
            ("CLAUDE", false), ("GROK-BOT", false), ("GB-FIXER", false), ("CURSOR", false),
        ])
        XCTAssertEqual(registry.liveTags, ["CLAUDE", "GB-FIXER", "CURSOR"])
    }

    func test_the_named_members_of_the_group_stay_on_the_roster() throws {
        let registry = try manifestRegistry([("GB-FIXER", false), ("GB-DIRECTOR", false)])
        XCTAssertEqual(registry.liveTags, ["GB-FIXER", "GB-DIRECTOR"])
    }

    func test_only_the_group_tag_is_set_aside() {
        XCTAssertTrue(NotASeat.contains("GROK-BOT"))
        for seat in ["GB-FIXER", "GROK", "GROK-WEB", "CURSOR", "MM"] {
            XCTAssertFalse(NotASeat.contains(seat), seat)
        }
    }

    func test_the_app_row_is_named_for_the_group() throws {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Muster/Model/FleetRegistry.swift")
        let source = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(source.contains("title: \"Grok Bots\""))
        XCTAssertFalse(source.contains("title: \"Grok Bot\""))
    }
}
