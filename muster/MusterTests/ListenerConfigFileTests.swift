import XCTest

/// The listener's configuration file:  what it reads,  and what a write leaves alone.
final class ListenerConfigFileTests: XCTestCase {
    /// A file like the listener's own.
    private let sample = """
    [daemon]
    owner_user_id = 4242  # pinned by `simple-chat daemon init`
    host_wake_load_per_core = 0
    outgoing_webhook_seats = ["BF-BUILDER", "BF-COMPILER"]

    [seat.MM]
    bot = "MM"
    wake = "muse"
    model = "muse-spark-1.3-contributor"
    muse = "/tmp/fixture-apps/muse-bin"
    fallback = "mcode"
    fallback_uncontained_ok = true
    budget = { wakes_per_hour = 5, wakes_per_day = 80, per_topic_per_hour = 6, owner_per_day = 40, owner_per_topic_per_hour = 12, usd_per_day = 4.0 }
    live = { per_hour = 6, per_day = 30, per_topic_minutes = 5, owner_per_day = 20, loop_turns = 3 }
    """

    /// A scalar in a section is read.
    func test_it_reads_a_scalar_from_a_section() {
        let document = ListenerConfigDocument(text: sample)
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "wake"),
            .text("muse")
        )
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "fallback_uncontained_ok"),
            .bool(true)
        )
    }

    /// A value inside an inline table is read.
    func test_it_reads_a_value_from_an_inline_table() {
        let document = ListenerConfigDocument(text: sample)
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_hour"]),
            .number(5)
        )
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "live", subPath: ["loop_turns"]),
            .number(3)
        )
        XCTAssertEqual(
            document.value(section: "seat.MM", key: "budget", subPath: ["usd_per_day"]),
            .number(4)
        )
    }

    /// A comment after a value is not part of the value.
    func test_it_leaves_a_comment_out_of_the_value() {
        let document = ListenerConfigDocument(text: sample)
        XCTAssertEqual(
            document.value(section: "daemon", key: "owner_user_id"),
            .number(4242)
        )
    }

    /// A list of strings is read as a list.
    func test_it_reads_a_list_of_strings() {
        let document = ListenerConfigDocument(text: sample)
        XCTAssertEqual(
            document.value(section: "daemon", key: "outgoing_webhook_seats"),
            .list([.text("BF-BUILDER"), .text("BF-COMPILER")])
        )
    }

    /// A value the file does not hold is nothing,  and not a guess.
    func test_a_value_the_file_does_not_hold_is_nothing() {
        let document = ListenerConfigDocument(text: sample)
        XCTAssertNil(document.value(section: "seat.MM", key: "provider"))
        XCTAssertNil(document.value(section: "seat.MM", key: "budget", subPath: ["board_per_day"]))
        XCTAssertNil(document.value(section: "seat.NOPE", key: "wake"))
    }

    /// A section header may carry a comment,  as the listener's own file does.
    func test_a_section_header_with_a_comment_is_read() {
        let document = ListenerConfigDocument(text: """
        [seat.GROK]                       # terminal Grok and Grok Build are one seat
        wake = "inbox"
        """)
        XCTAssertEqual(document.problems, [])
        XCTAssertEqual(document.value(section: "seat.GROK", key: "wake"), .text("inbox"))
    }

    /// A line the app cannot read is reported,  and never guessed at.
    func test_a_line_it_cannot_read_is_reported() {
        let document = ListenerConfigDocument(text: "[seat.MM]\nthis is not toml\n")
        XCTAssertEqual(document.problems.count, 1)
        XCTAssertNil(document.value(section: "seat.MM", key: "this"))
    }

    /// A write changes one value,  and leaves the comment and the rest of the file alone.
    func test_a_write_changes_one_value_and_leaves_the_rest() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.setting(
            section: "daemon",
            key: "owner_user_id",
            to: .number(999)
        )
        XCTAssertTrue(written.contains("owner_user_id = 999  # pinned by `simple-chat daemon init`"))
        XCTAssertTrue(written.contains("host_wake_load_per_core = 0"))
        XCTAssertTrue(written.contains("wake = \"muse\""))
    }

    /// A write inside an inline table keeps the table's other keys.
    func test_a_write_inside_an_inline_table_keeps_the_other_keys() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.setting(
            section: "seat.MM",
            key: "budget",
            subPath: ["wakes_per_hour"],
            to: .number(9)
        )
        XCTAssertTrue(written.contains("wakes_per_hour = 9"))
        XCTAssertTrue(written.contains("wakes_per_day = 80"))
        XCTAssertTrue(written.contains("usd_per_day = 4.0"))
        XCTAssertTrue(written.contains("live = { per_hour = 6"))
    }

    /// A key the section does not have is added to it.
    func test_a_write_adds_a_key_to_a_section() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.setting(
            section: "seat.MM",
            key: "provider",
            to: .text("MiniMax")
        )
        let reread = ListenerConfigDocument(text: written)
        XCTAssertEqual(reread.value(section: "seat.MM", key: "provider"), .text("MiniMax"))
        XCTAssertEqual(reread.value(section: "seat.MM", key: "wake"), .text("muse"))
    }

    /// A section the file does not have is added at the end.
    func test_a_write_adds_a_section_that_is_not_there() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.setting(
            section: "seat.NEW",
            key: "budget",
            subPath: ["wakes_per_hour"],
            to: .number(6)
        )
        let reread = ListenerConfigDocument(text: written)
        XCTAssertEqual(
            reread.value(section: "seat.NEW", key: "budget", subPath: ["wakes_per_hour"]),
            .number(6)
        )
        XCTAssertEqual(reread.value(section: "seat.MM", key: "wake"), .text("muse"))
    }

    /// A cap a table does not hold yet is added to it,  and the rest of the line stays.  The exact
    /// lines here are parsed by `tomllib` in `scripts/agent_sync/tests/test_check_config.py`.
    func test_a_write_adds_a_key_to_an_existing_table() throws {
        let document = ListenerConfigDocument(text: """
        [seat.MM]
        budget = { wakes_per_hour = 5, wakes_per_day = 80 }  # the Mac's caps
        wake = "muse"
        """)
        let written = try document.setting(
            section: "seat.MM",
            key: "budget",
            subPath: ["owner_per_day"],
            to: .number(9)
        )
        XCTAssertEqual(written, """
        [seat.MM]
        budget = { wakes_per_hour = 5, wakes_per_day = 80, owner_per_day = 9 }  # the Mac's caps
        wake = "muse"
        """)
        let reread = ListenerConfigDocument(text: written)
        XCTAssertEqual(reread.problems, [])
        XCTAssertEqual(reread.value(section: "seat.MM", key: "budget", subPath: ["owner_per_day"]), .number(9))
        XCTAssertEqual(reread.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_hour"]), .number(5))
    }

    /// An empty table takes its first key with no stray comma.
    func test_a_write_adds_a_key_to_an_empty_table() throws {
        let document = ListenerConfigDocument(text: "[seat.MM]\nbudget = {}\n")
        let written = try document.setting(
            section: "seat.MM",
            key: "budget",
            subPath: ["owner_per_day"],
            to: .number(9)
        )
        XCTAssertEqual(written, "[seat.MM]\nbudget = { owner_per_day = 9 }\n")
    }

    /// Clearing a cap and setting it again works,  which is how a partial table comes about.
    func test_a_cap_can_be_cleared_and_set_again() throws {
        let document = ListenerConfigDocument(text: sample)
        let cleared = try document.removing(section: "seat.MM", key: "budget", subPath: ["owner_per_day"])
        let written = try ListenerConfigDocument(text: cleared).setting(
            section: "seat.MM",
            key: "budget",
            subPath: ["owner_per_day"],
            to: .number(7)
        )
        let reread = ListenerConfigDocument(text: written)
        XCTAssertEqual(reread.problems, [])
        XCTAssertEqual(reread.value(section: "seat.MM", key: "budget", subPath: ["owner_per_day"]), .number(7))
        XCTAssertEqual(reread.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_day"]), .number(80))
    }

    /// A key that is not a table is still refused,  and now the message is true.
    func test_a_write_into_a_key_that_is_not_a_table_is_refused() {
        let document = ListenerConfigDocument(text: "[seat.MM]\nbudget = 5\n")
        XCTAssertThrowsError(
            try document.setting(section: "seat.MM", key: "budget", subPath: ["owner_per_day"], to: .number(9))
        ) { error in
            XCTAssertEqual(error as? ListenerConfigDocument.WriteError, .notATable("seat.MM.budget"))
        }
    }

    /// A key held under its own header is not defined a second time.
    func test_a_write_to_a_key_under_its_own_header_is_refused() {
        let document = ListenerConfigDocument(text: "[seat.MM]\nbot = \"MM\"\n\n[seat.MM.budget]\nwakes_per_hour = 5\n")
        XCTAssertThrowsError(
            try document.setting(section: "seat.MM", key: "budget", subPath: ["owner_per_day"], to: .number(9))
        ) { error in
            XCTAssertEqual(error as? ListenerConfigDocument.WriteError, .heldInItsOwnSection("seat.MM.budget"))
        }
    }

    /// A problem belongs to the section it is in,  so one bad line does not block another seat.
    func test_a_problem_belongs_to_its_section() {
        let document = ListenerConfigDocument(text: """
        [seat.MM]
        budget.wakes_per_hour = 5

        [seat.MC]
        wake = "inbox"
        """)
        XCTAssertEqual(document.problems.count, 1)
        XCTAssertEqual(document.sections(ofSeat: "MM").flatMap(\.problems), document.problems)
        XCTAssertEqual(document.sections(ofSeat: "MC").flatMap(\.problems), [])
    }

    /// A dotted header is a table inside a seat,  not a seat of its own.
    func test_a_dotted_header_is_not_a_seat() {
        let document = ListenerConfigDocument(text: """
        [daemon]
        instance = "mac"
        [seat.MM]
        wake = "muse"
        [seat.MM.held_back]
        catch_up = true
        [seat.GROK-WEB]
        wake = "inbox"
        """)
        XCTAssertEqual(document.seatTags, ["MM", "GROK-WEB"])
        XCTAssertEqual(document.sections(ofSeat: "MM").map(\.name), ["seat.MM", "seat.MM.held_back"])
        XCTAssertEqual(document.sections(ofSeat: "M").map(\.name), [])
    }

    /// A seat's section goes,  with its tables,  and every other line stays.
    func test_removing_a_seat_leaves_every_other_line() {
        let document = ListenerConfigDocument(text: """
        [daemon]
        instance = "mac"

        [seat.MA]
        # a note inside the section
        budget = { wakes_per_hour = 9 }

        [seat.MA.held_back]
        catch_up = true

        # the Mac seats
        [seat.MM]
        wake = "muse"  # keep this comment
        """)
        XCTAssertEqual(
            document.removingSeat("MA"),
            "[daemon]\ninstance = \"mac\"\n\n\n\n# the Mac seats\n[seat.MM]\nwake = \"muse\"  # keep this comment"
        )
    }

    /// A section with lines this app cannot read still goes,  since the whole section is what the
    /// listener refuses,  and a seat the file does not have changes nothing.
    func test_removing_a_seat_takes_lines_it_could_not_read() {
        let text = "[seat.MM]\nwake = \"muse\"\n\n[seat.MA]\nthis is not toml\n"
        let document = ListenerConfigDocument(text: text)
        XCTAssertEqual(document.removingSeat("MA"), "[seat.MM]\nwake = \"muse\"\n\n")
        XCTAssertEqual(document.removingSeat("NOPE"), text)
    }

    /// Every control character is escaped,  and a carriage return with a line feed is not one
    /// character that slips past.  The exact text is parsed by `tomllib` in the Python tests.
    func test_text_is_quoted_so_the_file_stays_valid() {
        let value = ListenerConfigValue.text("a\r\nb\u{01}c\u{7F}d\te")
        XCTAssertEqual(value.tomlText, #""a\r\nb\u0001c\u007Fd\te""#)
        XCTAssertEqual(ListenerConfigValue.text(#"q"b\s"#).tomlText, #""q\"b\\s""#)
        XCTAssertEqual(ListenerConfigValue.text("é").tomlText, "\"é\"")
    }

    /// What the writer quotes,  the reader reads back.
    func test_quoted_text_reads_back() {
        let original = "a\r\nb\u{01}c\u{7F}d\te\u{08}f\u{0C}g"
        let document = ListenerConfigDocument(
            text: "[s]\nkey = \(ListenerConfigValue.text(original).tomlText)\n"
        )
        XCTAssertEqual(document.problems, [])
        XCTAssertEqual(document.value(section: "s", key: "key"), .text(original))
        let long = ListenerConfigDocument(text: "[s]\nkey = \"\\U0001F600\"\nbad = \"\\uZZZZ\"\n")
        XCTAssertEqual(long.value(section: "s", key: "key"), .text("\u{1F600}"))
        XCTAssertEqual(long.problems.count, 1)
    }

    /// A removal takes one key out of an inline table,  and leaves the rest.
    func test_a_removal_takes_one_key_out_of_an_inline_table() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.removing(
            section: "seat.MM",
            key: "budget",
            subPath: ["usd_per_day"]
        )
        let reread = ListenerConfigDocument(text: written)
        XCTAssertNil(reread.value(section: "seat.MM", key: "budget", subPath: ["usd_per_day"]))
        XCTAssertEqual(
            reread.value(section: "seat.MM", key: "budget", subPath: ["wakes_per_hour"]),
            .number(5)
        )
    }

    /// A removal takes a whole key out of the section.
    func test_a_removal_takes_a_whole_key_out() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.removing(section: "seat.MM", key: "fallback")
        let reread = ListenerConfigDocument(text: written)
        XCTAssertNil(reread.value(section: "seat.MM", key: "fallback"))
        XCTAssertEqual(reread.value(section: "seat.MM", key: "wake"), .text("muse"))
    }

    /// Removing the last key of an inline table takes the comma with it.
    ///
    /// The comma has to go inside the removed range.  Left behind it becomes a trailing
    /// comma,  and TOML 1.0 forbids one in an inline table,  so `tomllib.load` would
    /// reject the whole file and the listener would fall back to an empty config.
    func test_a_removal_of_the_last_inline_key_leaves_no_trailing_comma() throws {
        let document = ListenerConfigDocument(text: sample)
        let written = try document.removing(
            section: "seat.MM",
            key: "live",
            subPath: ["loop_turns"]
        )
        XCTAssertFalse(
            written.contains("owner_per_day = 20, }"),
            "the last key went but its comma stayed: \(written)"
        )
        // The rest of the table is intact.
        XCTAssertTrue(written.contains("live = { per_hour = 6"))
        XCTAssertTrue(written.contains("owner_per_day = 20 }"))
        let reread = ListenerConfigDocument(text: written)
        XCTAssertNil(reread.value(section: "seat.MM", key: "live", subPath: ["loop_turns"]))
        XCTAssertEqual(
            reread.value(section: "seat.MM", key: "live", subPath: ["per_hour"]),
            .number(6)
        )
        // Nothing outside the table moved.
        XCTAssertEqual(reread.value(section: "seat.MM", key: "wake"), .text("muse"))
    }

    /// Removing a key the file does not hold changes nothing,  and is not an error.
    ///
    /// The panel seeds an unset setting's field with a dash,  so clearing one is a
    /// legitimate "remove this" gesture.  Throwing there aborted the whole Apply and threw
    /// away every other edit in the same batch.
    func test_a_removal_of_a_key_the_file_lacks_is_a_no_op() throws {
        let document = ListenerConfigDocument(text: sample)
        XCTAssertEqual(try document.removing(section: "seat.MM", key: "provider"), sample)
        XCTAssertEqual(try document.removing(section: "seat.NOPE", key: "wake"), sample)
        // A key that exists but not at that sub-path leaves the file alone too.
        XCTAssertEqual(
            try document.removing(section: "seat.MM", key: "budget", subPath: ["board_per_day"]),
            sample
        )
    }

    /// A batch of edits keeps the ones that land when one of them removes a missing key.
    func test_a_batch_keeps_its_edits_when_one_removes_a_missing_key() throws {
        let document = ListenerConfigDocument(text: sample)
        var written = try document.removing(section: "seat.MM", key: "provider")
        written = try ListenerConfigDocument(text: written).setting(
            section: "seat.MM",
            key: "wake",
            to: .text("claude")
        )
        let reread = ListenerConfigDocument(text: written)
        XCTAssertEqual(reread.value(section: "seat.MM", key: "wake"), .text("claude"))
    }

    /// A cap is never negative,  and never not-a-number.
    func test_a_cap_that_is_negative_or_not_finite_is_not_written() {
        XCTAssertNil(WakeSettingKey.wakesPerHour.value(from: "-1"))
        XCTAssertNil(WakeSettingKey.usdPerDay.value(from: "-0.5"))
        XCTAssertNil(WakeSettingKey.liveLoopTurns.value(from: "nan"))
        XCTAssertNil(WakeSettingKey.liveLoopTurns.value(from: "inf"))
        // Zero is a real cap.
        XCTAssertEqual(WakeSettingKey.wakesPerHour.value(from: "0"), .number(0))
    }

    /// Two numbers with the same value are the same number,  whatever they are written as.
    func test_two_numbers_with_the_same_value_match() {
        XCTAssertTrue(ListenerConfigValue.number(5).matches(.number(5.0)))
        XCTAssertFalse(ListenerConfigValue.number(5).matches(.number(6)))
        XCTAssertTrue(ListenerConfigValue.text("muse").matches(.text("muse")))
        XCTAssertFalse(ListenerConfigValue.text("muse").matches(.text("claude")))
    }

    /// A value the panel holds is read into the value the file holds.
    func test_a_value_from_the_panel_is_read_by_its_kind() {
        XCTAssertEqual(WakeSettingKey.wakesPerHour.value(from: " 6 "), .number(6))
        XCTAssertEqual(WakeSettingKey.fallbackUncontained.value(from: "true"), .bool(true))
        XCTAssertEqual(WakeSettingKey.wakeType.value(from: "muse"), .text("muse"))
        XCTAssertNil(WakeSettingKey.wakesPerHour.value(from: "six"))
        XCTAssertNil(WakeSettingKey.wakeType.value(from: ""))
    }
}
