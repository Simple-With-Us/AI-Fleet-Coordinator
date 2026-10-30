// Tests for the OpenCode letter plugin (AFC-564): node --test scripts/agent_sync/tests/opencode_letter_plugin.test.mjs
// The OpenCode side (session.get and session.update) and the platform-letter hook are both faked, except in the last
// group, which runs the real spawn plumbing against a small script.
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import plugin, { createLetters, NO_LETTER_MARK, optedOut, readSettings, runHook, sharedServer } from "../opencode_letter_plugin/index.js";

const settle = () => new Promise((resolve) => setImmediate(resolve));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Wait for every queue of the engine to go idle.
async function idle(letters) {
  for (let i = 0; i < 5; i++) {
    await Promise.all([...letters.sessions.values()].map((st) => st.chain));
    await settle();
  }
}

function harness({ sessions = {}, answers = {}, failUpdates = 0, settleMs = 0 } = {}) {
  const calls = [];
  const writes = [];
  const reads = [];
  let failures = failUpdates;
  let clock = 1_000_000;
  const api = {
    get: async (sid) => {
      reads.push(sid);
      if (!(sid in sessions)) throw new Error("not found");
      return sessions[sid];
    },
    update: async (sid, title) => {
      if (failures > 0) {
        failures--;
        throw new Error("refused");
      }
      writes.push([sid, title]);
      if (sid in sessions) sessions[sid] = { ...sessions[sid], title }; // the chat has the title it was given
    },
  };
  const run = async (payload) => {
    calls.push(payload);
    const answer = answers[payload.event];
    return typeof answer === "function" ? answer(payload) : answer === undefined ? {} : answer;
  };
  const letters = createLetters({ api, run, now: () => clock, settleMs });
  return {
    letters,
    sessions,
    calls,
    writes,
    reads,
    advance: (ms) => {
      clock += ms;
    },
    emit: (type, data, seq) => letters.onEvent({ type, data, durable: seq === undefined ? undefined : { seq } }),
    // Someone names the chat: it has the name, and the event says so.
    rename: (sid, title) => {
      sessions[sid] = { ...sessions[sid], title };
      letters.onEvent({ type: "session.renamed", data: { sessionID: sid, title } });
    },
  };
}

const ids = (calls) => calls.map((c) => c.event + ":" + c.session_id);

test("a new chat is started with its parent and titled from the hook's answer", async () => {
  const h = harness({ answers: { start: { label: "AA", title: "AA: New Chat" } } });
  h.emit("session.created", { sessionID: "ses_a", parentID: undefined });
  await idle(h.letters);
  assert.deepEqual(h.calls, [{ event: "start", session_id: "ses_a", parent_session_id: "", source: "startup", title: "" }]);
  assert.deepEqual(h.writes, [["ses_a", "AA: New Chat"]]);
});

test("a sub-chat is started after its parent, and says who its parent is", async () => {
  const h = harness({
    sessions: { ses_p: { id: "ses_p", title: "Plan the Work" }, ses_c: { id: "ses_c", parentID: "ses_p" } },
  });
  h.emit("session.created", { sessionID: "ses_c", parentID: "ses_p" });
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:ses_p", "start:ses_c"]);
  assert.equal(h.calls[0].title, "Plan the Work");
  assert.equal(h.calls[0].source, "resume");
  assert.equal(h.calls[1].parent_session_id, "ses_p");
  assert.equal(h.calls[1].source, "startup");
});

test("a parent that is a cycle or too deep does not hang the queue", async () => {
  const h = harness({
    sessions: { a: { parentID: "b" }, b: { parentID: "a" } },
  });
  h.emit("session.inbox.enqueued", { sessionID: "a", item: { type: "user", payload: { text: "hi there" } } });
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:b", "start:a", "prompt:a"]);
  assert.equal(h.calls[1].parent_session_id, "b");
});

test("a chat with a past that is first seen through a prompt is started first", async () => {
  const h = harness({ sessions: { s1: { id: "s1", title: "Older Work" } } });
  h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "go on" } } }, 7);
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "prompt:s1"]);
  assert.equal(h.calls[0].title, "Older Work");
});

test("a chat first seen through its very first prompt is registered and named by that prompt alone", async () => {
  const h = harness({
    sessions: { s1: { id: "s1" } },
    answers: { prompt: { label: "AB", title: "AB: Fix Login" } },
  });
  const say = (text, seq) =>
    h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text } } }, seq);
  say("fix the login", 1);
  await idle(h.letters);
  assert.deepEqual(h.calls, [
    { event: "prompt", session_id: "s1", parent_session_id: "", prompt: "fix the login", title: "" },
  ]);
  assert.deepEqual(h.writes, [["s1", "AB: Fix Login"]]); // no placeholder first
  say("and the logout"); // a steady-state prompt costs no hook
  await idle(h.letters);
  assert.equal(h.calls.length, 1);
  h.advance(24 * 60 * 1000);
  say("still nothing");
  await idle(h.letters);
  assert.equal(h.calls.length, 1);
  h.advance(2 * 60 * 1000); // half an hour since the hook last heard of the chat
  say("one more thing");
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["prompt:s1", "prompt:s1"]);
});

test("a chat seen at its creation is titled New Chat, then named by its first prompt", async () => {
  const h = harness({
    answers: { start: { label: "AB", title: "AB: New Chat" }, prompt: { label: "AB", title: "AB: Fix Login" } },
  });
  h.emit("session.created", { sessionID: "s1", parentID: "" });
  await idle(h.letters);
  h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "fix the login" } } });
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "prompt:s1"]);
  assert.equal(h.calls[1].prompt, "fix the login");
  assert.equal(h.calls[1].title, "AB: New Chat"); // what the chat showed when the prompt came
  assert.deepEqual(h.writes, [["s1", "AB: New Chat"], ["s1", "AB: Fix Login"]]);
});

test("a prompt is cut to what the hook reads, and only a user's prompt counts", async () => {
  const h = harness({ sessions: { s1: {} } });
  h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "x".repeat(9000) } } }, 1);
  await idle(h.letters);
  assert.equal(h.calls.find((c) => c.event === "prompt").prompt.length, 4096);
  const g = harness({ sessions: { s2: {} } });
  g.emit("session.inbox.enqueued", { sessionID: "s2", item: { type: "synthetic", payload: { text: "note" } } });
  await idle(g.letters);
  assert.deepEqual(ids(g.calls), ["start:s2"]); // seen, started, but no prompt
});

test("our own title coming back is ignored, and a rename someone made is sent", async () => {
  const h = harness({
    sessions: { s1: {} },
    answers: {
      start: { label: "AC", title: "AC: New Chat" },
      rename: (p) => ({ label: "AC", title: "AC: " + p.title }),
    },
  });
  h.emit("session.created", { sessionID: "s1", parentID: "" });
  await idle(h.letters);
  h.emit("session.renamed", { sessionID: "s1", title: "AC: New Chat" }); // the echo of our update
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1"]);
  h.rename("s1", "Billing Audit"); // the owner's own name
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "rename:s1"]);
  assert.deepEqual(h.writes.at(-1), ["s1", "AC: Billing Audit"]);
  h.emit("session.renamed", { sessionID: "s1", title: "AC: Billing Audit" });
  await idle(h.letters);
  assert.equal(h.calls.length, 2);
});

test("a name given while the hook was working is kept, with the letter in front of it", async () => {
  // OpenCode names a chat from its first prompt about the time the hook answers: the hook's own title is not
  // written over that name.
  const sessions = { s1: { id: "s1" } };
  const h = harness({
    sessions,
    answers: {
      prompt: () => {
        sessions.s1.title = "Mutex Explained"; // OpenCode got there first
        return { label: "AA", title: "AA: Explain a Mutex" };
      },
      rename: (p) => ({ label: "AA", title: "AA: " + p.title }),
    },
  });
  h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "explain a mutex" } } }, 1);
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["prompt:s1", "rename:s1"]);
  assert.equal(h.calls[1].title, "Mutex Explained");
  assert.deepEqual(h.writes, [["s1", "AA: Mutex Explained"]]); // never "AA: Explain a Mutex"
  h.emit("session.renamed", { sessionID: "s1", title: "Mutex Explained" }); // the event of OpenCode's own naming
  await idle(h.letters);
  assert.equal(h.calls.length, 2);
});

test("a late event for an older title is judged by the title the chat has now", async () => {
  const h = harness({
    sessions: { s1: {} },
    answers: { start: { label: "AB", title: "AB: New Chat" }, rename: (p) => ({ label: "AB", title: "AB: " + p.title }) },
  });
  h.emit("session.created", { sessionID: "s1", parentID: "" });
  await idle(h.letters);
  h.rename("s1", "Billing");
  await idle(h.letters);
  assert.deepEqual(h.writes, [["s1", "AB: New Chat"], ["s1", "AB: Billing"]]);
  // The events of the first write and of OpenCode's own naming, reaching the queue last: the chat is "AB: Billing".
  h.emit("session.renamed", { sessionID: "s1", title: "AB: New Chat" });
  h.emit("session.renamed", { sessionID: "s1", title: "Billing" });
  h.emit("session.renamed", { sessionID: "s1", title: "AB: Billing" });
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "rename:s1"]); // nothing more was asked of the hook
});

test("a title OpenCode refuses is tried again, but not before a minute is over", async () => {
  const h = harness({ sessions: { s1: {} }, answers: { start: { label: "AD", title: "AD: New Chat" } }, failUpdates: 1 });
  h.emit("session.created", { sessionID: "s1", parentID: "" });
  await idle(h.letters);
  assert.deepEqual(h.writes, []);
  h.emit("session.execution.started", { sessionID: "s1" });
  await idle(h.letters);
  assert.deepEqual(h.writes, []);
  h.advance(61 * 1000);
  h.emit("session.execution.started", { sessionID: "s1" });
  await idle(h.letters);
  assert.deepEqual(h.writes, [["s1", "AD: New Chat"]]);
});

test("a hook that did not run is asked again after ten minutes, not on every prompt", async () => {
  const h = harness({ sessions: { s1: {} }, answers: { start: null, prompt: null } });
  const say = (seq) =>
    h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "hi there" } } }, seq);
  say(1);
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["prompt:s1"]);
  h.advance(5 * 60 * 1000);
  say();
  await idle(h.letters);
  assert.equal(h.calls.length, 1);
  h.advance(6 * 60 * 1000);
  say();
  await idle(h.letters);
  assert.equal(h.calls.length, 2);
});

test("archive closes the registration, unarchive starts the chat again, delete forgets it", async () => {
  const h = harness({ sessions: { s1: { id: "s1" } }, answers: { start: { label: "AE", title: "AE: New Chat" } } });
  h.emit("session.created", { sessionID: "s1", parentID: "" });
  await idle(h.letters);
  h.emit("session.archived", { sessionID: "s1" });
  await idle(h.letters);
  assert.deepEqual(h.calls.at(-1), { event: "end", session_id: "s1", reason: "archived" });
  h.emit("session.unarchived", { sessionID: "s1" });
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "end:s1", "start:s1"]);
  assert.equal(h.calls.at(-1).source, "resume");
  h.emit("session.deleted", { sessionID: "s1" });
  await idle(h.letters);
  assert.deepEqual(h.calls.at(-1), { event: "end", session_id: "s1", reason: "deleted" });
  assert.equal(h.letters.sessions.has("s1"), false);
  h.emit("session.usage.updated", { sessionID: "s1" }); // a late event of a deleted chat
  await idle(h.letters);
  assert.equal(h.calls.length, 4);
});

test("an archived chat that is first seen holds no letter, and a chat that cannot be read is not registered", async () => {
  const h = harness({ sessions: { old: { time: { archived: 5 } } } });
  h.emit("session.execution.started", { sessionID: "old" });
  h.emit("session.execution.started", { sessionID: "missing" });
  await idle(h.letters);
  assert.deepEqual(h.calls, []);
});

test("a chat first seen through any event is started with its stored title", async () => {
  const h = harness({ sessions: { s9: { id: "s9", title: "Older Work" } } });
  h.emit("session.step.started", { sessionID: "s9" });
  h.emit("session.text.delta", { sessionID: "s9" });
  await idle(h.letters);
  assert.deepEqual(h.calls, [{ event: "start", session_id: "s9", parent_session_id: "", source: "resume", title: "Older Work" }]);
});

test("events that are not a chat's, and malformed ones, start nothing and throw nothing", async () => {
  const h = harness();
  for (const event of [
    null,
    {},
    { type: "model.updated", data: {} },
    { type: "session.created", data: {} },
    { type: "session.created" },
    { type: 7, data: { sessionID: "x" } },
    { type: "session.renamed", data: { sessionID: "x", title: 5 } },
  ]) {
    h.letters.onEvent(event);
  }
  await idle(h.letters);
  assert.deepEqual(h.calls.filter((c) => c.event === "start" && c.session_id !== "x"), []);
});

test("at most three hooks run at once", async () => {
  let live = 0;
  let peak = 0;
  const api = { get: async () => ({}), update: async () => {} };
  const run = async () => {
    live++;
    peak = Math.max(peak, live);
    await new Promise((resolve) => setTimeout(resolve, 20));
    live--;
    return {};
  };
  const letters = createLetters({ api, run, settleMs: 0 });
  for (let i = 0; i < 10; i++) letters.onEvent({ type: "session.created", data: { sessionID: "s" + i, parentID: "" } });
  await idle(letters);
  assert.equal(peak, 3);
});

// ---- who takes a letter: the caller's environment is read only where it is the caller's -----------------------

test("settings: off switches and launcher sessions apply in a private server, where the environment is the caller's", () => {
  assert.equal(readSettings({}, { SESSION_LETTER: "0" }, false), null);
  assert.equal(readSettings({}, { OPENCODE_LETTER: "0" }, false), null);
  assert.equal(readSettings({}, { AGENT_LAUNCHER: "botfleet" }, false), null);
  assert.equal(readSettings({}, { AGENT_LAUNCH_SEAT: "BF" }, false), null);
  const mac = readSettings(undefined, { PLATFORM_LETTER_BIN: "/x/platform-letter" }, false);
  assert.equal(mac.command, "/x/platform-letter");
  assert.deepEqual(mac.args, ["hook", "--platform", "opencode"]);
  const server = readSettings({ location: "Server", remote: "mac", command: "/y/pl", log: "/tmp/l" }, {}, false);
  assert.deepEqual(server.args, ["hook", "--platform", "opencode", "--location", "server", "--remote", "mac"]);
  assert.equal(server.command, "/y/pl");
  assert.equal(server.log, "/tmp/l");
  const odd = readSettings({ location: "cloud", remote: "-oProxyCommand=x" }, {}, false);
  assert.deepEqual(odd.args, ["hook", "--platform", "opencode"]); // neither is passed on
  assert.equal(odd.settleMs, 1500);
  assert.equal(readSettings({ settle: 0 }, {}, false).settleMs, 0);
  assert.equal(readSettings({ settle: 99999 }, {}, false).settleMs, 1500); // out of range: the default
});

test("the shared service does not take its environment for the caller's, and the hook is run without it", () => {
  // The first client to start `opencode serve --service` leaves its environment on the process for every later client.
  const env = {
    AGENT_LAUNCHER: "botfleet",
    AGENT_LAUNCH_SEAT: "BF-TEST",
    SESSION_LETTER: "0",
    OPENCODE_LETTER: "0",
    SESSION_LETTER_LOCATION: "mac",
  };
  const shared = readSettings({}, env, true);
  assert.notEqual(shared, null); // not switched off for everyone
  assert.deepEqual(shared.env, { SESSION_LETTER_LOCATION: "mac" }); // the hook's own checks cannot switch it off either
  assert.equal(env.AGENT_LAUNCHER, "botfleet"); // the process's own environment is not touched
  assert.equal(readSettings({}, env, false), null);
  assert.equal(readSettings({ shared: true }, env, false).shared, true); // the option says how the process is used
  assert.equal(readSettings({ shared: false }, env, true), null);
});

test("a process that serves several clients is told from the private server a client starts", () => {
  const bun = (...args) => ["bun", "/$bunfs/root/opencode", ...args];
  assert.equal(sharedServer(bun("serve", "--service")), true);
  assert.equal(sharedServer(bun("serve", "--hostname", "100.1.2.3", "--port", "62306")), true);
  assert.equal(sharedServer(bun("web")), true);
  assert.equal(sharedServer(bun("serve", "--stdio")), false); // `run --standalone`
  assert.equal(sharedServer(bun("serve", "--stdio", "--port", "0")), false); // `acp`
  assert.equal(sharedServer(bun()), false);
  assert.equal(sharedServer(["node", "/x/index.test.mjs"]), false);
  assert.equal(sharedServer([]), false);
});

test("a chat marked to take no letter is told by its title or its metadata", () => {
  assert.equal(NO_LETTER_MARK, "[no-letter]");
  assert.equal(optedOut("[no-letter] pr-sweep", undefined), true);
  assert.equal(optedOut("  [No-Letter] x", undefined), true);
  assert.equal(optedOut("Fix Login", { platform_letter: "off" }), true);
  assert.equal(optedOut("", { platform_letter: " OFF " }), true);
  assert.equal(optedOut("Fix Login", { platform_letter: "on" }), false);
  assert.equal(optedOut("The [no-letter] mark in a title", null), false); // only a mark at the start counts
  assert.equal(optedOut("", undefined), false);
  assert.equal(optedOut(undefined, "off"), false);
});

test("the hook is run with the environment the settings name, and without the caller's in the shared service", async () => {
  const { file } = script('env | grep -E "^(AGENT_LAUNCHER|AGENT_LAUNCH_SEAT|SESSION_LETTER|OPENCODE_LETTER|KEPT)=" | sort > "$0.env"\ncat > /dev/null');
  const env = { PATH: process.env.PATH, AGENT_LAUNCHER: "botfleet", AGENT_LAUNCH_SEAT: "BF-TEST", SESSION_LETTER: "0", KEPT: "yes" };
  const shared = readSettings({ command: file }, env, true);
  await runHook(shared, { event: "start" });
  assert.equal(readFileSync(file + ".env", "utf8"), "KEPT=yes\n");
  const own = readSettings({ command: file, shared: false }, { PATH: process.env.PATH, KEPT: "yes" }, false);
  await runHook(own, { event: "start" });
  assert.equal(readFileSync(file + ".env", "utf8"), "KEPT=yes\n");
});

test("a chat whose title carries the mark takes no letter, and neither does what it starts", async () => {
  const h = harness({
    sessions: {
      sweep: { id: "sweep", title: "[no-letter] pr-sweep" },
      kid: { id: "kid", parentID: "sweep", title: "Explore the repo" },
      other: { id: "other", title: "" },
    },
    answers: { start: { label: "AA", title: "AA: New Chat" } },
  });
  h.emit("session.created", { sessionID: "sweep", parentID: "" });
  await idle(h.letters);
  h.emit("session.inbox.enqueued", { sessionID: "sweep", item: { type: "user", payload: { text: "review the pr" } } });
  h.emit("session.created", { sessionID: "kid", parentID: "sweep" });
  h.emit("session.inbox.enqueued", { sessionID: "kid", item: { type: "user", payload: { text: "look around" } } });
  h.emit("session.renamed", { sessionID: "sweep", title: "[no-letter] pr-sweep" });
  await idle(h.letters);
  h.emit("session.archived", { sessionID: "sweep" });
  h.emit("session.archived", { sessionID: "kid" });
  await idle(h.letters);
  assert.deepEqual(h.calls, []); // not one hook, for the chats, their prompts or their ends
  assert.deepEqual(h.writes, []);
  h.emit("session.created", { sessionID: "other", parentID: "" }); // a chat of the owner's is not affected
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:other"]);
});

test("session metadata that says off keeps a chat from taking a letter", async () => {
  const h = harness({ sessions: { api1: { id: "api1", title: "", metadata: { platform_letter: "off" } } } });
  h.emit("session.created", { sessionID: "api1", parentID: "" });
  h.emit("session.inbox.enqueued", { sessionID: "api1", item: { type: "user", payload: { text: "hello there" } } }, 1);
  await idle(h.letters);
  assert.deepEqual(h.calls, []);
});

test("a title given right after the chat is created is seen before the chat is registered", async () => {
  // `opencode run --title "[no-letter] x"`: the session is created, renamed and prompted within milliseconds.
  const sessions = { s1: { id: "s1" } };
  const h = harness({ sessions, settleMs: 30 });
  h.emit("session.created", { sessionID: "s1" }, 0);
  sessions.s1 = { id: "s1", title: "[no-letter] pr-sweep" };
  h.emit("session.renamed", { sessionID: "s1", title: "[no-letter] pr-sweep" }, 1);
  h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "review it" } } }, 2);
  await sleep(60);
  await idle(h.letters);
  assert.deepEqual(h.calls, []);
});

test("an empty new chat is registered when its first prompt does not come, and a prompt that does registers and names it at once", async () => {
  const quiet = harness({ sessions: { s1: { id: "s1" } }, settleMs: 30, answers: { start: { label: "AB", title: "AB: New Chat" } } });
  quiet.emit("session.created", { sessionID: "s1", parentID: "" }, 0);
  await sleep(10);
  assert.deepEqual(quiet.calls, []); // still waiting for a prompt or a title
  await sleep(50);
  await idle(quiet.letters);
  assert.deepEqual(ids(quiet.calls), ["start:s1"]);
  assert.deepEqual(quiet.writes, [["s1", "AB: New Chat"]]);

  const oneShot = harness({
    sessions: { s2: { id: "s2" } },
    settleMs: 5000, // a prompt does not wait for the settle to run out
    answers: { prompt: { label: "AC", title: "AC: Fix Login" } },
  });
  oneShot.emit("session.created", { sessionID: "s2", parentID: "" }, 0);
  oneShot.emit("session.inbox.enqueued", { sessionID: "s2", item: { type: "user", payload: { text: "fix the login" } } }, 1);
  await idle(oneShot.letters);
  assert.deepEqual(ids(oneShot.calls), ["prompt:s2"]); // one hook for the registration and the title, no placeholder
  assert.deepEqual(oneShot.writes, [["s2", "AC: Fix Login"]]);
});

// ---- forks, throttle, storms, parents, copies ---------------------------------------------------------------------

test("a fork is started as a fork with the title it was copied with, and is not the other chat's child", async () => {
  // OpenCode 2.1.1: the fork gets "<title> (fork #1)" with the letter in it, `fork.sessionID` and no `parentID`, and a
  // `session.forked` event (not `session.created`) whose `parentID` is the chat it was copied from.
  const h = harness({
    sessions: {
      f1: { id: "f1", title: "AA: Reply with the Single Word Ok (fork #1)", fork: { sessionID: "orig" } },
      f2: { id: "f2", title: "AA: Another (fork #1)", fork: { sessionID: "orig" } },
    },
    answers: { start: (p) => ({ label: "AB", title: "AB: " + p.title.replace(/^AA: /, "") }) },
  });
  h.emit("session.forked", { sessionID: "f1", parentID: "orig", boundary: { type: "through" } }, 0);
  await idle(h.letters);
  assert.deepEqual(h.calls, [
    { event: "start", session_id: "f1", parent_session_id: "", source: "fork", title: "AA: Reply with the Single Word Ok (fork #1)" },
  ]);
  assert.deepEqual(h.writes, [["f1", "AB: Reply with the Single Word Ok (fork #1)"]]);
  // A fork the plugin first sees later (through a prompt) is a fork all the same.
  h.emit("session.inbox.enqueued", { sessionID: "f2", item: { type: "user", payload: { text: "go on" } } }, 9);
  await idle(h.letters);
  assert.deepEqual(h.calls[1], { event: "start", session_id: "f2", parent_session_id: "", source: "fork", title: "AA: Another (fork #1)" });
  assert.equal(h.calls[2].event, "prompt");
});

test("a hook that ran and left the chat without a letter is asked again after ten minutes, not twenty-five", async () => {
  // The hook exits 0 with a line of context and no label when the Operator's host does not answer.
  const h = harness({ sessions: { s1: {} }, answers: { start: { context: "No Operator letter yet" }, prompt: { context: "No Operator letter yet" } } });
  const say = (seq) =>
    h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "hi there" } } }, seq);
  say(1);
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["prompt:s1"]);
  h.advance(3 * 60 * 1000); // minute 3
  say();
  await idle(h.letters);
  h.advance(5 * 60 * 1000); // minute 8
  say();
  await idle(h.letters);
  assert.equal(h.calls.length, 1); // too soon
  h.advance(3 * 60 * 1000); // minute 11
  say();
  await idle(h.letters);
  assert.equal(h.calls.length, 2);
});

test("a chat that has a letter and no name yet is not held to the half hour, so its next prompt names it", async () => {
  // A first prompt of only a link gives the hook nothing to name the chat from.
  let named = false;
  const h = harness({
    sessions: { s1: { id: "s1" } },
    answers: {
      start: { label: "AA", title: "AA: New Chat" },
      prompt: () => (named ? { label: "AA", title: "AA: Fix the Login Page Redirect" } : { label: "AA" }),
    },
  });
  h.emit("session.created", { sessionID: "s1", parentID: "" }, 0);
  await idle(h.letters);
  const say = (words) =>
    h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: words } } });
  say("https://github.com/org/repo/pull/12");
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "prompt:s1"]);
  h.advance(60 * 1000);
  named = true;
  say("please fix the login page redirect");
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:s1", "prompt:s1", "prompt:s1"]); // one minute later, not twenty-six
  assert.deepEqual(h.writes.at(-1), ["s1", "AA: Fix the Login Page Redirect"]);
  say("ship it"); // named now: a steady-state prompt costs nothing again
  await idle(h.letters);
  assert.equal(h.calls.length, 3);
});

test("a chat with a letter and a placeholder title is not named yet either", async () => {
  const h = harness({ sessions: { s1: { id: "s1" } }, answers: { start: { label: "AA", title: "AA: New Chat" }, prompt: { label: "AA", title: "AA: New Chat" } } });
  h.emit("session.created", { sessionID: "s1", parentID: "" }, 0);
  await idle(h.letters);
  for (let i = 0; i < 3; i++) {
    h.emit("session.inbox.enqueued", { sessionID: "s1", item: { type: "user", payload: { text: "😀" } } });
    await idle(h.letters);
  }
  assert.deepEqual(ids(h.calls), ["start:s1", "prompt:s1", "prompt:s1", "prompt:s1"]);
});

test("the events of an archived chat, or of one that cannot be read, do not read the chat again and again", async () => {
  const h = harness({ sessions: { old: { id: "old", time: { archived: 5 } } } });
  for (let i = 0; i < 2000; i++) h.emit("session.text.delta", { sessionID: "old" });
  await idle(h.letters);
  assert.equal(h.reads.filter((s) => s === "old").length, 1);
  const g = harness({ sessions: {} });
  for (let i = 0; i < 500; i++) g.emit("session.text.delta", { sessionID: "missing" });
  await idle(g.letters);
  assert.equal(g.reads.length, 1);
  g.emit("session.text.delta", { sessionID: "missing" }); // still within the minute
  await idle(g.letters);
  assert.equal(g.reads.length, 1);
  g.advance(61 * 1000);
  g.emit("session.text.delta", { sessionID: "missing" });
  await idle(g.letters);
  assert.equal(g.reads.length, 2);
  // A prompt, or an unarchive, tries a chat that was held off.
  g.sessions.missing = { id: "missing", title: "Back" };
  g.emit("session.inbox.enqueued", { sessionID: "missing", item: { type: "user", payload: { text: "hello again" } } });
  await idle(g.letters);
  assert.ok(ids(g.calls).includes("start:missing"));
  h.sessions.old = { id: "old", title: "Old" };
  h.emit("session.unarchived", { sessionID: "old" });
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["start:old"]);
});

test("a sub-chat made while its parent's hook is still running waits for the parent, so it takes the pair after it", async () => {
  const sessions = {
    par: { id: "par", title: "" },
    kid: { id: "kid", parentID: "par", title: "" },
  };
  const order = [];
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  const api = { get: async (sid) => sessions[sid], update: async () => {} };
  const run = async (payload) => {
    order.push("begin " + payload.event + ":" + payload.session_id);
    if (payload.session_id === "par") await gate; // the Mac is slow to answer the parent
    order.push("end " + payload.event + ":" + payload.session_id);
    return { label: payload.session_id === "par" ? "AA" : "AB", title: "" };
  };
  const letters = createLetters({ api, run, settleMs: 0 });
  letters.onEvent({ type: "session.inbox.enqueued", data: { sessionID: "par", item: { type: "user", payload: { text: "use a subagent" } } }, durable: { seq: 1 } });
  await sleep(20);
  letters.onEvent({ type: "session.created", data: { sessionID: "kid", parentID: "par" }, durable: { seq: 0 } });
  await sleep(40);
  assert.deepEqual(order, ["begin prompt:par"]); // the child has not gone ahead
  release();
  await idle(letters);
  assert.deepEqual(order, ["begin prompt:par", "end prompt:par", "begin start:kid", "end start:kid"]);
});

test("a parent whose hook did not answer is asked once more before its child, but not within ten minutes", async () => {
  const sessions = { par: { id: "par", title: "" }, k1: { id: "k1", parentID: "par" }, k2: { id: "k2", parentID: "par" } };
  const parentAnswers = [{ label: "AA", title: "" }];
  const h = harness({
    sessions,
    answers: { start: (p) => (p.session_id === "par" ? parentAnswers.shift() : { label: "AB", title: "" }), prompt: null },
  });
  h.emit("session.inbox.enqueued", { sessionID: "par", item: { type: "user", payload: { text: "go" } } }, 1);
  await idle(h.letters);
  h.emit("session.created", { sessionID: "k1", parentID: "par" }, 0);
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["prompt:par", "start:k1"]); // the parent's retry is not due
  h.advance(11 * 60 * 1000);
  h.emit("session.created", { sessionID: "k2", parentID: "par" }, 0);
  await idle(h.letters);
  assert.deepEqual(ids(h.calls), ["prompt:par", "start:k1", "start:par", "start:k2"]);
  assert.equal(h.letters.sessions.get("par").label, "AA");
});

test("the copies of the plugin OpenCode loads for each directory share one engine and handle an event once", async () => {
  const { file } = script('cat > /dev/null\necho "$$" >> "$0.runs"\n' +
    'echo \'{"platform_letter": {"event": "start", "platform": "opencode", "label": "AH", "title": "AH: New Chat"}}\'');
  const events = [
    { type: "session.created", data: { sessionID: "ses_a", parentID: "" }, durable: { seq: 0 } },
    { type: "session.created", data: { sessionID: "ses_a", parentID: "" }, durable: { seq: 0 } },
  ];
  const options = { command: file, location: "server", remote: "mac", settle: 0, shared: false };
  const one = fakeContext(events, { options });
  const two = fakeContext(events, { options });
  const disposeOne = plugin.setup(one.ctx);
  const disposeTwo = plugin.setup(two.ctx);
  await sleep(500);
  await disposeOne();
  assert.ok(globalThis[Symbol.for("platform-letter-opencode.engine")], "the engine stays while another copy listens");
  await disposeTwo();
  assert.equal(globalThis[Symbol.for("platform-letter-opencode.engine")], undefined);
  const runs = readFileSync(file + ".runs", "utf8").trim().split("\n");
  assert.equal(runs.length, 1); // four deliveries of one event (two copies, each told it twice): one hook
  assert.equal(one.updates.length + two.updates.length, 1); // and the title was written once
});

test("a copy whose directory has gone away does not take the engine down with it", async () => {
  const { file } = script("cat > /dev/null\necho '{\"platform_letter\": {\"label\": \"AJ\", \"title\": \"AJ: New Chat\"}}'");
  const options = { command: file, settle: 0, shared: false };
  const dead = fakeContext([], { options });
  dead.ctx.session.get = async () => {
    throw new Error("the directory is gone");
  };
  dead.ctx.session.update = async () => {
    throw new Error("the directory is gone");
  };
  const live = fakeContext(
    [{ type: "session.created", data: { sessionID: "ses_b", parentID: "" }, durable: { seq: 0 } }],
    { options, sessions: { ses_b: { id: "ses_b", title: "" } } },
  );
  const disposeDead = plugin.setup(dead.ctx);
  const disposeLive = plugin.setup(live.ctx);
  await sleep(500);
  await disposeLive();
  await disposeDead();
  assert.deepEqual(live.updates, [{ sessionID: "ses_b", title: "AJ: New Chat" }]); // the second handle answered
});

// ---- the plugin as OpenCode loads it -----------------------------------------------------------------------

function fakeContext(events, { options = {}, sessions = {} } = {}) {
  const updates = [];
  const aborted = [];
  return {
    updates,
    aborted,
    ctx: {
      options,
      session: {
        get: async ({ sessionID }) => sessions[sessionID] ?? {},
        update: async (input) => {
          updates.push(input);
        },
      },
      event: {
        subscribe: ({ signal }) => {
          signal.addEventListener("abort", () => aborted.push(true));
          return (async function* () {
            for (const event of events) yield event;
          })();
        },
      },
    },
  };
}

function script(body) {
  const dir = mkdtempSync(join(tmpdir(), "letter-plugin-"));
  const file = join(dir, "platform-letter");
  writeFileSync(file, "#!/bin/sh\n" + body + "\n");
  chmodSync(file, 0o755);
  return { dir, file };
}

test("the plugin starts a chat through the real spawn and writes the title the hook prints", async () => {
  const { dir, file } = script(
    'cat > "$0.in"\n' +
      'echo \'{"platform_letter": {"event": "start", "platform": "opencode", "label": "AF", "title": "AF: New Chat"}}\'',
  );
  const log = join(dir, "log");
  const { ctx, updates } = fakeContext(
    [{ type: "session.created", data: { sessionID: "ses_live", parentID: "" } }],
    { options: { command: file, log, location: "server", remote: "mac", settle: 0, shared: false } },
  );
  const dispose = plugin.setup(ctx);
  assert.equal(plugin.id, "platform-letter-opencode");
  await new Promise((resolve) => setTimeout(resolve, 300));
  await dispose();
  assert.deepEqual(updates, [{ sessionID: "ses_live", title: "AF: New Chat" }]);
  const sent = JSON.parse(readFileSync(file + ".in", "utf8"));
  assert.deepEqual(sent, { event: "start", session_id: "ses_live", parent_session_id: "", source: "startup", title: "" });
  const kinds = readFileSync(log, "utf8").trim().split("\n").map((l) => JSON.parse(l).kind);
  assert.deepEqual(kinds, ["setup", "hook", "title", "dispose", "disposed"]);
  assert.doesNotMatch(readFileSync(log, "utf8"), /prompt/);
});

test("the plugin does nothing when it is switched off or lacks what it needs", () => {
  const { ctx } = fakeContext([], { options: {} });
  const saved = process.env.OPENCODE_LETTER;
  process.env.OPENCODE_LETTER = "0";
  try {
    assert.equal(plugin.setup(ctx), undefined);
  } finally {
    if (saved === undefined) delete process.env.OPENCODE_LETTER;
    else process.env.OPENCODE_LETTER = saved;
  }
  assert.equal(plugin.setup({ options: {}, session: {} }), undefined);
  assert.equal(plugin.setup(undefined), undefined);
});

test("runHook: a missing command, a hook that hangs and a hook that prints nothing", async () => {
  assert.equal(await runHook({ command: "/no/such/command", args: [], timeoutMs: 2000 }, {}), null);
  const hang = script("exec sleep 30");
  const started = Date.now();
  assert.equal(await runHook({ command: hang.file, args: [], timeoutMs: 200 }, {}), null);
  assert.ok(Date.now() - started < 5000);
  const quiet = script("cat > /dev/null");
  assert.deepEqual(await runHook({ command: quiet.file, args: [], timeoutMs: 2000 }, {}), {});
  const noisy = script('cat > /dev/null\necho "warning"\necho \'{"platform_letter": {"label": "AG"}}\'\necho "done"');
  assert.deepEqual(await runHook({ command: noisy.file, args: [], timeoutMs: 2000 }, {}), { label: "AG" });
});

test("a title and a prompt reach the hook only on its stdin, never in its command line", async () => {
  const { file } = script('echo "$@" > "$0.argv"\ncat > "$0.in"');
  const evil = '"; rm -rf / #';
  await runHook({ command: file, args: ["hook", "--platform", "opencode"], timeoutMs: 2000 }, { event: "rename", title: evil });
  assert.equal(readFileSync(file + ".argv", "utf8").trim(), "hook --platform opencode");
  assert.equal(JSON.parse(readFileSync(file + ".in", "utf8")).title, evil);
});
