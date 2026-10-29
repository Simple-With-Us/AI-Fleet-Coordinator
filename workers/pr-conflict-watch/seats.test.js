import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { seatMentionFor, seatPrefixFor } from "./seats.js";

const pr = (ref) => ({ head: { ref } });

describe("seatPrefixFor", () => {
  it("takes the text before the first slash", () => {
    assert.equal(seatPrefixFor("gb-deployer/afc-123-fix"), "gb-deployer");
    assert.equal(seatPrefixFor("mm/pr-conflict-watch-zulip"), "mm");
    assert.equal(seatPrefixFor("a/b/c"), "a");
  });

  it("has no prefix for a branch with no seat", () => {
    assert.equal(seatPrefixFor("main"), null);
    assert.equal(seatPrefixFor(""), null);
    assert.equal(seatPrefixFor("/leading-slash"), null);
    assert.equal(seatPrefixFor(undefined), null);
  });
});

describe("seatMentionFor", () => {
  it("maps the fixed seats", () => {
    assert.equal(seatMentionFor(pr("mm/afc-1-x")), "@**MiniMax**");
    assert.equal(seatMentionFor(pr("minimax/afc-1-x")), "@**MiniMax**");
    assert.equal(seatMentionFor(pr("cursor/afc-1-x")), "@**Cursor**");
    assert.equal(seatMentionFor(pr("codex/afc-1-x")), "@**Codex**");
    assert.equal(seatMentionFor(pr("ag/afc-1-x")), "@**Antigravity**");
    assert.equal(seatMentionFor(pr("claude/afc-1-x")), "@**Claude**");
  });

  it("gives GrokBot no mention", () => {
    assert.equal(seatMentionFor(pr("grokbot/afc-1-x")), null);
  });

  it("maps the generic gb- and bf- prefixes with a capitalised role", () => {
    assert.equal(seatMentionFor(pr("gb-deployer/afc-1-x")), "@**GB-Deployer**");
    assert.equal(seatMentionFor(pr("bf-builder/afc-1-x")), "@**BF-Builder**");
    assert.equal(seatMentionFor(pr("gb-ops/afc-1-x")), "@**GB-Ops**");
  });

  it("gives no mention for an unknown prefix", () => {
    assert.equal(seatMentionFor(pr("someone-else/afc-1-x")), null);
    assert.equal(seatMentionFor(pr("main")), null);
    assert.equal(seatMentionFor({}), null);
    assert.equal(seatMentionFor(undefined), null);
  });

  it("never mentions @all or @everyone", () => {
    const refs = ["mm/x", "gb-deployer/x", "bf-builder/x", "cursor/x", "grokbot/x", "nope/x"];
    for (const ref of refs) {
      const mention = seatMentionFor(pr(ref));
      if (mention === null) continue;
      assert.ok(!/@all\b/i.test(mention), `${ref} produced ${mention}`);
      assert.ok(!/@everyone\b/i.test(mention), `${ref} produced ${mention}`);
    }
  });
});
