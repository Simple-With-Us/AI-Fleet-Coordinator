import XCTest

/// The README's panel table and the app's own list of places.
///
/// `muster/README.md` carries one table row per place in the sidebar:  which machine the
/// panel describes, where its data comes from, how fresh it is and what it can change.  This
/// keeps that table and `MusterDestination` the same list, so a panel cannot be added,
/// renamed or removed without the README saying so.
final class ReadmeSurfaceTests: XCTestCase {
    /// The heading the table sits under.
    static let heading = "## Every Panel"

    /// The README, found from this file's own path:  the tests run from the checkout.
    private func readme() throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("README.md")
        return try String(contentsOf: url, encoding: .utf8)
    }

    /// The first cell of every body row of the first table under the heading.
    ///
    /// The header row and the rule under it are left out.  A section with no table gives
    /// nothing,  so a missing table fails every title rather than passing.
    static func panelNames(in text: String) -> [String] {
        let lines = text.components(separatedBy: "\n")
        guard let start = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == heading }) else {
            return []
        }
        var tableRows: [String] = []
        for line in lines[(start + 1)...] {
            if line.hasPrefix("## ") { break }
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("|") {
                tableRows.append(trimmed)
            } else if !tableRows.isEmpty {
                break
            }
        }
        return tableRows.dropFirst(2).compactMap { row in
            let cells = row.split(separator: "|", omittingEmptySubsequences: false)
            guard cells.count > 1 else { return nil }
            return cells[1].trimmingCharacters(in: .whitespaces)
        }
    }

    func test_every_place_has_exactly_one_row() throws {
        let names = Self.panelNames(in: try readme())
        let titles = MusterDestination.allCases.map(\.title)
        let missing = titles.filter { !names.contains($0) }
        XCTAssertTrue(missing.isEmpty, "README.md \(Self.heading) has no row for: \(missing)")
        let unknown = names.filter { !titles.contains($0) }
        XCTAssertTrue(unknown.isEmpty, "README.md \(Self.heading) names no place the app has: \(unknown)")
        XCTAssertEqual(names.count, Set(names).count, "README.md \(Self.heading) names a place twice: \(names)")
    }

    /// The parser reads the first cell of body rows only,  and stops at the next heading.
    func test_parser_reads_body_rows_under_the_heading_only() {
        let text = """
        # Muster

        | Panel | About |
        | --- | --- |
        | Not This One | x |

        ## Every Panel

        Words before the table.

        | Panel | About |
        | --- | --- |
        | Overview | The Mac |
        | Apps & Work | Across the fleet |

        ## Next

        | Panel | About |
        | --- | --- |
        | Nor This One | x |
        """
        XCTAssertEqual(Self.panelNames(in: text), ["Overview", "Apps & Work"])
    }

    /// A README with no such heading names nothing,  so the real test fails loudly.
    func test_missing_heading_names_nothing() {
        XCTAssertEqual(Self.panelNames(in: "# Muster\n\n| Panel |\n| --- |\n| Overview |\n"), [])
    }
}
