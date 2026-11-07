import XCTest

/// Whether the Operator can post (`status.operator_voice`):  decoded leniently,  never read as
/// able to post when the eavesdropper does not say,  and tied to dropped wakes only when the
/// ledger gives that reason.
final class OperatorVoiceTests: XCTestCase {
    private func decode(_ json: String) throws -> ListenerStatus {
        try JSONDecoder().decode(ListenerStatus.self, from: Data(json.utf8))
    }

    /// 2:14 PM on the owner's clock,  Saturday,  November 7,  2026.
    private let since = "2026-11-07T20:14:00Z"
    private var sinceDate: Date { FleetTime.parse(since) ?? Date.distantPast }

    // MARK: Decoding

    func test_present_and_available() throws {
        let status = try decode("""
        {"running": true, "status": {"instance": "mac",
          "operator_voice": {"available": true, "code": null, "since": "\(since)"}}}
        """)
        XCTAssertEqual(status.status?.operator_voice?.available, true)
        XCTAssertNil(status.status?.operator_voice?.code)
        let reading = OperatorVoiceReading(status.status)
        XCTAssertEqual(reading, .available(since: sinceDate, sinceText: since))
        XCTAssertEqual(reading.label, "Can post")
        XCTAssertFalse(reading.isProblem)
        XCTAssertTrue(reading.problemLines(seats: []).isEmpty)
    }

    func test_present_and_unavailable_with_code_and_since() throws {
        let status = try decode("""
        {"running": true, "status": {"instance": "mac",
          "operator_voice": {"available": false, "code": "credential-rejected", "since": "\(since)"}}}
        """)
        let reading = OperatorVoiceReading(status.status)
        XCTAssertEqual(reading, .unavailable(code: "credential-rejected", since: sinceDate, sinceText: since))
        XCTAssertEqual(reading.label, "Cannot post")
        XCTAssertTrue(reading.isProblem)
        XCTAssertTrue(reading.isUnavailable)
        // The time is on Jay's clock,  12-hour with AM or PM,  and no zone is written.
        let sentence = reading.sentence(now: sinceDate)
        XCTAssertEqual(sentence, "The Operator cannot post to Zulip, and has not been able to since 2:14 PM.")
        XCTAssertFalse(sentence.contains("UTC"))
        XCTAssertFalse(sentence.contains("credential-rejected"), "the code stays in the tooltip")
        // The code is in the tooltip,  and its time is on Jay's clock too,  never the raw UTC text.
        let tooltip = reading.tooltip(now: sinceDate)
        XCTAssertTrue(tooltip.contains("credential-rejected"), tooltip)
        XCTAssertTrue(tooltip.contains("since 2:14 PM"), tooltip)
        XCTAssertFalse(tooltip.contains(since), tooltip)
        XCTAssertFalse(tooltip.contains("UTC"), tooltip)
        // A day other than today names the day.
        let nextDay = sinceDate.addingTimeInterval(86_400)
        XCTAssertTrue(reading.sentence(now: nextDay).hasSuffix("since Nov 7, 2:14 PM."), reading.sentence(now: nextDay))
        // The problem box says it.
        let lines = reading.problemLines(seats: [], now: sinceDate)
        XCTAssertEqual(lines, [sentence])
    }

    /// An older eavesdropper does not publish the block:  that reads as not reported,  never
    /// as able to post.
    func test_absent_reads_as_not_reported() throws {
        let status = try decode(#"{"running": true, "status": {"instance": "mac", "seats": {}}}"#)
        XCTAssertNil(status.status?.operator_voice)
        let reading = OperatorVoiceReading(status.status)
        XCTAssertEqual(reading, .notReported)
        XCTAssertEqual(reading.label, "Not reported")
        XCTAssertFalse(reading.label.lowercased().contains("available"))
        XCTAssertTrue(reading.tooltip().contains("older eavesdropper"), reading.tooltip())
        XCTAssertTrue(reading.problemLines(seats: []).isEmpty)
        // A null block is the same as none.
        let null = try decode(#"{"running": true, "status": {"operator_voice": null}}"#)
        XCTAssertEqual(OperatorVoiceReading(null.status), .notReported)
    }

    /// A block of the wrong shape costs that block only:  the rest of the status is read,
    /// and the block reads as not known,  never as available and never as not reported.
    func test_malformed_costs_only_the_block() throws {
        let shapes = [
            #""operator_voice": "yes""#,
            #""operator_voice": [true]"#,
            #""operator_voice": {"available": "yes", "code": 7, "since": false}"#,
            #""operator_voice": {}"#,
        ]
        for shape in shapes {
            let status = try decode("""
            {"running": true, "pid": 42, "status": {"instance": "mac", \(shape),
              "seats": {"MM": {"bot": "MM", "wake": "muse", "connected": true}}}}
            """)
            XCTAssertEqual(status.running, true, shape)
            XCTAssertEqual(status.pid, 42, shape)
            XCTAssertEqual(status.status?.instance, "mac", shape)
            XCTAssertEqual(status.seat("MM")?.connected, true, shape)
            XCTAssertNotNil(status.status?.operator_voice, shape)
            let reading = OperatorVoiceReading(status.status)
            XCTAssertEqual(reading, .unreadable, shape)
            XCTAssertEqual(reading.label, "Not known", shape)
        }
    }

    /// `available: null` is the eavesdropper's own "not tried yet" (its first Operator build since
    /// it started has not finished),  which is neither a wrong shape nor an older eavesdropper.
    func test_a_null_available_reads_as_not_tried_yet() throws {
        let status = try decode("""
        {"running": true, "status": {"operator_voice": {"available": null, "code": null, "since": null}}}
        """)
        XCTAssertEqual(status.status?.operator_voice?.availableIsNull, true)
        let reading = OperatorVoiceReading(status.status)
        XCTAssertEqual(reading, .notTriedYet)
        XCTAssertEqual(reading.label, "Not tried yet")
        XCTAssertFalse(reading.isProblem)
        XCTAssertTrue(reading.problemLines(seats: []).isEmpty)
        XCTAssertTrue(reading.sentence().contains("has not tried the Operator yet"), reading.sentence())
        // A missing `available` is not the same:  it is a block Muster cannot read.
        let missing = try decode(#"{"running": true, "status": {"operator_voice": {"code": null}}}"#)
        XCTAssertEqual(missing.status?.operator_voice?.availableIsNull, false)
        XCTAssertEqual(OperatorVoiceReading(missing.status), .unreadable)
    }

    /// The eavesdropper's three reason codes read as their cause in plain words,  after the
    /// time,  and the token itself stays in the tooltip.  A code Muster does not know adds
    /// nothing to the sentence.
    func test_known_reason_codes_read_as_their_cause() throws {
        let causes = [
            "operator_credential_rejected": "Zulip refused the Operator's credential.",
            "operator_owner_not_pinned": "The owner is not pinned in the listener's settings, so the Operator does not post.",
            "operator_unavailable": "The listener could not get the Operator ready to post.",
        ]
        for (code, cause) in causes {
            let reading = OperatorVoiceReading.unavailable(code: code, since: sinceDate, sinceText: since)
            let sentence = reading.sentence(now: sinceDate)
            XCTAssertEqual(sentence, "The Operator cannot post to Zulip, and has not been able to since 2:14 PM.\u{00A0} " + cause)
            XCTAssertFalse(sentence.contains(code), "the token stays out of the sentence:  \(sentence)")
            XCTAssertTrue(reading.tooltip(now: sinceDate).contains(code), reading.tooltip(now: sinceDate))
            XCTAssertEqual(reading.problemLines(seats: [], now: sinceDate), [sentence])
        }
        XCTAssertNil(OperatorVoiceReading.cause("something_new"))
        let unknown = OperatorVoiceReading.unavailable(code: "something_new", since: nil, sinceText: nil)
        XCTAssertEqual(unknown.sentence(), "The Operator cannot post to Zulip.")
    }

    /// A field of the wrong type inside a block that answers costs that field only.
    func test_a_mistyped_code_or_since_keeps_the_answer() throws {
        let status = try decode("""
        {"running": true, "status": {"operator_voice": {"available": false, "code": 12, "since": "soon"}}}
        """)
        let reading = OperatorVoiceReading(status.status)
        XCTAssertEqual(reading, .unavailable(code: nil, since: nil, sinceText: "soon"))
        XCTAssertEqual(reading.sentence(), "The Operator cannot post to Zulip.")
        // A time Muster could not read is quoted in the tooltip and said to be so.
        XCTAssertTrue(reading.tooltip().contains("a time Muster could not read:  soon"), reading.tooltip())
    }

    // MARK: The panel's reading,  from the panel's own status

    /// The panel's model,  from one status envelope read at `readAt`.
    private func panel(_ json: String, readAt: Date) throws -> WakeSettingsModel {
        var sources = WakeSettingsSources()
        sources.status = try decode(json)
        sources.readAt = readAt
        return WakeSettingsBuilder.build(sources)
    }

    private func envelope(running: Bool, updated: Date, voice: String) -> String {
        """
        {"running": \(running), "status": {"instance": "mac", "updated": \(updated.timeIntervalSince1970),
          "operator_voice": \(voice)}}
        """
    }

    /// A running eavesdropper's current status is believed.
    func test_the_panel_reads_a_current_status() throws {
        let voice = #"{"available": true, "code": null, "since": "\#(since)"}"#
        let model = try panel(envelope(running: true, updated: sinceDate, voice: voice),
                              readAt: sinceDate.addingTimeInterval(5))
        XCTAssertTrue(model.statusIsCurrent)
        XCTAssertEqual(model.operatorVoice, .available(since: sinceDate, sinceText: since))
        XCTAssertEqual(model.operatorVoice.label, "Can post")
    }

    /// A stopped eavesdropper's file,  or an old one,  is its last word,  not what is true now:
    /// its `available: true` never reads as Can post.
    func test_a_stopped_or_old_status_never_reads_as_can_post() throws {
        let voice = #"{"available": true, "code": null, "since": "\#(since)"}"#
        let stopped = try panel(envelope(running: false, updated: sinceDate, voice: voice),
                                readAt: sinceDate.addingTimeInterval(5))
        let old = try panel(envelope(running: true, updated: sinceDate, voice: voice),
                            readAt: sinceDate.addingTimeInterval(3_600))
        for (model, why) in [(stopped, "The eavesdropper is not running"), (old, "The eavesdropper's status is not current")] {
            XCTAssertFalse(model.statusIsCurrent, why)
            XCTAssertEqual(model.operatorVoice,
                           .notCurrent(why: why, lastAvailable: true, since: sinceDate, sinceText: since))
            XCTAssertEqual(model.operatorVoice.label, "Not known")
            XCTAssertNotEqual(model.operatorVoice.label, "Can post")
            let sentence = model.operatorVoice.sentence(now: sinceDate)
            XCTAssertTrue(sentence.hasPrefix(why + ", so whether the Operator can post now is not known."), sentence)
            XCTAssertTrue(sentence.contains("Its last status said the Operator could post from 2:14 PM."), sentence)
            XCTAssertTrue(model.operatorVoice.problemLines(seats: []).isEmpty)
        }
    }

    /// An old status whose last word was that the Operator could not post still says so in the
    /// problem box,  as not known now.
    func test_an_old_status_that_could_not_post_says_it_is_not_current() throws {
        let voice = #"{"available": false, "code": "credential-rejected", "since": "\#(since)"}"#
        let model = try panel(envelope(running: true, updated: sinceDate, voice: voice),
                              readAt: sinceDate.addingTimeInterval(3_600))
        XCTAssertEqual(model.operatorVoice.label, "Not known")
        XCTAssertTrue(model.operatorVoice.isProblem)
        let lines = model.operatorVoice.problemLines(seats: [], now: sinceDate)
        XCTAssertEqual(lines.count, 1)
        XCTAssertTrue(lines[0].contains("not current"), lines[0])
        XCTAssertTrue(lines[0].contains("could not post from 2:14 PM"), lines[0])
    }

    /// No status at all,  or another instance's,  is not known.
    func test_no_status_or_another_instance_is_not_known() throws {
        XCTAssertEqual(WakeSettingsBuilder.build(WakeSettingsSources()).operatorVoice.label, "Not known")
        let server = try decode(#"{"running": true, "status": {"instance": "server", "operator_voice": {"available": true}}}"#)
        let reading = OperatorVoiceReading.current(server, runState: .running, statusIsCurrent: true)
        XCTAssertEqual(reading.label, "Not known")
    }

    // MARK: The dropped wakes

    private func wakeModel(_ outcomes: [String: [WakeLedger.Outcome]]) -> WakeSettingsModel {
        var sources = WakeSettingsSources()
        sources.seats = ["MM", "CLAUDE"]
        sources.partition = ListenerPartition(seats: ["MM": "mac", "CLAUDE": "mac"])
        sources.recentWakes = outcomes
        return WakeSettingsBuilder.build(sources)
    }

    private func outcome(_ id: String, _ state: String, _ reason: String?) -> WakeLedger.Outcome {
        WakeLedger.Outcome(id: id, state: state, topic: "t", owner: false, at: nil, reason: reason)
    }

    /// Wakes the ledger shows as dropped for `operator_unavailable` are tied to the Operator
    /// when it cannot post,  and other drops are not.
    func test_dropped_wakes_are_tied_to_the_operator_only_by_their_own_reason() throws {
        let model = wakeModel([
            "MM": [
                outcome("w1", "dropped", "operator_unavailable"),
                outcome("w2", "dropped", "operator_unavailable"),
                outcome("w3", "dropped", "host_busy"),
                outcome("w4", "done", nil),
            ],
            "CLAUDE": [outcome("w5", "dropped", "host_busy")],
        ])
        let dropped = OperatorVoiceReading.droppedForVoice(model.seats)
        XCTAssertEqual(dropped.count, 2)
        XCTAssertEqual(dropped.seats, ["MM"])

        let down = OperatorVoiceReading.unavailable(code: "credential-rejected", since: nil, sinceText: nil)
        let lines = down.problemLines(seats: model.seats)
        XCTAssertEqual(lines.count, 1)
        XCTAssertTrue(lines[0].hasPrefix("The Operator cannot post to Zulip."), lines[0])
        XCTAssertTrue(lines[0].contains("2 of the last wakes listed for MM were dropped because the Operator could not post."), lines[0])
        XCTAssertTrue(lines[0].contains("\u{00A0} "), "sentences are joined with the sentence gap")

        // When the Operator can post,  or nobody says,  no drop is tied to it.
        XCTAssertTrue(OperatorVoiceReading.available(since: nil, sinceText: nil)
            .problemLines(seats: model.seats).isEmpty)
        XCTAssertTrue(OperatorVoiceReading.notReported.problemLines(seats: model.seats).isEmpty)

        // No drop for that reason:  the line says only that the Operator cannot post.
        let quiet = wakeModel(["MM": [outcome("w3", "dropped", "host_busy")]])
        XCTAssertEqual(down.problemLines(seats: quiet.seats), ["The Operator cannot post to Zulip."])
    }
}
