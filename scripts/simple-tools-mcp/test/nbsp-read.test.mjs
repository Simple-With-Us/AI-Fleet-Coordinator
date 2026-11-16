// AFC-729: hosted reads must show U+00A0.  NFKC used to map it to U+0020 inside
// fold(), and every fenced read goes through fold().  GitHub workflow results
// and FleetLink file JSON never did.  This file pins both.

import test from "node:test";
import assert from "node:assert/strict";
import { HostedTools } from "../src/hosted-tools.js";
import { ZulipClient } from "../src/zulip.js";
import * as S from "../src/seat-state.js";
import { loadConfig } from "../src/config.js";
import { clean, fold, okResult } from "../src/contract.js";
import { linearClean } from "../src/linear.js";
import { recallClean } from "../src/recall.js";
import { fleetlinkEnvelope, fleetlinkMessage } from "../src/fleetlink.js";
import { wakeBody } from "../src/linear-wake.js";
import { toolCallEvent } from "../src/mcp-lifecycle.js";
import { FakeZulip, BOT_EMAIL } from "./fake-zulip.mjs";
import { testEnv } from "./helpers.mjs";

const NBSP = "\u00A0";
const GAP = `${NBSP} `;
const config = loadConfig(testEnv());

function memoryGate(clock) {
  const store = S.memoryStore();
  const g = { store };
  for (const name of ["reserve", "noteRateLimited", "idemBegin", "idemSet", "idemDrop", "idemPeek", "auditCall", "roleSet"]) {
    g[name] = (options) => S[name](store, options, clock.now);
  }
  g.roleGet = () => S.roleGet(store, clock.now);
  g.callTail = (n) => S.callTail(store, n);
  return g;
}

test("fold keeps every Unicode space separator and still folds look-alikes", () => {
  const spaces = ["\u00A0", "\u1680", "\u2003", "\u2007", "\u202F", "\u205F", "\u3000"];
  for (const ch of spaces) {
    assert.equal(fold(`A${ch}B`), `A${ch}B`, `U+${ch.codePointAt(0).toString(16)} was rewritten`);
  }
  assert.equal(fold("\uFF22\uFF25\uFF27\uFF29\uFF2E"), "BEGIN");
  assert.equal(fold("A\u200BB\u202EC"), "ABC");
  assert.equal(fold("ﬁle"), "file");
});

test("clean keeps a sentence-gap no-break space and still strips a marker", () => {
  const [body] = clean(`Hello.${GAP}Next`);
  assert.equal(body, `Hello.${GAP}Next`);
  assert.equal(clean(`BEGIN${NBSP}UNTRUSTED${NBSP}ZULIP`)[0], "[marker removed]");
  assert.equal(clean(`BEG${NBSP}INUNTRUSTEDZULIP`)[0], `BEG${NBSP}INUNTRUSTEDZULIP`);
  assert.equal(clean("BEGIN\u200BUNTRUSTED\u200BZULIP")[0], "[marker removed]");
  assert.equal(clean(`Hello.${GAP}Next\u200B`)[0], `Hello.${GAP}Next`);
});

test("Linear, recall and FleetLink cleaners keep U+00A0", () => {
  assert.equal(linearClean(`Issue.${GAP}Done`)[0], `Issue.${GAP}Done`);
  assert.equal(recallClean(`Hit.${GAP}Text`)[0], `Hit.${GAP}Text`);
  const fenced = fleetlinkEnvelope("simple-tools fleetlink_read_file", { text: `File.${GAP}Body` }, "0123456789abcdef");
  assert.ok(fenced.includes(`File.${GAP}Body`));
  assert.equal(fleetlinkMessage(`Err.${GAP}Next\nline`), `Err.${GAP}Next line`);
});

test("a Linear wake excerpt collapses ASCII whitespace and keeps U+00A0", () => {
  const body = wakeBody({ excerpt: `  Hello.${NBSP}\nNext  ` });
  assert.equal(body.excerpt, `Hello.${NBSP} Next`);
});

test("a Zulip message read returns the stored U+00A0", async () => {
  const fake = new FakeZulip();
  const clock = { now: Date.parse("2026-10-09T10:00:00Z") };
  const tools = new HostedTools({
    seat: "GROK-WEB",
    scopes: ["zulip:read"],
    clientId: "https://grok.com/oauth/mcp-client.json",
    grantRef: "abcdef012345",
    paused: false,
    config,
    zulip: new ZulipClient({
      email: BOT_EMAIL,
      key: fake.key,
      ownerUserId: config.ownerUserId,
      fetch: fake.fetch,
      sleep: async (ms) => {
        clock.now += ms;
      },
      now: () => clock.now,
    }),
    key: fake.key,
    gate: memoryGate(clock),
    now: () => clock.now,
    sleep: async (ms) => {
      clock.now += ms;
    },
    log: () => {},
    usersCache: new Map(),
    channelsCache: new Map(),
  });
  const stored = `Stored.${GAP}Sentence.`;
  const message = fake.addMessage("Codex", "simple-tools", "gap", stored);
  const got = await tools.call("zulip_get_message", { message_id: message.id });
  assert.equal(got.isError, undefined);
  const line = got.content[0].text.split("\n").find((row) => row.startsWith("{"));
  assert.equal(JSON.parse(line).body, stored);
  assert.ok(got.content[0].text.includes(NBSP));
});

test("a GitHub workflow read keeps U+00A0 on the hosted wire", async () => {
  const pull = { title: `Title.${GAP}Here`, body: `Body.${GAP}There`, content: `line.${GAP}two\n` };
  const result = okResult(JSON.stringify(pull), pull);
  const router = {
    toolsByName: { gr_wf_pr_get: {} },
    call: async () => result,
  };
  const event = await toolCallEvent({ id: 7, name: "gr_wf_pr_get", args: {} }, router);
  // The text block and structuredContent each carry the three gaps.
  assert.equal([...event].filter((ch) => ch === NBSP).length, 6);
  const payload = JSON.parse(event.slice(event.indexOf("data: ") + 6));
  assert.equal(payload.result.content[0].text, JSON.stringify(pull));
  assert.equal(payload.result.structuredContent.body, pull.body);
});
