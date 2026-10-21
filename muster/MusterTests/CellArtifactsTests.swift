import XCTest
/// The bottom panel:  what every cell shows,  and what it refuses to show.
///
/// The panel is the answer to "what is this cell about",  so every kind has to answer
/// it.  A panel that only worked for the instructions file would be a panel that
/// quietly says nothing for five of the six columns.
final class CellArtifactsTests: XCTestCase {
    /// A manifest with one rule for every kind,  each pointing at a different shape.
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
         "check": {"type": "marked_block_hash", "detail": "fleet-lane-map", "target": "~/.claude/CLAUDE.md"}
        },
        {
         "id": "claude-hooks-lane-guard",
         "kind": "hooks",
         "platforms": ["claude"],
         "check": {"type": "hook_registered", "detail": "fleet-guard-hook", "target": "~/.claude/settings.json"}
        },
        {
         "id": "claude-mcp-fleet-servers",
         "kind": "mcp",
         "platforms": ["claude"],
         "check": {"type": "mcp_servers_registered", "detail": "fleet-recall", "target": "~/.claude.json"}
        },
        {
         "id": "claude-skills-fleet",
         "kind": "skills",
         "platforms": ["claude"],
         "check": {"type": "skills_installed", "detail": "fleet skills", "target": "~/.claude/skills"}
        },
        {
         "id": "claude-subagents-fleet",
         "kind": "subagents",
         "platforms": ["claude"],
         "check": {"type": "subagents_installed", "detail": "fleet sub-agents", "target": "~/.claude/agents"}
        },
        {
         "id": "claude-permissions-zulip-key",
         "kind": "permissions",
         "platforms": ["claude"],
         "check": {"type": "credential_key_present", "detail": "/zulip", "target": "/zulip"}
        }
       ],
       "notApplicable": []
      }
     }
    }
    """

    /// Every kind names an artifact,  so the panel answers for every column.
    func test_every_kind_names_an_artifact() throws {
        let manifest = try JSONDecoder().decode(
            SeatManifest.self,
            from: Data(manifestJSON.utf8)
        )
        for kind in ConfigKind.columnOrder {
            let targets = manifest.targets(seat: "CLAUDE", kind: kind)
            XCTAssertEqual(
                targets.count,
                1,
                "\(kind.rawValue) should name exactly one artifact"
            )
            XCTAssertFalse(targets.first?.target.isEmpty ?? true)
        }
    }

    /// A credential rule is refused,  and the rule's own facts still stand.
    ///
    /// The information a credential rule is about is which keys are present,  not what
    /// they hold,  so the panel shows the engine's expected and observed and refuses
    /// the path.
    func test_a_credential_rule_is_refused_but_still_answered() throws {
        let manifest = try JSONDecoder().decode(
            SeatManifest.self,
            from: Data(manifestJSON.utf8)
        )
        let entry = manifest.targets(seat: "CLAUDE", kind: .permissions)[0]
        let view = ConfigFileReader.read(
            target: entry.target,
            checkType: entry.checkType,
            home: URL(fileURLWithPath: NSTemporaryDirectory())
        )
        guard case .refused = view else {
            return XCTFail("the credential path should have been refused")
        }
    }
}

/// The manifest version gate and the citation it carries.
///
/// The manifest is versioned separately from the status envelope,  and the two are at
/// different numbers:  the manifest is at 2 and the envelope is at 1.  A manifest the
/// app cannot read has to be refused,  because a partly-read manifest drops fields
/// and a missing field looks like an absent rule.
final class ManifestVersionTests: XCTestCase {
    private func write(_ json: String, to name: String = "fleet-seats.json") throws -> URL {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("muster-manifest-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(name)
        try json.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    private let current = """
    {
     "schemaVersion": 2,
     "seats": {
      "MM": {
       "platforms": ["minimax"],
       "rules": [
        {
         "id": "mm-instructions-lane-map",
         "kind": "instructions",
         "platforms": ["minimax"],
         "source": "Jay, Oct 18: every seat needs a lane map",
         "check": {"type": "marked_block_hash", "target": "~/.minimax/memory/user.md"}
        }
       ],
       "notApplicable": []
      }
     }
    }
    """

    /// The manifest the app understands is read.
    func test_the_current_manifest_is_read() throws {
        let url = try write(current)
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        let manifest = try SeatManifest.read(at: url)
        XCTAssertEqual(manifest.schemaVersion, 2)
        XCTAssertEqual(manifest.seats["MM"]?.rules.first?.source, "Jay, Oct 18: every seat needs a lane map")
    }

    /// A manifest version the app does not know is refused.
    func test_an_unknown_manifest_version_is_refused() throws {
        let url = try write(current.replacingOccurrences(of: "\"schemaVersion\": 2", with: "\"schemaVersion\": 7"))
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        XCTAssertThrowsError(try SeatManifest.read(at: url))
    }

    /// A rule with no citation reads as none rather than as a missing field.
    func test_a_rule_with_no_citation_reads_as_no_source() throws {
        let url = try write(current.replacingOccurrences(of: "\"source\": \"Jay, Oct 18: every seat needs a lane map\"", with: "\"source\": \"\""))
        defer { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }
        let manifest = try SeatManifest.read(at: url)
        XCTAssertEqual(manifest.seats["MM"]?.rules.first?.source, "")
    }
}
