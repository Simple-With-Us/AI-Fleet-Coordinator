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
