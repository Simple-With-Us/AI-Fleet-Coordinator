import XCTest

// The Operator panel's wake router, held-back and owner-queue rows, and Test Wake (AFC-825).  The
// answers are the shapes the server's control helper writes (`scripts/fleet-ops-control.py`:
// `reduce_router_health`, `reduce_operator_wakes` and `reduce_test_wake`).

private let now = FleetTime.parse("2026-11-17T18:00:00Z")!

private func at(_ secondsAgo: Int) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.string(from: now.addingTimeInterval(-Double(secondsAgo)))
}

private func result(_ stdout: String, exit: Int32 = 0) -> OpsProcessResult {
    OpsProcessResult(exitCode: exit, stdout: stdout, stderr: "", timedOut: false)
}

private func builder() -> OpsCommandBuilder {
    OpsCommandBuilder(
        repoRoot: URL(fileURLWithPath: "/tmp/example-checkout"),
        home: URL(fileURLWithPath: "/tmp/example-home"),
        sshAuthSocket: nil
    )
}

private let target = OperatorWakeTarget(seat: "CLAUDE", letter: "BB", generation: 2, hostCode: "S")

/// A fixture that did not read is a failed test, never a skipped one.
private struct FixtureDidNotRead: Error {}

// MARK: - The exact chat

final class OperatorWakeTargetTests: XCTestCase {
    func test_a_target_is_one_word_the_helper_takes() {
        XCTAssertEqual(target.word, "CLAUDE.BB.2.S")
        XCTAssertEqual(target.name, "CLAUDE BB.2 (server)")
        XCTAssertEqual(OperatorWakeTarget(word: "CLAUDE.BB.2.S"), target)
        XCTAssertEqual(OperatorWakeTarget(word: "GB-HOUSEKEEPER.A.1.M")?.hostWord, "Mac")
    }

    func test_anything_that_is_not_exactly_one_chat_is_no_target() {
        for word in ["CLAUDE", "CLAUDE.BB.2", "claude.BB.2.S", "CLAUDE.bb.2.S", "CLAUDE.BBB.2.S",
                     "CLAUDE.BB.0.S", "CLAUDE.BB.+2.S", "CLAUDE.BB.2.X", "-CLAUDE.BB.2.S", "CLA UDE.BB.2.S",
                     "CLAUDE.BB.2.S.S", ""] {
            XCTAssertNil(OperatorWakeTarget(word: word), word)
        }
        XCTAssertNil(OperatorWakeTarget.make(seat: "CLAUDE", letter: nil, generation: 2, hostCode: "S"))
        XCTAssertNil(OperatorWakeTarget.make(seat: "CLAUDE", letter: "BB", generation: nil, hostCode: "S"))
        XCTAssertNil(OperatorWakeTarget.make(seat: "CLAUDE", letter: "BB", generation: 2, hostCode: nil))
        XCTAssertEqual(OperatorWakeTarget.make(seat: "CLAUDE", letter: "BB", generation: 2, hostCode: "S"), target)
    }

    func test_the_action_sends_that_word_and_nothing_else() throws {
        let plan = try builder().invocation(for: .testWake(target), mode: .plan)
        XCTAssertEqual(Array(plan.arguments.suffix(5)), ["server", "test-wake", "CLAUDE.BB.2.S", "--plan", "--json"])
        let run = try builder().invocation(for: .testWake(target), mode: .yes)
        XCTAssertEqual(Array(run.arguments.suffix(5)), ["server", "test-wake", "CLAUDE.BB.2.S", "--yes", "--json"])
        let bad = OperatorWakeTarget(seat: "CLAUDE;x", letter: "BB", generation: 2, hostCode: "S")
        XCTAssertThrowsError(try builder().invocation(for: .testWake(bad), mode: .plan))
    }

    func test_the_plan_names_the_one_chat_before_anything_is_sent() {
        let action = OpsAction.testWake(target)
        XCTAssertTrue(action.title.contains("CLAUDE BB.2 (server)"), action.title)
        XCTAssertTrue(action.effectSentence.contains("CLAUDE BB.2 (server), and to no other chat"), action.effectSentence)
        XCTAssertTrue(action.effectSentence.contains("delivers nothing"), action.effectSentence)
        XCTAssertFalse(action.changesNothing)
        XCTAssertEqual(action.auditTarget, "CLAUDE.BB.2.S")
    }
}

// MARK: - What a test wake came to

final class OperatorTestWakeResultTests: XCTestCase {
    private func answer(result word: String = "done", ok: Bool = true, error: String? = nil,
                        passed: Bool = true, picked: String = "BB", generation: Int = 2) -> String {
        let code = error.map { #", "errorCode": "\#($0)""# } ?? ""
        let failure = error.map { #""\#($0)""# } ?? "null"
        return """
        {"ok": \(ok), "verb": "server test-wake", "target": "CLAUDE.BB.2.S",
         "commands": [["/usr/bin/docker", "exec"]], "result": "\(word)", "detail": "the router answered"\(code),
         "testWake": {"target": {"seat": "CLAUDE", "letter": "BB", "generation": 2, "host": "server"},
                      "mode": "dry_run", "passed": \(passed), "accepted": true, "delivered": false,
                      "dryRun": true, "picked": true, "wakeId": "test-wake-20261118T053000Z-0a1b2c3d",
                      "httpStatus": 200, "error": \(failure),
                      "router": {"healthy": true, "delivery": false, "reason": null},
                      "answer": {"status": "test", "pick": "letter", "pickedLetter": "\(picked)",
                                 "pickedGeneration": \(generation), "host": "server", "reason": "addressed_letter",
                                 "delivered": false, "letterLive": true, "candidates": 2}}}
        """
    }

    func test_a_passed_dry_run_is_accepted_and_never_called_delivered() throws {
        let outcome = OpsResultParser.parse(result(answer()), mode: .yes, action: .testWake(target), timeout: 180)
        XCTAssertTrue(outcome.isSuccess(for: .yes))
        let testWake = try XCTUnwrap(outcome.testWake)
        XCTAssertEqual(testWake.target, target)
        XCTAssertEqual(testWake.wakeID, "test-wake-20261118T053000Z-0a1b2c3d")
        let lines = testWake.lines
        XCTAssertTrue(lines.contains("Addressed: CLAUDE BB.2 (server)."), "\(lines)")
        XCTAssertTrue(lines.contains("Router: accepted the dry run (HTTP 200)."), "\(lines)")
        XCTAssertTrue(lines.contains("Picked: BB.2 (server), by letter, reason addressed letter."), "\(lines)")
        XCTAssertTrue(lines.contains("Delivered: nothing, because a test wake is a dry run."), "\(lines)")
        XCTAssertTrue(lines.contains("Router delivery for CLAUDE: off, so a real wake would be held."), "\(lines)")
        XCTAssertTrue(lines.contains("Wake id: test-wake-20261118T053000Z-0a1b2c3d"), "\(lines)")
        let title = outcome.resultTitle(for: .testWake(target))
        XCTAssertTrue(title.hasPrefix("The router accepted the dry run"), title)
        XCTAssertTrue(title.hasSuffix("Nothing was delivered."), title)
    }

    func test_a_pick_of_another_chat_fails_and_says_nothing_was_delivered() throws {
        let text = answer(result: "failed", ok: false, error: "wrong_target", passed: false, generation: 1)
        let outcome = OpsResultParser.parse(result(text, exit: 1), mode: .yes, action: .testWake(target), timeout: 180)
        XCTAssertFalse(outcome.isSuccess(for: .yes))
        XCTAssertEqual(outcome.errorCode, "wrong_target")
        XCTAssertEqual(outcome.resultTitle(for: .testWake(target)),
                       "The test wake did not pass.\(ServerStatusCopy.sentenceGap)Nothing was delivered.")
        XCTAssertNotNil(outcome.advice(for: .testWake(target)))
        let lines = try XCTUnwrap(outcome.testWake).lines
        XCTAssertTrue(lines.contains("Why it did not pass: wrong target."), "\(lines)")
        XCTAssertFalse(lines.contains { $0.lowercased().contains("delivered: yes") }, "\(lines)")
    }

    func test_a_refused_target_says_nothing_was_sent() {
        let text = """
        {"ok": false, "verb": "server test-wake", "target": "CLAUDE.BB.2.S", "commands": [], "result": "refused",
         "detail": "no live chat in the letter registry has that seat, letter, generation and host",
         "errorCode": "target_gone"}
        """
        let plan = OpsResultParser.parse(result(text, exit: 2), mode: .plan, action: .testWake(target), timeout: 90)
        XCTAssertEqual(plan.kind, .refused)
        XCTAssertNil(plan.testWake)
        XCTAssertTrue(plan.advice(for: .testWake(target))?.contains("no longer live") == true)
    }

    func test_every_code_the_helper_answers_has_advice() {
        // Kody, PR 2299:  each test-wake code the helper can answer says why in the sheet.
        let codes = ["target_gone", "target_ambiguous", "registry_unavailable", "letter_not_picked", "wrong_target",
                     "no_pick", "not_the_router", "router_unreachable", "routine_not_configured",
                     "routine_key_refused", "routine_url_refused", "seat_not_loaded", "no_routine", "usage",
                     "not_accepted", "not_dry_run", "test_wake_failed", "docker_unavailable",
                     "container_not_running", "read_failed", "timeout", "too_large", "not_json"]
        for code in codes {
            var outcome = OpsOutcome(kind: .failed, ok: false, commands: [], detail: "", exitCode: 1)
            outcome.errorCode = code
            XCTAssertNotNil(outcome.advice(for: .testWake(target)), code)
        }
    }

    func test_a_plan_carries_no_result() {
        let outcome = OpsResultParser.parse(result(answer(result: "planned")), mode: .plan,
                                            action: .testWake(target), timeout: 90)
        XCTAssertNil(outcome.testWake)
    }
}

// MARK: - The router's health

final class OperatorRouterHealthTests: XCTestCase {
    private func healthAnswer(healthy: String = "false") -> String {
        """
        {"ok": true, "verb": "server router-health", "target": "server", "commands": [], "result": "planned",
         "detail": "read-only health of the server's wake router",
         "routerHealth": {"schema": 1, "generated_at": "\(at(1))", "instance": "server", "healthy": \(healthy),
           "counts": {"total": 4}, "truncated": false, "dropped": 0,
           "seats": [
             {"seat": "AG", "installed": true, "healthy": true, "delivery": false, "registry_configured": true, "reason": null},
             {"seat": "CLAUDE", "installed": true, "healthy": true, "delivery": true, "registry_configured": true, "reason": null},
             {"seat": "MC", "installed": false, "healthy": false, "delivery": null, "registry_configured": null, "reason": "not_installed"},
             {"seat": "ZC", "installed": true, "healthy": false, "delivery": null, "registry_configured": null, "reason": "seat_not_served"}]}}
        """
    }

    private func document(_ text: String) throws -> OperatorRouterHealthDocument {
        guard case .read(let document) = OperatorRouterHealthDecoder.reading(result(text)) else {
            throw FixtureDidNotRead()
        }
        return document
    }

    func test_the_card_says_running_and_names_the_seats_with_delivery_off() throws {
        let health = OperatorRouterHealthDecoder.reading(result(healthAnswer()))
        let card = OperatorPanelPresentation.routerCard(health: health, wakes: nil, status: nil, now: now)
        XCTAssertEqual(card.state, "Running")
        XCTAssertEqual(card.tone, .warning)
        XCTAssertTrue(card.lines.contains("3 seats on the router, 1 with delivery on."), "\(card.lines)")
        XCTAssertTrue(card.lines.contains("Delivery off: AG."), "\(card.lines)")
        XCTAssertTrue(card.lines.contains("Not healthy: ZC."), "\(card.lines)")
        XCTAssertFalse(card.lines.contains(OperatorPanelPresentation.routerStateNotReported))
    }

    func test_a_router_that_does_not_answer_is_not_running() {
        let text = """
        {"ok": false, "verb": "server router-health", "target": "server", "commands": [], "result": "unavailable",
         "detail": "the wake router is not listening on loopback 8794", "errorCode": "router_not_running"}
        """
        let health = OperatorRouterHealthDecoder.reading(result(text, exit: 1))
        XCTAssertEqual(health, .unavailable(OperatorRouterHealthDecoder.notRunning))
        let card = OperatorPanelPresentation.routerCard(health: health, wakes: nil, status: nil, now: now)
        XCTAssertEqual(card.state, "Not running")
        XCTAssertEqual(card.tone, .bad)
    }

    func test_without_a_health_read_the_card_is_the_old_card() {
        let old = OperatorPanelPresentation.router(wakes: nil, status: nil, now: now)
        XCTAssertEqual(OperatorPanelPresentation.routerCard(health: nil, wakes: nil, status: nil, now: now), old)
    }

    func test_a_helper_without_the_verb_says_so() {
        let text = """
        {"ok": false, "verb": "server router-health", "target": "server", "commands": [], "result": "refused",
         "detail": "unknown server verb"}
        """
        let health = OperatorRouterHealthDecoder.reading(result(text, exit: 2))
        XCTAssertEqual(health.failure?.contains("does not have this read yet"), true, "\(health)")
    }

    func test_each_seat_shows_its_delivery_and_the_routers_words() throws {
        let lines = OperatorPanelPresentation.routerSeatLines(try document(healthAnswer()))
        XCTAssertEqual(lines.map(\.seat), ["AG", "CLAUDE", "MC", "ZC"])
        XCTAssertEqual(lines[0].delivery.text, "Off")
        XCTAssertEqual(lines[0].delivery.tone, .warning)
        XCTAssertEqual(lines[1].delivery.text, "On")
        XCTAssertEqual(lines[2].installed.text, "Not installed")
        XCTAssertEqual(lines[2].reason, "Not installed")
        XCTAssertEqual(lines[3].health.text, "Not healthy")
        XCTAssertEqual(lines[3].reason, "Seat not served")
    }

    @MainActor
    func test_only_a_seat_the_router_serves_is_routed() async throws {
        let text = healthAnswer()
        let store = OperatorRouterHealthStore(read: { OperatorRouterHealthDecoder.reading(result(text)) })
        XCTAssertFalse(store.isRouted("CLAUDE"))
        await store.refresh()
        XCTAssertTrue(store.isRouted("CLAUDE"))
        XCTAssertTrue(store.isRouted("AG"))
        XCTAssertFalse(store.isRouted("MC"))
        XCTAssertFalse(store.isRouted("CODEX"))
    }
}

// MARK: - Held back and waiting for the owner

final class OperatorQueueTests: XCTestCase {
    private func wakes() throws -> OperatorWakesDocument {
        let text = """
        {"ok": true, "verb": "server operator-wakes", "target": "server", "commands": [], "result": "planned",
         "wakes": {"schema": 1, "generated_at": "\(at(1))", "instance": "server", "window_hours": 24,
           "totals": {"recent": 0, "held_back": 86, "held_back_lost": 1, "owner_queue": 102},
           "truncated": false, "unreadable": [],
           "seats": [
             {"seat": "CLAUDE", "recent": {"total": 0, "by_state": {}, "rows": []},
              "held_back": {"count": 1, "lost": 1, "reasons": {"wakes_per_hour": 1},
                            "rows": [{"message_id": 1, "at": "\(at(600))", "reason": "wakes_per_hour", "dm": false}]},
              "owner_queue": {"pending": 1, "rows": [{"seq": 4, "at": "\(at(120))", "kind": "question", "risk": "high"}]}},
             {"seat": "JET", "recent": {"total": 0, "by_state": {}, "rows": []},
              "held_back": {"count": 85, "lost": 0, "reasons": {"per_topic_per_hour": 85},
                            "rows": [{"message_id": 2, "at": "\(at(7200))", "reason": "per_topic_per_hour", "dm": false},
                                     {"message_id": 3, "at": "\(at(60))", "reason": "per_topic_per_hour", "dm": false}]},
              "owner_queue": {"pending": 101, "rows": []}},
             {"seat": "CODEX", "recent": {"total": 0, "by_state": {}, "rows": []},
              "held_back": {"count": 0, "lost": 0, "reasons": {}, "rows": []},
              "owner_queue": {"pending": 0, "rows": []}}]}}
        """
        guard case .read(let document) = OperatorWakesDecoder.reading(result(text)) else {
            throw FixtureDidNotRead()
        }
        return document
    }

    func test_the_held_back_and_owner_rows_are_decoded() throws {
        let document = try wakes()
        let jet = try XCTUnwrap(document.seats.first { $0.seat == "JET" })
        XCTAssertEqual(jet.heldBack?.count, 85)
        XCTAssertEqual(jet.heldBack?.reasons, ["per_topic_per_hour": 85])
        XCTAssertEqual(jet.heldBack?.rows.count, 2)
        XCTAssertEqual(jet.ownerQueue?.pending, 101)
    }

    func test_a_seat_with_nothing_held_and_nothing_waiting_is_not_listed() throws {
        let lines = OperatorPanelPresentation.queueLines(try wakes(), now: now)
        XCTAssertEqual(lines.map(\.seat), ["JET", "CLAUDE"])
        XCTAssertEqual(lines[0].heldBack, "85")
        XCTAssertEqual(lines[0].reasons, "Per topic per hour 85")
        XCTAssertEqual(lines[0].oldestHeld, "2h ago")
        XCTAssertEqual(lines[0].ownerQueue, "101")
        XCTAssertEqual(lines[0].tone, .warning)
        XCTAssertEqual(lines[1].heldBack, "1 (1 lost)")
        XCTAssertEqual(lines[1].ownerRows, "Question, High risk 2m ago")
    }

    func test_an_older_answer_without_the_rows_still_reads() throws {
        let text = """
        {"ok": true, "verb": "server operator-wakes", "target": "server", "commands": [], "result": "planned",
         "wakes": {"schema": 1, "instance": "server", "seats": [{"seat": "CLAUDE", "recent": {"total": 0, "rows": []}}]}}
        """
        guard case .read(let document) = OperatorWakesDecoder.reading(result(text)) else {
            return XCTFail("not read")
        }
        XCTAssertNil(document.seats.first?.heldBack)
        XCTAssertEqual(OperatorPanelPresentation.queueLines(document, now: now), [])
    }
}

// MARK: - Which rows offer a Test Wake

final class OperatorTestWakeRowTests: XCTestCase {
    func test_only_a_live_chat_named_exactly_carries_a_target() throws {
        let text = """
        {"ok": true, "verb": "server operator-sessions", "target": "server", "commands": [], "result": "planned",
         "sessions": {"schema": 1, "instance": "server", "counts": {"active": 3, "closed": 0}, "truncated": false,
           "dropped": 0, "sessions": [
             {"letter": "BB", "generation": 2, "seat": "CLAUDE", "platform": "claude", "host": "server", "host_code": "S",
              "status": "active", "wake": "none", "registered_at": "\(at(600))", "last_seen_at": "\(at(60))", "issues": []},
             {"letter": null, "generation": null, "seat": "CODEX", "platform": "codex", "host": "server", "host_code": "S",
              "status": "active", "wake": "none", "registered_at": "\(at(600))", "last_seen_at": "\(at(120))", "issues": []},
             {"letter": "CC", "generation": 1, "seat": "AG", "platform": "antigravity", "host": "mac", "host_code": "M",
              "status": "closed", "wake": "none", "registered_at": "\(at(600))", "last_seen_at": "\(at(180))", "issues": []}]}}
        """
        guard case .read(let document) = OperatorSessionsDecoder.reading(result(text)) else {
            return XCTFail("not read")
        }
        let table = OperatorPanelPresentation.seatsTable(document, status: nil, now: now, grouping: .flat)
        let bySeat = Dictionary(uniqueKeysWithValues: table.rows.map { ($0.seat, $0.testTarget) })
        XCTAssertEqual(bySeat["CLAUDE"] ?? nil, target)
        XCTAssertNil(bySeat["CODEX"] ?? nil)
        XCTAssertNil(bySeat["AG"] ?? nil)
    }
}
