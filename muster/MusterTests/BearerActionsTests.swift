import XCTest

/// The owner action route, tested against the Worker's documented contract (`scripts/simple-tools-mcp/src/admin-api.js`).
final class BearerActionsTests: XCTestCase {
    private struct FakeTransport: BearerActionTransport {
        var token: String?
        var answer: (status: Int, data: Data)?
        var sent: Box = Box()
        final class Box { var body: Data?; var token: String? }
        func accessToken() async -> String? { token }
        func post(body: Data, accessToken: String) async -> (status: Int, data: Data)? {
            sent.body = body; sent.token = accessToken
            return answer
        }
    }

    private func json(_ text: String) -> Data { Data(text.utf8) }

    func test_a_mint_id_is_32_hex_and_starts_with_the_clock() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let id = BearerActionRequest.newMintId(now: now, random: { 0xAB })
        XCTAssertEqual(id.count, 32)
        XCTAssertNotNil(id.range(of: "^[0-9a-f]{32}$", options: .regularExpression))
        XCTAssertTrue(id.hasPrefix(String(format: "%012llx", UInt64(1_700_000_000_000))))
        XCTAssertTrue(id.hasSuffix(String(repeating: "ab", count: 10)))
    }

    func test_bodies_carry_exactly_the_fields_the_api_reads() throws {
        let revoke = try BearerActionRequest.make(.revoke, identity: "WISE", mcpAccept: nil).body()
        XCTAssertEqual(String(data: revoke, encoding: .utf8), #"{"action":"revoke","identity":"WISE"}"#)
        let mint = BearerActionRequest.make(.mint, identity: "WISE", mcpAccept: true)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: mint.body()) as? [String: Any])
        XCTAssertEqual(object["action"] as? String, "mint")
        XCTAssertEqual(object["mcp_accept"] as? Bool, true)
        XCTAssertEqual((object["mint_id"] as? String)?.count, 32)
    }

    func test_mcp_accept_is_sent_only_where_the_contract_reads_it() {
        XCTAssertEqual(BearerActionRequest.mcpAccept(for: .mint, live: nil), true)
        XCTAssertEqual(BearerActionRequest.mcpAccept(for: .mint, live: false), false)
        XCTAssertNil(BearerActionRequest.mcpAccept(for: .rotate, live: nil))
        XCTAssertEqual(BearerActionRequest.mcpAccept(for: .rotate, live: false), false)
        XCTAssertNil(BearerActionRequest.mcpAccept(for: .revoke, live: true))
    }

    func test_a_mint_answer_yields_the_bearer_once() {
        let out = BearerActionOutcome.read(status: 200, data: json(#"{"ok":true,"action":"mint","identity":"WISE","kind":"seat","token":"gb_x","expiresAt":"2026-12-23T00:00:00.000Z","mcpAccept":true}"#), identity: "WISE")
        XCTAssertEqual(out, .minted(identity: "WISE", bearer: "gb_x", expiresAt: "2026-12-23T00:00:00.000Z"))
        XCTAssertFalse(out.summary.contains("gb_x"))
    }

    func test_revoke_and_completed_retry_carry_no_bearer() {
        XCTAssertEqual(BearerActionOutcome.read(status: 200, data: json(#"{"ok":true,"action":"revoke","identity":"WISE"}"#), identity: "WISE"), .revoked(identity: "WISE"))
        XCTAssertEqual(BearerActionOutcome.read(status: 200, data: json(#"{"ok":true,"action":"mint","identity":"WISE","completed":true,"tokenShown":false,"result":{}}"#), identity: "WISE"), .alreadyCompleted(identity: "WISE"))
    }

    func test_refusals_say_why() {
        let active = BearerActionOutcome.read(status: 409, data: json(#"{"ok":false,"error":"bearer_active","hint":"use rotate to replace a live bearer"}"#), identity: "WISE")
        XCTAssertEqual(active, .refused("Refused by the Worker:  bearer active.  use rotate to replace a live bearer"))
        let missing = BearerActionOutcome.read(status: 409, data: json(#"{"ok":false,"error":"credentials_missing","missingRecords":["ZC_ZULIP_KEY"]}"#), identity: "ZC")
        XCTAssertTrue(missing.summary.contains("ZC_ZULIP_KEY"))
        if case .refused(let text) = BearerActionOutcome.read(status: 302, data: Data(), identity: "ZC") {
            XCTAssertTrue(text.contains("cloudflared access login"))
        } else { XCTFail("a redirect is a refusal") }
    }

    func test_the_runner_sends_the_owner_token_and_the_body() async throws {
        let box = FakeTransport.Box()
        let transport = FakeTransport(token: "a.b.c", answer: (200, json(#"{"ok":true,"action":"revoke","identity":"WISE"}"#)), sent: box)
        let out = await BearerActionRunner(transport: transport).run(.make(.revoke, identity: "WISE", mcpAccept: nil))
        XCTAssertEqual(out, .revoked(identity: "WISE"))
        XCTAssertEqual(box.token, "a.b.c")
        XCTAssertEqual(String(data: try XCTUnwrap(box.body), encoding: .utf8), #"{"action":"revoke","identity":"WISE"}"#)
    }

    func test_no_login_or_no_answer_sends_nothing_or_says_so() async {
        let none = await BearerActionRunner(transport: FakeTransport(token: nil, answer: nil)).run(.make(.revoke, identity: "WISE", mcpAccept: nil))
        XCTAssertEqual(none.summary.contains("cloudflared access login"), true)
        let down = await BearerActionRunner(transport: FakeTransport(token: "a.b.c", answer: nil)).run(.make(.mint, identity: "WISE", mcpAccept: true))
        XCTAssertTrue(down.summary.contains("could not be reached"))
    }
}
