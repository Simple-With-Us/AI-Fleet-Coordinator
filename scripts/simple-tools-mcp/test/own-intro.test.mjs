import test from "node:test";
import assert from "node:assert/strict";
import { ownIntro, stripOwnIntro } from "../src/contract.js";

test("ownIntro accepts either folder glyph", () => {
  for (const glyph of ["\u{1F4C1}", "\u{1F4C2}"]) {
    const found = ownIntro("[MC", `[MC: LL.1.S ${glyph}AFC] Body`);
    assert.ok(found, glyph);
    assert.equal(found.rest, " Body");
  }
});

test("ownIntro accepts a bare arrow intro with no folder", () => {
  const found = ownIntro("[MC", "[MC → CODEX] Body");
  assert.ok(found);
  assert.equal(found.rest, " Body");
});

test("stripOwnIntro removes either glyph form but keeps foreign intros", () => {
  assert.equal(stripOwnIntro("MC", "[MC: LL.1.S \u{1F4C2}AFC] Body"), "Body");
  assert.equal(stripOwnIntro("MC", "[MC: LL.1.S \u{1F4C1}AFC] Body"), "Body");
  assert.equal(stripOwnIntro("MC", "[MM: HH.1.M \u{1F4C2}AFC] Body"), "[MM: HH.1.M \u{1F4C2}AFC] Body");
});
