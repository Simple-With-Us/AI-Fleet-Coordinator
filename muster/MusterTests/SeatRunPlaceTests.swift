import XCTest

/// Where a seat runs:  the manifest's optional `runsOn` list, read leniently, and the grid's
/// place chip and row note built from it (GRID-4, GRID-5).
final class SeatRunPlaceTests: XCTestCase {
    private let gap = "\u{00A0} "

    /// One seat entry with the given `runsOn` text spliced in, or no key at all for nil.
    private func entry(runsOn: String?) throws -> SeatManifestEntry {
        let key = runsOn.map { ", \"runsOn\": \($0)" } ?? ""
        let json = "{\"platforms\": [], \"rules\": [], \"notApplicable\": []\(key)}"
        return try JSONDecoder().decode(SeatManifestEntry.self, from: Data(json.utf8))
    }

    /// A manifest shaped like the fleet's own for the seats the key matters to.
    private let manifestJSON = """
    {
     "schemaVersion": 2,
     "seats": {
      "CLAUDE": {
       "platforms": ["claude"],
       "rules": [
        {"id": "claude-hooks-lane-guard", "kind": "hooks", "platforms": ["claude"],
         "check": {"type": "hook_registered", "target": "~/.claude/settings.json"}}
       ],
       "notApplicable": [],
       "runsOn": ["mac", "server"]
      },
      "MA": {
       "platforms": ["muse"],
       "rules": [
        {"id": "ma-mcp-fleet-servers", "kind": "mcp", "platforms": ["muse"],
         "check": {"type": "mcp_servers_registered", "target": "~/.config/muse/settings.json"}}
       ],
       "notApplicable": [
        {"kind": "skills", "platforms": ["muse"], "reason": "Muse Assist runs on a cloud VM."}
       ],
       "runsOn": ["vm"]
      },
      "GROK-BOT": {
       "platforms": [],
       "rules": [],
       "notApplicable": [
        {"kind": "instructions", "platforms": [], "reason": "Grok Bot runs as Cursor cloud agents."}
       ],
       "runsOn": ["cloud"]
      },
      "OLDMAC": {
       "platforms": ["claude"],
       "rules": [
        {"id": "oldmac-hooks", "kind": "hooks", "platforms": ["claude"],
         "check": {"type": "hook_registered", "target": "~/.oldmac/settings.json"}}
       ],
       "notApplicable": []
      },
      "ECHO": {
       "platforms": [],
       "rules": [],
       "notApplicable": [
        {"kind": "instructions", "platforms": [], "reason": "Echo runs elsewhere."}
       ]
      },
      "HOMELESS": {
       "platforms": [],
       "rules": [],
       "notApplicable": [
        {"kind": "instructions", "platforms": [], "reason": "Its desk is somewhere else."}
       ],
       "runsOn": ["mac"]
      }
     }
    }
    """

    private func manifest() throws -> SeatManifest {
        try JSONDecoder().decode(SeatManifest.self, from: Data(manifestJSON.utf8))
    }

    private func location(_ seat: String) throws -> GridPresentation.SeatLocation {
        GridPresentation.SeatLocation.of(seat: seat, manifest: try manifest())
    }

    // MARK: Decoding

    /// Each of the four words reads as its own place.
    func test_each_place_word_decodes() throws {
        XCTAssertEqual(try entry(runsOn: "[\"mac\"]").runsOn, [.mac])
        XCTAssertEqual(try entry(runsOn: "[\"server\"]").runsOn, [.server])
        XCTAssertEqual(try entry(runsOn: "[\"vm\"]").runsOn, [.vm])
        XCTAssertEqual(try entry(runsOn: "[\"cloud\"]").runsOn, [.cloud])
    }

    /// A seat on several places names them all.
    func test_several_places_decode() throws {
        XCTAssertEqual(try entry(runsOn: "[\"mac\", \"server\"]").runsOn, [.mac, .server])
        XCTAssertEqual(try entry(runsOn: "[\"cloud\", \"vm\", \"mac\"]").runsOn, [.mac, .vm, .cloud])
    }

    /// A word this app does not know is skipped, and the words it knows are kept.
    func test_an_unknown_word_is_ignored() throws {
        XCTAssertEqual(try entry(runsOn: "[\"moon\", \"vm\"]").runsOn, [.vm])
        XCTAssertEqual(try entry(runsOn: "[\"Mac\"]").runsOn, [])
        XCTAssertEqual(try entry(runsOn: "[1, \"server\", null]").runsOn, [.server])
    }

    /// An empty list, a missing key, a null and a value that is not a list all mean the
    /// place is not known, and none of them stops the entry from decoding.
    func test_an_empty_or_missing_list_means_not_known() throws {
        XCTAssertEqual(try entry(runsOn: "[]").runsOn, [])
        XCTAssertEqual(try entry(runsOn: nil).runsOn, [])
        XCTAssertEqual(try entry(runsOn: "null").runsOn, [])
        XCTAssertEqual(try entry(runsOn: "\"mac\"").runsOn, [])
        XCTAssertEqual(try entry(runsOn: "{\"mac\": true}").runsOn, [])
    }

    /// The rest of the entry decodes as it did:  its fields are read, and a missing required
    /// field is still refused.
    func test_the_rest_of_the_entry_is_unchanged() throws {
        let manifest = try manifest()
        XCTAssertEqual(manifest.seats["MA"]?.platforms, ["muse"])
        XCTAssertEqual(manifest.seats["MA"]?.rules.map(\.id), ["ma-mcp-fleet-servers"])
        XCTAssertEqual(manifest.seats["MA"]?.notApplicable.map(\.kind), ["skills"])
        XCTAssertEqual(manifest.runsOn(seat: "CLAUDE"), [.mac, .server])
        XCTAssertEqual(manifest.runsOn(seat: "ECHO"), [])
        XCTAssertEqual(manifest.runsOn(seat: "NOBODY"), [])
        let noRules = "{\"platforms\": [], \"notApplicable\": [], \"runsOn\": [\"vm\"]}"
        XCTAssertThrowsError(try JSONDecoder().decode(SeatManifestEntry.self, from: Data(noRules.utf8)))
        // The shared fixture, which has no runsOn anywhere, still decodes.
        let fixture = try JSONDecoder().decode(SeatManifest.self, from: Data(Fixtures.manifest.utf8))
        XCTAssertEqual(fixture.runsOn(seat: "CLAUDE"), [])
    }

    // MARK: Place words

    /// The chip names the places in a fixed order, and says a preposition again only when
    /// it changes.
    func test_place_words_are_joined() {
        XCTAssertEqual(SeatRunPlace.title([.mac]), "On The Mac")
        XCTAssertEqual(SeatRunPlace.title([.server]), "On The Server")
        XCTAssertEqual(SeatRunPlace.title([.vm]), "On A VM")
        XCTAssertEqual(SeatRunPlace.title([.cloud]), "In The Cloud")
        XCTAssertEqual(SeatRunPlace.title([.server, .mac]), "On The Mac And The Server")
        XCTAssertEqual(SeatRunPlace.title([.cloud, .vm]), "On A VM And In The Cloud")
        XCTAssertEqual(SeatRunPlace.title([.cloud, .mac]), "On The Mac And In The Cloud")
        XCTAssertEqual(SeatRunPlace.prose([.mac, .server]), "on the Mac and the server")
        XCTAssertEqual(SeatRunPlace.prose([.vm]), "on a VM")
        XCTAssertEqual(SeatRunPlace.prose([.cloud]), "in the cloud")
        XCTAssertEqual(SeatRunPlace.title([]), "")
    }

    // MARK: The grid's chip and row note

    /// CLAUDE runs on the Mac and the server:  its chip says both, its row needs no note,
    /// and its tooltip says the server's side is not checked here.
    func test_claude_on_the_mac_and_the_server() throws {
        let place = try location("CLAUDE")
        XCTAssertEqual(place, .runsOn(places: [.mac, .server], checkedOnTheMac: true, reason: nil))
        XCTAssertEqual(place.chip, "On The Mac And The Server")
        XCTAssertNil(place.note)
        XCTAssertTrue(place.runsOnTheMac)
        XCTAssertEqual(
            place.help,
            "This seat runs on the Mac and the server.\(gap)Its config files are read on the Mac, and its config on the server is not checked here."
        )
    }

    /// MA runs on a VM and the Mac holds its settings file:  its row says where it runs and
    /// that only the Mac's part is checked.
    func test_ma_on_a_vm_with_a_mac_rule() throws {
        let place = try location("MA")
        XCTAssertEqual(place, .runsOn(places: [.vm], checkedOnTheMac: true, reason: nil))
        XCTAssertEqual(place.chip, "On A VM")
        XCTAssertEqual(place.note, "Runs on a VM, not on the Mac;  only what the Mac holds for it is checked here.")
        XCTAssertFalse(place.runsOnTheMac)
        XCTAssertEqual(place.help, "This seat runs on a VM, not on the Mac.\(gap)Only the files the Mac holds for it are read here.")
    }

    /// GROK-BOT runs in the cloud and has no rule:  its row says where it runs, then its
    /// cover's own sentence.
    func test_grok_bot_in_the_cloud() throws {
        let place = try location("GROK-BOT")
        XCTAssertEqual(
            place,
            .runsOn(places: [.cloud], checkedOnTheMac: false, reason: "Grok Bot runs as Cursor cloud agents.")
        )
        XCTAssertEqual(place.chip, "In The Cloud")
        XCTAssertEqual(
            place.note,
            "Runs in the cloud, not on the Mac, so nothing here is checked for it.\(gap)Grok Bot runs as Cursor cloud agents."
        )
        XCTAssertFalse(place.runsOnTheMac)
        XCTAssertEqual(place.help, "This seat runs in the cloud, not on the Mac, so the Mac holds none of its config.")
    }

    /// A seat said to run on the Mac with no rule says the manifest names nothing to check.
    func test_a_mac_seat_with_no_rule_says_so() throws {
        let place = try location("HOMELESS")
        XCTAssertEqual(place.chip, "On The Mac")
        XCTAssertTrue(place.runsOnTheMac)
        XCTAssertEqual(
            place.note,
            "Runs on the Mac, but the manifest names nothing on the Mac to check for it.\(gap)Its desk is somewhere else."
        )
    }

    /// A seat without the key is a seat whose place is not known, whatever its rules check:
    /// the place is never guessed from the rules (the fleet config design, section 6.1).
    func test_a_seat_without_the_key_has_no_known_place() throws {
        let withRules = try location("OLDMAC")
        XCTAssertEqual(withRules, .placeNotSaid(rulesCheckTheMac: true, reason: nil))
        XCTAssertEqual(withRules.chip, "Place Not Known")
        XCTAssertNil(withRules.note)
        XCTAssertFalse(withRules.runsOnTheMac)
        XCTAssertEqual(withRules.help, "The manifest does not say where this seat runs.\(gap)Its rules check files on the Mac.")
        XCTAssertFalse(withRules.help.contains("runs on"))

        let covered = try location("ECHO")
        XCTAssertEqual(covered, .placeNotSaid(rulesCheckTheMac: false, reason: "Echo runs elsewhere."))
        XCTAssertEqual(covered.chip, "Place Not Known")
        XCTAssertEqual(
            covered.note,
            "The manifest does not say where this seat runs, and no rule checks anything on the Mac for it.\(gap)Echo runs elsewhere."
        )
        XCTAssertFalse(covered.runsOnTheMac)
        XCTAssertEqual(covered.help, "The manifest does not say where this seat runs.\(gap)No rule checks anything on the Mac for it.")

        let unnamed = try location("NOBODY")
        XCTAssertEqual(unnamed, .notKnown)
        XCTAssertEqual(unnamed.chip, "Place Not Known")
        XCTAssertFalse(unnamed.runsOnTheMac)
    }

    // MARK: The chip's colour

    /// The grid draws a plain chip only for a seat that runs on the Mac;  every other place,
    /// and a place not known, is marked, so the colour cannot flip back silently.
    func test_chip_tone_for_every_place() throws {
        XCTAssertEqual(try location("CLAUDE").chipTone, .plain)
        XCTAssertEqual(try location("HOMELESS").chipTone, .plain)

        let serverOnly = GridPresentation.SeatLocation.runsOn(places: [.server], checkedOnTheMac: false, reason: nil)
        XCTAssertEqual(serverOnly.chip, "On The Server")
        XCTAssertEqual(serverOnly.chipTone, .marked)
        XCTAssertEqual(try location("MA").chipTone, .marked)
        XCTAssertEqual(try location("GROK-BOT").chipTone, .marked)

        XCTAssertEqual(try location("OLDMAC").chipTone, .marked)
        XCTAssertEqual(try location("ECHO").chipTone, .marked)
        XCTAssertEqual(try location("NOBODY").chipTone, .marked)
    }
}
