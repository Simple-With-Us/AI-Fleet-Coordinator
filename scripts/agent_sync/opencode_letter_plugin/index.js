// OpenCode plugin: an Operator session letter, and the title "XX: Title", on every OpenCode chat (AFC-564, owner
// request Oct 29, 2026).  The Python side is `platform-letter hook --platform opencode`
// (agent_sync/platform_letter_opencode.py on the shared library platform_letter.py); this file is the part that has
// to run inside OpenCode: it watches the chat's events, runs that hook with them, and puts the title the hook answers
// with on the chat.
//
// What OpenCode v2.1.1 gives a plugin (read from live runs of this plugin and of a diagnostic one, on both hosts).
// - A plugin listed in the `plugins` array of opencode.json is loaded as `{ id, setup(ctx) }`.  `server(input)` and the
//   v1 SDK client (`client.session.update({ path, body })`) are NOT called for it, so the title is written with the
//   v2 context: `ctx.session.update({ sessionID, title })` (answers with nothing and emits `session.renamed`).
// - Whose process it is.  `opencode run` and `opencode mini` use the background service (`opencode serve --service`),
//   one detached process that serves every client, so the plugin's environment is that of whichever client started the
//   service days ago and says nothing about the client of a chat.  Only a private server (`serve --stdio`: what ACP,
//   which BotFleet uses, and `--standalone` start as the client's own child) has the client's environment.
// - The plugin is loaded when OpenCode first uses a directory, once for each directory, all in the one process, and
//   every copy is told the events of every chat the process serves (the copy of one directory saw the created, prompted
//   and forked events of a chat in another).  So one engine, kept on globalThis, handles each event once.
// - `ctx.event.subscribe()` carries the events: `session.created` (with `parentID` for a chat another chat started),
//   `session.forked` (a fork: `parentID` there is the chat it was copied from, and the session has no parent of its
//   own), `session.inbox.enqueued` (a prompt, with the text), `session.renamed`, `session.archived`,
//   `session.unarchived` and `session.deleted`.  `ctx.session.get({ sessionID })` answers with the session, which has
//   `parentID`, `fork` (the chat a fork was copied from), `title` (absent until the chat is named), `metadata` and
//   `time.archived`.
// - `opencode run` makes the session and sends its prompt within milliseconds (a title given with --title arrives as a
//   rename between the two), and so does the interactive client: nothing in an event tells a one-shot from a chat.
// - OpenCode titles a chat from its first prompt only while the chat has no title, so a letter put on the chat first
//   (`AB: New Chat`) is replaced by the hook's own words from the first prompt and not by OpenCode's.
//
// The rules.
// - A hook never blocks a chat and neither does this plugin: everything here is a promise that is awaited by nothing
//   OpenCode waits for, every failure is swallowed, and nothing is printed.  Events of one chat are handled in order
//   (one queue per chat), and at most MAX_HOOKS hooks run at once.
// - The hook is the only thing that knows letters, titles and the Operator.  The plugin sends it `start`, `prompt`,
//   `rename` and `end` events as JSON on stdin and reads one JSON line back (`platform_letter`), whose `title` it
//   writes to the chat.  A prompt is sent for the first prompt of a chat and then at most about every half hour (the
//   hook marks the chat seen and keeps the Operator's words current); a rename someone made is sent when it happens.
//   A chat the Operator has no letter for, or that has no name yet, is not held to that: the next prompt asks again
//   (a hook that did not answer, ten minutes later), because that prompt may be the one that registers or names it.
// - A title that could not be written (OpenCode refused the update) is tried again on the next event, no sooner than a
//   minute later.  A chat that cannot be read is not asked about again for a minute; an archived one, not until it is
//   unarchived or prompted.
// - A chat another chat started is registered after its parent: the parent is started first, or waited for when its
//   hook is still running, so the Operator can give the child the pair after the parent's.
// - A fork is a new main chat whose title was copied from the other chat, letter and all: it is started as a fork, which
//   keeps the words and drops the other chat's letter.
// - Titles and prompts are untrusted data: they travel only as JSON on the hook's stdin (never in a command line, never
//   through a shell), and the hook scrubs secrets from them before anything is stored.
//
// Who takes no letter.
// - A chat whose title begins with "[no-letter]" (`opencode run --title "[no-letter] pr-sweep" ...`), or whose session
//   metadata says {"platform_letter": "off"} (an API client sets it when it creates the session): the signals that work
//   in the shared service, where the caller's environment cannot be seen.  A chat started by a chat that takes none
//   (a sub-agent of a sweep worker) takes none.  The title is looked at when the chat is registered, a moment after it
//   is created, so a title given with the chat counts.
// - In a private server, the environment is the caller's: SESSION_LETTER=0 or OPENCODE_LETTER=0 turns the plugin off,
//   and a session a launcher started (AGENT_LAUNCHER or AGENT_LAUNCH_SEAT) takes no letter.  In the shared service
//   those variables are ignored, and taken out of the environment the hook is run with (the first client to start the
//   service would otherwise decide them for every later chat).
//
// Options (the entry in opencode.json is a path, or `{ "package": path, "options": { ... } }`):
//   location  "mac" or "server": where this chat runs (the hook knows the Mac by itself, a server must say so)
//   remote    the ssh destination that holds the Operator's registry (a server only)
//   command   the platform-letter command to run (default ~/.local/bin/platform-letter, or $PLATFORM_LETTER_BIN)
//   log       a file to append a short line per hook call to (ids and outcomes only, never a prompt)
//   shared    true or false: say whether this process serves several clients (default: read from how it was started)
//   settle    milliseconds a new chat waits for its first prompt before it is registered (default 1500)

import { spawn } from "node:child_process";
import { appendFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const PLUGIN_ID = "platform-letter-opencode";
export const PLATFORM = "opencode";
export const NO_LETTER_MARK = "[no-letter]"; // the same string as NO_LETTER_MARK in platform_letter_opencode.py
const HOOK_TIMEOUT_MS = 25000; // the ssh hop takes up to 8 seconds, and the hook's own budget elsewhere is 15 to 20
const TOUCH_MS = 25 * 60 * 1000; // the hook marks a chat seen at most every 30 minutes; ask a little sooner
const RETRY_HOOK_MS = 10 * 60 * 1000; // a hook that did not answer, or that left the chat without a letter
const RETRY_TITLE_MS = 60 * 1000; // a title OpenCode refused
const RETRY_READ_MS = 60 * 1000; // a chat OpenCode could not be asked about
const SETTLE_MS = 1500; // a new chat waits this long for its first prompt, or for the title it is given right away
const DISPOSE_MS = 4000; // the most a shutting-down OpenCode is made to wait for hooks still running
const PROMPT_CHARS = 4096; // the hook looks at no more than this much of a prompt
const SEEN_EVENTS = 64; // durable events remembered for each chat, to drop the copies the other plugin copies are told
const MAX_HOOKS = 3;
const MAX_OUTPUT = 64 * 1024;
const MAX_LINEAGE = 6; // parents above a chat that are registered first
const DEST_RE = /^[A-Za-z0-9][A-Za-z0-9._@-]*$/;
const LOCATIONS = ["mac", "server"];
const CALLER_ENV = ["AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "SESSION_LETTER", "OPENCODE_LETTER"];
const SLOT = Symbol.for("platform-letter-opencode.engine"); // on globalThis: the one engine of this process

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

// Whether this process serves several clients: `opencode serve` (the background service, or a server someone started for
// their clients) and `opencode web`, but not `serve --stdio`, the private server a client starts as its own child.
export function sharedServer(argv = process.argv) {
  const args = argv.slice(2).map(String);
  return (args.includes("serve") && !args.includes("--stdio")) || args[0] === "web";
}

// A chat that takes no letter by what it says about itself: the title it was given or its session metadata.
export function optedOut(title, metadata) {
  if (text(title).toLowerCase().startsWith(NO_LETTER_MARK)) return true;
  const meta = metadata && typeof metadata === "object" ? metadata : {};
  return text(meta.platform_letter).toLowerCase() === "off";
}

export function readSettings(options, env = process.env, shared = sharedServer()) {
  // null turns the plugin off.
  const opts = options && typeof options === "object" ? options : {};
  if (typeof opts.shared === "boolean") shared = opts.shared;
  if (!shared) {
    // A private server is the client's own child: its environment is the caller's, so the caller's switches apply.
    for (const name of ["SESSION_LETTER", "OPENCODE_LETTER"]) {
      if (text(env[name]) === "0") return null;
    }
    if (text(env.AGENT_LAUNCHER) || text(env.AGENT_LAUNCH_SEAT)) return null;
  }
  const command =
    text(opts.command) || text(env.PLATFORM_LETTER_BIN) || join(homedir(), ".local", "bin", "platform-letter");
  const args = ["hook", "--platform", PLATFORM];
  const location = text(opts.location).toLowerCase();
  if (LOCATIONS.includes(location)) args.push("--location", location);
  const remote = text(opts.remote);
  if (DEST_RE.test(remote)) args.push("--remote", remote);
  const settle = Number(opts.settle);
  const settleMs = Number.isFinite(settle) && settle >= 0 && settle <= 10000 ? Math.floor(settle) : SETTLE_MS;
  // In the shared service the environment is whichever client's started it: what the hook would read of it as the caller's
  // (a launcher, an off switch) is not the caller's, so the hook is run without it.
  const hookEnv = { ...env };
  if (shared) for (const name of CALLER_ENV) delete hookEnv[name];
  return { command, args, log: text(opts.log), timeoutMs: HOOK_TIMEOUT_MS, env: hookEnv, shared, settleMs };
}

function parseAnswer(output) {
  // The hook prints one JSON line, or nothing.  {} is "ran, nothing to apply"; the caller keeps null for "did not run".
  const lines = output.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (!line.startsWith("{")) continue;
    try {
      const body = JSON.parse(line).platform_letter;
      return body && typeof body === "object" ? body : {};
    } catch {
      continue;
    }
  }
  return {};
}

// Run the platform-letter hook once.  Resolves with its answer, {} for no answer, or null when it did not run or did
// not finish in time.  Never rejects.
export function runHook(settings, payload) {
  return new Promise((resolve) => {
    let done = false;
    let output = "";
    let child;
    const timer = setTimeout(() => {
      try {
        if (child?.pid) process.kill(-child.pid, "SIGKILL"); // the hook's own group: it and the ssh it started
      } catch {
        try {
          child?.kill("SIGKILL");
        } catch {
          // already gone
        }
      }
      try {
        child?.stdout?.destroy();
      } catch {
        // already gone
      }
      finish(null);
    }, settings.timeoutMs);
    function finish(value) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(value);
    }
    try {
      // Detached, so that a hook still running when a short-lived OpenCode (`opencode run --standalone`) exits is not taken
      // down with it: it finishes the registration and its note, and the chat gets its title when it is next used.
      child = spawn(settings.command, settings.args, {
        stdio: ["pipe", "pipe", "ignore"],
        env: settings.env || process.env,
        detached: true,
      });
      child.unref();
    } catch {
      finish(null);
      return;
    }
    child.on("error", () => finish(null));
    child.stdout.on("data", (chunk) => {
      if (output.length < MAX_OUTPUT) output += chunk;
    });
    child.on("close", () => finish(parseAnswer(output)));
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(payload));
  });
}

function sessionOf(event) {
  const id = event?.data?.sessionID;
  return typeof id === "string" && id ? id : "";
}

// The engine, with the OpenCode side (`api`) and the hook (`run`) handed in so a test can fake both.
//   api.get(sid)            the session, or throws
//   api.update(sid, title)  writes the title, or throws
export function createLetters({ api, run, now = Date.now, log = () => {}, settleMs = SETTLE_MS }) {
  const sessions = new Map();
  const gone = new Set(); // chats deleted while this plugin was running
  let running = 0;
  const waiting = [];

  const acquire = () =>
    new Promise((resolve) => {
      if (running < MAX_HOOKS) {
        running++;
        resolve();
      } else {
        waiting.push(resolve);
      }
    });
  const release = () => {
    const next = waiting.shift();
    if (next) next();
    else running--;
  };
  async function hook(payload) {
    await acquire();
    try {
      const answer = await run(payload);
      log("hook", { event: payload.event, session: payload.session_id, ran: answer !== null, label: answer?.label });
      return answer;
    } catch {
      return null;
    } finally {
      release();
    }
  }

  function stateOf(sid) {
    let st = sessions.get(sid);
    if (!st) {
      st = {
        chain: Promise.resolve(),
        started: false, // the engine knows this chat (the hook has been told, or the first prompt will tell it)
        off: false, // the chat takes no letter (a launcher's, a sweep's: see optedOut)
        hold: 0, // no start of this chat is tried before this time (Infinity: not until it is unarchived or prompted)
        created: false, // the chat's creation was seen: it is new, its first prompt registers it
        sawPrompt: false, // a prompt of it has come in
        wake: null, // ends the wait a new chat makes for its first prompt
        seen: new Set(), // the durable events handled, as "type:seq"
        parent: "", // the chat that started it, if any
        forked: false, // it was copied from another chat
        prompted: false, // the hook has seen a prompt of it that named it, or that it need not be asked about again
        label: "",
        title: "", // the title OpenCode shows now, as far as events have told
        want: "", // a title OpenCode refused
        retryAt: 0,
        nextAt: 0, // the next prompt that is sent to the hook, apart from the first
      };
      sessions.set(sid, st);
    }
    return st;
  }

  // One queue per chat: its events are handled in the order they came, and a chat's slow hook holds up nothing else.
  function enqueue(sid, task) {
    const st = stateOf(sid);
    st.chain = st.chain.then(task).catch((err) => log("task.error", { session: sid, error: String(err).slice(0, 120) }));
    return st.chain;
  }

  // The chat as OpenCode has it now.
  async function read(sid) {
    const got = await api.get(sid);
    return got && got.data && typeof got.data === "object" ? got.data : got;
  }

  // The chat has a name of its own under its letter (a placeholder, or no title, is not one).
  function titled(st) {
    if (!st.label) return false;
    const shown = st.want || st.title;
    const lead = st.label + ": ";
    if (!shown.startsWith(lead)) return false;
    const words = shown.slice(lead.length).trim();
    return words !== "" && words !== "New Chat";
  }

  async function setTitle(sid, title, depth = 0) {
    const st = stateOf(sid);
    if (!title || title === st.title) {
      st.want = "";
      return;
    }
    st.want = title;
    // A name given while the hook was working (OpenCode names a chat from its first prompt about the time the hook
    // answers) stands: the title is not written over it, the name is taken in and the letter goes in front of it.
    let current = "";
    try {
      current = text((await read(sid))?.title);
    } catch {
      current = "";
    }
    if (current && current !== st.title && current !== title && depth < 2) {
      st.want = "";
      await renamed(sid, current, depth + 1);
      return;
    }
    try {
      await api.update(sid, title);
      st.title = title;
      st.want = "";
      st.retryAt = 0;
      log("title", { session: sid, title });
    } catch (err) {
      st.retryAt = now() + RETRY_TITLE_MS;
      log("title.error", { session: sid, error: String(err).slice(0, 120) });
    }
  }

  async function apply(sid, answer, depth = 0) {
    const st = stateOf(sid);
    if (!answer) return;
    if (answer.label) st.label = answer.label;
    if (answer.title) await setTitle(sid, answer.title, depth);
  }

  async function retryTitle(sid) {
    const st = sessions.get(sid);
    if (st?.want && now() >= st.retryAt) await setTitle(sid, st.want);
  }

  // Wait, for a new chat, for its first prompt (or for the title a client gives it right after creating it), so that the
  // chat is judged by what it is and not by the instant it was made.
  async function settled(sid) {
    const st = stateOf(sid);
    if (!settleMs || st.sawPrompt) return;
    let timer;
    await new Promise((resolve) => {
      st.wake = resolve;
      timer = setTimeout(resolve, settleMs);
    });
    clearTimeout(timer);
    st.wake = null;
  }

  // The Operator gives a chat another chat started the pair after its parent's, so the parent goes first: started when this
  // plugin has not seen it, waited for when its own hook is still running, and asked once more when that hook did not
  // answer (a Mac that was asleep), no sooner than the retry interval.
  async function parentFirst(parentId, lineage) {
    const pst = sessions.get(parentId);
    if (!pst || (!pst.started && !pst.off)) await enqueue(parentId, () => start(parentId, { source: "resume", lineage }));
    else await pst.chain;
    const p = sessions.get(parentId);
    if (p?.started && !p.label && !p.off && now() >= p.nextAt) await enqueue(parentId, () => restart(parentId));
  }

  // Make a chat known to the engine: read it (its title, parent, fork, metadata, whether it is archived), and start its
  // parent first.  Answers "no" for a chat that cannot be registered (archived, unreadable, one that takes no letter), "was"
  // for one already known and "now" for one that became known by this call.  `force` is a prompt or an unarchive: it
  // tries a chat whose last try was held off.
  async function ready(sid, { parent, title, lineage = [], force = false } = {}) {
    const st = stateOf(sid);
    if (st.started) return "was";
    if (st.off) return "no";
    if (!force && now() < st.hold) return "no";
    let info = null;
    try {
      info = await read(sid);
    } catch {
      if (parent === undefined && title === undefined) {
        st.hold = now() + RETRY_READ_MS; // not read again for every event of a chat OpenCode cannot answer for
        return "no";
      }
    }
    if (info?.time?.archived) {
      st.hold = Infinity; // an archived chat holds no letter, and its events start nothing
      return "no";
    }
    st.hold = 0;
    const name = info ? text(info.title) : text(title);
    if (optedOut(name, info?.metadata)) return silence(sid, "marked");
    const parentId = text(parent ?? info?.parentID);
    const seen = [...lineage, sid];
    if (parentId && !seen.includes(parentId) && seen.length < MAX_LINEAGE) {
      await parentFirst(parentId, seen);
      if (sessions.get(parentId)?.off) return silence(sid, "parent"); // a sweep worker's sub-agents are not chats of anyone's
    }
    if (st.started) return "was";
    st.started = true;
    st.parent = parentId;
    st.title = name;
    st.forked = Boolean(text(info?.fork?.sessionID));
    return "now";
  }

  function silence(sid, why) {
    stateOf(sid).off = true;
    log("off", { session: sid, why });
    return "no";
  }

  // Tell the hook this chat exists.  It is titled `XX: New Chat` until its first prompt names it, or `XX: <name>` when
  // it has a name already.  A fork carries the title of the chat it was copied from: it is started as one.
  async function start(sid, { source = "resume", force = false, ...known } = {}) {
    if ((await ready(sid, { force, ...known })) !== "now") return;
    const st = stateOf(sid);
    const answer = await hook({
      event: "start",
      session_id: sid,
      parent_session_id: st.parent,
      source: st.forked ? "fork" : source,
      title: st.title,
    });
    await apply(sid, answer);
    st.nextAt = now() + (answer === null || !st.label ? RETRY_HOOK_MS : TOUCH_MS);
  }

  // A parent whose start was not answered: the same event once more.
  async function restart(sid) {
    const st = sessions.get(sid);
    if (!st?.started || st.label || st.off) return;
    const answer = await hook({
      event: "start",
      session_id: sid,
      parent_session_id: st.parent,
      source: st.forked ? "fork" : "resume",
      title: st.title,
    });
    await apply(sid, answer);
    st.nextAt = now() + (answer === null || !st.label ? RETRY_HOOK_MS : TOUCH_MS);
  }

  // A prompt.  The very first prompt of a brand-new chat (`fresh`: the first event OpenCode wrote for it, or a chat whose
  // creation was seen) needs no start: the hook registers it on the prompt and names it from the prompt at once, one
  // registration and one title in place of a placeholder and then a title.  Any other chat first seen through a prompt is
  // a chat with a past (it may have a registration already, from an earlier OpenCode), so it is started first and shown
  // its title again.
  async function prompt(sid, words, fresh = false) {
    const st = stateOf(sid);
    if (!fresh && !st.started) await start(sid, { source: "resume", force: true });
    if ((await ready(sid, { force: true })) === "no") return;
    if (st.prompted && now() < st.nextAt) {
      await retryTitle(sid); // a steady-state prompt costs no hook
      return;
    }
    try {
      st.title = text((await read(sid))?.title); // the name it has now, which may be OpenCode's own from this prompt
    } catch {
      // keep what the events told
    }
    const answer = await hook({
      event: "prompt",
      session_id: sid,
      parent_session_id: st.parent,
      prompt: String(words || "").slice(0, PROMPT_CHARS),
      title: st.title,
    });
    await apply(sid, answer);
    if (answer === null || !st.label) {
      st.prompted = true; // no letter yet (the Mac asleep): ask again in ten minutes, not with every prompt
      st.nextAt = now() + RETRY_HOOK_MS;
    } else if (!titled(st)) {
      st.prompted = false; // a letter and no name (a prompt of only a link, or an image): the next prompt may give one
    } else {
      st.prompted = true;
      st.nextAt = now() + TOUCH_MS;
    }
  }

  // A chat was renamed.  The event is a cue to look: the title it carries can be old by the time this chat's queue
  // gets to it (the plugin's own title comes back through here too), so the title the chat has now is the one judged.
  async function renamed(sid, hint, depth = 0) {
    const st = stateOf(sid);
    if (!st.started) {
      await start(sid, { source: "resume" });
      if (!st.started) return;
    }
    let title = hint;
    try {
      title = text((await read(sid))?.title);
    } catch {
      title = hint;
    }
    log("renamed", { session: sid, title, ours: title === st.title });
    if (title === st.title) return; // the plugin's own title coming back, or no change
    st.title = title;
    if (!title) return;
    await apply(sid, await hook({ event: "rename", session_id: sid, title }), depth);
  }

  async function ended(sid, reason) {
    const st = sessions.get(sid);
    if (!st?.off) await hook({ event: "end", session_id: sid, reason });
    if (reason === "deleted") {
      sessions.delete(sid);
      if (gone.size >= 1000) gone.clear();
      gone.add(sid); // events still on their way for it start nothing
      return;
    }
    if (!st) return;
    // Archived: the registration is closed.  The chat starts again if it comes back.
    st.started = false;
    st.prompted = false;
    st.label = "";
    st.want = "";
    st.hold = Infinity; // its late events (a stream still running) start nothing
  }

  // The same durable event reaches the engine once for each copy of the plugin OpenCode has loaded.
  function duplicate(sid, event) {
    const seq = event.durable?.seq;
    if (typeof seq !== "number") return false;
    const st = stateOf(sid);
    const key = event.type + ":" + seq;
    if (st.seen.has(key)) return true;
    st.seen.add(key);
    if (st.seen.size > SEEN_EVENTS) st.seen.delete(st.seen.values().next().value);
    return false;
  }

  function onEvent(event) {
    try {
      const sid = sessionOf(event);
      if (!sid || typeof event.type !== "string" || !event.type.startsWith("session.")) return;
      if (gone.has(sid)) return;
      if (duplicate(sid, event)) return;
      const data = event.data;
      switch (event.type) {
        case "session.created":
          stateOf(sid).created = true;
          enqueue(sid, async () => {
            await settled(sid);
            // A prompt that came while the chat waited registers and names it in one step; its task is next in the queue.
            if (stateOf(sid).sawPrompt && !stateOf(sid).started) return;
            await start(sid, { source: "startup", parent: text(data.parentID), title: text(data.title) });
          });
          return;
        case "session.forked":
          // A fork is a main chat of its own: `parentID` in this event is the chat it was copied from.
          enqueue(sid, () => start(sid, { source: "fork", parent: "" }));
          return;
        case "session.inbox.enqueued": {
          const item = data.item;
          const words = item?.payload?.text;
          if (item?.type === "user" && typeof words === "string") {
            const st = stateOf(sid);
            const fresh = event.durable?.seq === 1 || st.created;
            st.sawPrompt = true;
            if (st.wake) st.wake();
            enqueue(sid, () => prompt(sid, words, fresh));
          } else if (!sessions.get(sid)?.started) enqueue(sid, () => start(sid));
          return;
        }
        case "session.renamed":
          enqueue(sid, () => renamed(sid, text(data.title)));
          return;
        case "session.archived":
          enqueue(sid, () => ended(sid, "archived"));
          return;
        case "session.deleted":
          enqueue(sid, () => ended(sid, "deleted"));
          return;
        case "session.unarchived":
          enqueue(sid, () => {
            const st = stateOf(sid);
            st.started = false;
            st.off = false;
            st.hold = 0;
            return start(sid, { source: "resume", force: true });
          });
          return;
        default: {
          const st = sessions.get(sid);
          if (!st?.started) {
            // A chat first seen through some other event; not one that is archived, unreadable just now, or silent.
            if (!st || (!st.off && now() >= st.hold)) enqueue(sid, () => start(sid));
          } else if (st.want) enqueue(sid, () => retryTitle(sid));
        }
      }
    } catch (err) {
      log("event.error", { error: String(err).slice(0, 120) });
    }
  }

  // Wait, for at most `ms`, for the hooks that are still running.
  async function flush(ms) {
    const all = Promise.all([...sessions.values()].map((st) => st.chain));
    let timer;
    await Promise.race([all, new Promise((resolve) => (timer = setTimeout(resolve, ms)))]);
    clearTimeout(timer);
  }

  return { onEvent, flush, sessions };
}

function logger(settings) {
  if (!settings.log) return () => {};
  return (kind, fields) => {
    try {
      appendFileSync(settings.log, JSON.stringify({ t: Date.now(), kind, ...fields }) + "\n");
    } catch {
      // a log that cannot be written is not worth a chat
    }
  };
}

// Try each copy's OpenCode handle until one answers: a copy whose directory has been shut down answers with an error.
async function anyHandle(handles, call) {
  let last;
  for (const handle of [...handles]) {
    try {
      return await call(handle);
    } catch (err) {
      last = err;
    }
  }
  throw last ?? new Error("no OpenCode handle");
}

// The one engine of this process.  OpenCode loads this plugin once for each directory it uses and tells every copy the
// events of every chat, so each copy joins the engine the first one made and feeds it the events, which it handles once.
function joinEngine(settings, handle) {
  let shared = globalThis[SLOT];
  if (!shared) {
    const handles = [];
    const log = logger(settings);
    const api = {
      get: (sid) => anyHandle(handles, (h) => h.get(sid)),
      update: (sid, title) => anyHandle(handles, (h) => h.update(sid, title)),
    };
    const letters = createLetters({ api, run: (payload) => runHook(settings, payload), log, settleMs: settings.settleMs });
    shared = { handles, users: 0, letters, log };
    globalThis[SLOT] = shared;
  }
  shared.handles.push(handle);
  shared.users++;
  return shared;
}

async function leave(shared, handle) {
  const at = shared.handles.indexOf(handle);
  if (at >= 0) shared.handles.splice(at, 1);
  shared.users--;
  if (shared.users > 0) return; // the other copies still feed the engine
  if (globalThis[SLOT] === shared) delete globalThis[SLOT];
  shared.log("dispose", { sessions: shared.letters.sessions.size });
  await shared.letters.flush(DISPOSE_MS);
  shared.log("disposed", {});
}

async function listen(ctx, letters, signal, log) {
  for (let attempt = 0; attempt < 5 && !signal.aborted; attempt++) {
    try {
      for await (const event of ctx.event.subscribe({ signal })) letters.onEvent(event);
      return; // the stream ended with the directory
    } catch (err) {
      if (signal.aborted) return;
      log("subscribe.error", { error: String(err).slice(0, 120) });
      await new Promise((resolve) => setTimeout(resolve, 2000 * (attempt + 1)));
    }
  }
}

export default {
  id: PLUGIN_ID,
  setup(ctx) {
    const settings = readSettings(ctx?.options);
    if (!settings || !ctx?.event?.subscribe || !ctx?.session?.update) return undefined;
    const handle = {
      get: (sid) => ctx.session.get({ sessionID: sid }),
      update: (sid, title) => ctx.session.update({ sessionID: sid, title }),
    };
    const shared = joinEngine(settings, handle);
    const controller = new AbortController();
    shared.log("setup", { command: settings.command, args: settings.args, shared: settings.shared, users: shared.users });
    void listen(ctx, shared.letters, controller.signal, shared.log).catch(() => {});
    return async () => {
      controller.abort();
      await leave(shared, handle);
    };
  },
};
