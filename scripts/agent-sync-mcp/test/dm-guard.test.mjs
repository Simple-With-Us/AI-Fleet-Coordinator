// The owner-in-the-conversation guard on the hosted transport (AFC-469).  Parity with the Python
// guard in scripts/agent_sync/zulip.py:  a bot may send a direct message only when Jay is an
// actual participant in the conversation, and participation is the authenticated Zulip user id.
// A display name that looks like the owner is not participation, and mentioning Jay in the message
// text is not participation either.
//
// The guard sits on the hosted client, so one check covers the hosted MCP and anything else that
// posts through it.  It fails closed:  with no owner id pinned a direct send is refused.
//
// No dependencies:  CI runs this with plain `node --test`.

import test from "node:test";
import assert from "node:assert/strict";
import { ZulipClient } from "../src/zulip.js";
import { DmGuardRefused } from "../src/contract.js";
import { FakeZulip, BOT_EMAIL, OWNER_ID } from "./fake-zulip.mjs";

const CODEX_ID = 1212001;
const CURSOR_ID = 1212002;

function harness({ ownerUserId = 0 } = {}) {
  const fake = new FakeZulip();
  const c = new ZulipClient({
    email: BOT_EMAIL,
    key: fake.key,
    ownerUserId,
    fetch: fake.fetch,
    sleep: async () => {},
    now: () => fake.time,
  });
  return { c, fake };
}

test("a direct send with no owner id is refused and nothing is sent", async () => {
  const { c, fake } = harness();
  await assert.rejects(
    c.post("messages", { type: "direct", to: [CODEX_ID], content: "hello" }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_pinned",
  );
  assert.equal(fake.messages.length, 0);
});

test("ownerUserId 0 fails closed", async () => {
  const { c, fake } = harness({ ownerUserId: 0 });
  await assert.rejects(
    c.post("messages", { type: "direct", to: [CODEX_ID], content: "hello" }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_pinned",
  );
  assert.equal(fake.messages.length, 0);
});

test("a stream post is unaffected:  a channel message is not a direct message", async () => {
  const { c, fake } = harness({ ownerUserId: 0 });
  const result = await c.post("messages", { type: "stream", to: fake.streamId("work-in-progress"), topic: "t", content: "hello" });
  assert.ok(result.id);
  assert.equal(fake.messages.length, 1);
});

test("a direct send with the owner in the recipients goes out", async () => {
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  const result = await c.post("messages", { type: "direct", to: [CODEX_ID, OWNER_ID], content: "hello" });
  assert.ok(result.id);
  assert.equal(fake.messages.length, 1);
});

test("a bot only recipient set is refused", async () => {
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  await assert.rejects(
    c.post("messages", { type: "direct", to: [CODEX_ID], content: "hello" }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_participant",
  );
  assert.equal(fake.messages.length, 0);
});

test("a bot and bot recipient set is refused", async () => {
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  await assert.rejects(
    c.post("messages", { type: "direct", to: [CODEX_ID, CURSOR_ID], content: "hello" }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_participant",
  );
  assert.equal(fake.messages.length, 0);
});

test("a recipient whose name merely looks like the owner does not count", async () => {
  //  The name never reaches the guard:  only the authenticated user id does, and this id is not
  //  the owner's.
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  const lookalike = fake.addUser("jay-jr@zulip.test", "Jay Wedgeworth Jr.", { isBot: true });
  await assert.rejects(
    c.post("messages", { type: "direct", to: [lookalike.user_id], content: "hello" }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_participant",
  );
  assert.equal(fake.messages.length, 0);
});

test("mentioning the owner in the text does not make it participation", async () => {
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  await assert.rejects(
    c.post("messages", { type: "direct", to: [CODEX_ID], content: "@**Jay Wedgeworth** can you look?" }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_participant",
  );
  assert.equal(fake.messages.length, 0);
});

test("a scheduled direct message is guarded too", async () => {
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  await assert.rejects(
    c.post("scheduled_messages", {
      type: "direct",
      to: [CODEX_ID],
      content: "hello",
      scheduled_delivery_timestamp: 4102444800,
    }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_participant",
  );
  assert.equal(fake.scheduled.size, 0);
});

test("a direct draft is guarded too", async () => {
  const { c, fake } = harness({ ownerUserId: OWNER_ID });
  await assert.rejects(
    c.post("drafts", { drafts: [{ type: "private", to: [CODEX_ID], topic: "", content: "hello" }] }),
    (e) => e instanceof DmGuardRefused && e.code === "dm_owner_not_participant",
  );
  assert.equal(fake.drafts.size, 0);
});

test("a stream draft is not a direct message and goes out", async () => {
  const { c, fake } = harness({ ownerUserId: 0 });
  await c.post("drafts", { drafts: [{ type: "stream", to: [fake.streamId("work-in-progress")], topic: "t", content: "hello" }] });
  assert.equal(fake.drafts.size, 1);
});
