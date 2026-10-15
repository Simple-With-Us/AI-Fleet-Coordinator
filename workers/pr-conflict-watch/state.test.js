import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { decideStateTransition } from "./state.js";

describe("decideStateTransition", () => {
  it("ignores blocked/unstable churn (no KV)", () => {
    assert.equal(decideStateTransition(null, "blocked"), "noop");
    assert.equal(decideStateTransition(null, "unstable"), "noop");
    assert.equal(decideStateTransition("blocked", "unstable"), "noop");
  });

  it("alerts once when a PR becomes dirty", () => {
    assert.equal(decideStateTransition(null, "dirty"), "alert");
    assert.equal(decideStateTransition("dirty", "dirty"), "noop");
  });

  it("recovers when leaving dirty for any non-alert state", () => {
    assert.equal(decideStateTransition("dirty", "clean"), "recover");
    assert.equal(decideStateTransition("dirty", "blocked"), "recover");
  });

  it("clears KV when the PR closes", () => {
    assert.equal(decideStateTransition("dirty", "closed"), "clear");
    assert.equal(decideStateTransition(null, "closed"), "noop");
  });
});
