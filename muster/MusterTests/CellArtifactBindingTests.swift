import XCTest

/// The detail panel must show the artifact of the cell that was clicked.
///
/// Jay reported the panel showing `CLAUDE.md` for every cell,  which is the first
/// cell in the roster,  and that is a stale preview rather than a wrong artifact.
final class CellArtifactBindingTests: XCTestCase {
    /// A manifest with two seats whose instructions point at different files.
    private let manifestJSON = """
    {
     "schemaVersion": 1,
     "seats": {
      "CLAUDE": {
       "platforms": ["claude"],
       "rules": [
        {
         "id": "claude-instructions-lane-map",
         "kind": "instructions",
         "platforms": ["claude"],
         "check": {"type": "marked_block_hash", "target": "~/.claude/CLAUDE.md"}
        }
       ],
       "notApplicable": []
      },
      "MM": {
       "platforms": ["minimax"],
       "rules": [
        {
         "id": "mm-instructions-lane-map",
         "kind": "instructions",
         "platforms": ["minimax"],
         "check": {"type": "marked_block_hash", "target": "~/.minimax/memory/user.md"}
        }
       ],
       "notApplicable": []
      }
     }
    }
    """

    /// Two cells of different seats must resolve to two different artifacts.
    func test_two_cells_resolve_to_two_different_artifacts() throws {
        let manifest = try JSONDecoder().decode(
            SeatManifest.self,
            from: Data(manifestJSON.utf8)
        )
        let home = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-bind-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent(".claude", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: home.appendingPathComponent(".minimax/memory", isDirectory: true),
            withIntermediateDirectories: true
        )
        try "claude file".write(
            to: home.appendingPathComponent(".claude/CLAUDE.md"),
            atomically: true,
            encoding: .utf8
        )
        try "minimax file".write(
            to: home.appendingPathComponent(".minimax/memory/user.md"),
            atomically: true,
            encoding: .utf8
        )
        defer { try? FileManager.default.removeItem(at: home) }

        let claude = manifest.targets(seat: "CLAUDE", kind: .instructions)
        let minimax = manifest.targets(seat: "MM", kind: .instructions)
        XCTAssertEqual(claude.first?.target, "~/.claude/CLAUDE.md")
        XCTAssertEqual(minimax.first?.target, "~/.minimax/memory/user.md")

        // The reader resolves each target to its own file.
        let claudeView = ConfigFileReader.read(
            target: claude[0].target,
            checkType: claude[0].checkType,
            home: home
        )
        let minimaxView = ConfigFileReader.read(
            target: minimax[0].target,
            checkType: minimax[0].checkType,
            home: home
        )
        guard case .file(_, let claudeText, _, _) = claudeView,
              case .file(_, let minimaxText, _, _) = minimaxView else {
            return XCTFail("both files should have been read")
        }
        XCTAssertEqual(claudeText, "claude file")
        XCTAssertEqual(minimaxText, "minimax file")
    }
}
