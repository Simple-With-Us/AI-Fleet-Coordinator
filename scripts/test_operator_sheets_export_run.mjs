// Tests for simple_tools/operator_sheets_export_run.mjs (AFC-412 PR2 runner).
// Run: node --test scripts/test_operator_sheets_export_run.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  parseArgs, cliOutbox, dispatchOutbox, loadAdapter, execUnderFlock, runLockPath,
  CANONICAL_DB, LOCK_NAME, holdingFlock, runOnce, main,
} from "./simple_tools/operator_sheets_export_run.mjs";
import { spawn, execFileSync } from "node:child_process";
import { EventEmitter } from "node:events";
import { pathToFileURL } from "node:url";
function pathToFileUrl() {
  return new URL("./simple_tools/operator_sheets_export_run.mjs", import.meta.url).href;
}

const DB = "/data/operator/registry.sqlite3";

test("parseArgs: flags, env fallback, and refusals", () => {
  const a = parseArgs(["--db", DB, "--loop", "--limit", "25", "--interval-ms", "5000",
    "--cwd", "/app/scripts"]);
  assert.deepEqual([a.db, a.loop, a.once, a.limit, a.intervalMs], [DB, true, false, 25, 5000]);
  assert.equal(parseArgs(["--dispatcher", "/d"]).once, true, "no --loop means one drain");
  assert.throws(() => parseArgs(["--dispatcher", "/d", "--once", "--loop"]), /exclude/);
  assert.throws(() => parseArgs(["--db", DB, "--limit", "0", "--cwd", "/app/scripts"]),
    /--limit/);
  assert.throws(() => parseArgs(["--db", DB, "--interval-ms", "100", "--cwd", "/app/scripts"]),
    /interval/);
  assert.throws(() => parseArgs(["--db", DB, "--module", "os", "--cwd", "/app/scripts"]),
    /module/);
  assert.throws(() => parseArgs(["--db"]), /needs a value/);
  assert.throws(() => parseArgs(["--db", DB, "--bogus", "--cwd", "/app/scripts"]),
    /unknown flag/);
});

test("parseArgs: a bridge is required and the two bridges exclude each other", () => {
  assert.throws(() => parseArgs(["--db", DB]), /no bridge/);
  assert.throws(() => parseArgs(["--db", DB, "--dispatcher", "/d", "--cwd", "/app/scripts"]),
    /exclude/);
  assert.equal(parseArgs(["--db", DB, "--cwd", "/app/scripts"]).cwd, "/app/scripts");
});

test("parseArgs: the local bridge drains only the canonical store", () => {
  // An existing-but-wrong database must fail closed, not drain.
  assert.throws(() => parseArgs(["--db", "/tmp/second-registry.sqlite3", "--cwd", "/app"]),
    /canonical store/);
  assert.equal(parseArgs(["--db", CANONICAL_DB, "--cwd", "/app"]).db, CANONICAL_DB);
});

test("parseArgs: --db does not apply with --dispatcher (the dispatch pins the store)", () => {
  assert.throws(() => parseArgs(["--db", DB, "--dispatcher", "/d"]),
    /--db does not apply/);
  process.env.SIMPLE_TOOLS_OPERATOR_REGISTRY = DB;
  try {
    assert.throws(() => parseArgs(["--dispatcher", "/d"]), /--db does not apply/);
  } finally {
    delete process.env.SIMPLE_TOOLS_OPERATOR_REGISTRY;
  }
});

test("cliOutbox: the bridge argv, the pinned cwd and the scrubbed environment", async () => {
  const calls = [];
  const execFileImpl = async (bin, args, opts) => {
    calls.push({ cmd: [bin, ...args], opts });
    return { stdout: args[2] === "pending-outbox" ? '[{"outbox_id":"o1"}]' : '{"marked":1}' };
  };
  const outbox = cliOutbox({ python: "python3", module: "simple_tools.operator_registry",
    db: DB, cwd: "/app/scripts", execFileImpl });
  assert.deepEqual(await outbox.pending(50), [{ outbox_id: "o1" }]);
  await outbox.markExported(["o1", "o2"]);
  assert.deepEqual(calls[0].cmd, ["python3", "-m", "simple_tools.operator_registry",
    "pending-outbox", "--db", DB, "--limit", "50"]);
  assert.deepEqual(calls[1].cmd, ["python3", "-m", "simple_tools.operator_registry",
    "mark-exported", "--db", DB, "o1", "o2"]);
  for (const c of calls) {
    assert.equal(c.opts.cwd, "/app/scripts", "the child never inherits the caller's cwd");
    assert.deepEqual(Object.keys(c.opts.env).sort(), ["PATH", "PYTHONNOUSERSITE"],
      "no PYTHONPATH or ambient SIMPLE_TOOLS_* steering reaches the bridge");
  }
});

test("cliOutbox refuses to run without an explicit cwd", () => {
  assert.throws(() => cliOutbox({ python: "python3", module: "m.m", db: DB }), /cwd/);
});

test("dispatchOutbox: the dispatch pins the store - no --db crosses this argv", async () => {
  const calls = [];
  const execFileImpl = async (bin, args) => {
    calls.push([bin, ...args]);
    return { stdout: args[1] === "pending-outbox" ? '[{"outbox_id":"o1"}]' : '{"marked":1}' };
  };
  const outbox = dispatchOutbox({ dispatcher: "/usr/local/bin/simple-chat", execFileImpl });
  assert.deepEqual(await outbox.pending(25), [{ outbox_id: "o1" }]);
  await outbox.markExported(["o1"]);
  assert.deepEqual(calls[0], ["/usr/local/bin/simple-chat", "operator-registry",
    "pending-outbox", "--limit", "25"]);
  assert.deepEqual(calls[1], ["/usr/local/bin/simple-chat", "operator-registry",
    "mark-exported", "o1"]);
  for (const c of calls) assert.ok(!c.includes("--db"), "the caller never names a store");
});

test("loadAdapter fails closed without a spec", async () => {
  await assert.rejects(() => loadAdapter(""), /OPERATOR_SHEETS_ADAPTER/);
});

test("loadAdapter requires the AFC-428 surface", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "adapter-"));
  const bad = path.join(dir, "bad.mjs");
  writeFileSync(bad, 'export default { validateHeaders() {} };\n');
  await assert.rejects(() => loadAdapter(bad), /lacks readRows/);
  const good = path.join(dir, "good.mjs");
  writeFileSync(good, `let created = 0;
export default { createAdapter() { created += 1; return {
  validateHeaders() {}, readRows() { return { rows: [] }; },
  appendSession() {}, updateSessionRow() {}, appendEvent() {}, created }; } };
`);
  const adapter = await loadAdapter(good);
  assert.equal(adapter.created, 1, "createAdapter() is honored");
});

test("runOnce delegates to drainOutbox and marks only confirmed writes", async () => {
  const marked = [];
  const outbox = {
    async pending() {
      return [{ outbox_id: "o1", kind: "event_append",
        payload: JSON.stringify({ cells: ["e1", "t", "r1", "l", "registered", "op", "ok", "src", ""] }) }];
    },
    async markExported(ids) { marked.push(...ids); },
  };
  const adapter = {
    async validateHeaders() {},
    async readRows() { return { rows: [] }; },
    async appendSession() { throw new Error("never for an event"); },
    async updateSessionRow() {},
    async appendEvent() {},
  };
  const { exported, failed } = await runOnce({ outbox, adapter, limit: 50, log() {} });
  assert.deepEqual([exported, failed], [1, 0]);
  assert.deepEqual(marked, ["o1"], "marked only after the write confirmed");
});

test("main: exit 2 with no adapter spec, 0 on a clean drain, 1 on a failed drain", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "adapter-"));
  const good = path.join(dir, "good.mjs");
  writeFileSync(good, `export default {
  validateHeaders() {}, readRows() { return { rows: [] }; },
  appendSession() {}, updateSessionRow() {}, appendEvent() {} };
`);
  assert.equal(await main(["--dispatcher", "/d"], { adapterSpec: "" }), 2);
  const empty = { async pending() { return []; }, async markExported() {} };
  assert.equal(await main(["--dispatcher", "/d"],
    { adapterSpec: good, outbox: empty, skipLock: true }), 0);
  const failing = {
    async pending() {
      return [{ outbox_id: "o1", kind: "event_append", payload: JSON.stringify({ cells: ["e1"] }) }];
    },
    async markExported() {},
  };
  const dir2 = mkdtempSync(path.join(tmpdir(), "adapter-"));
  const badAppend = path.join(dir2, "bad-append.mjs");
  writeFileSync(badAppend, `export default {
  validateHeaders() {}, readRows() { return { rows: [] }; },
  appendSession() {}, updateSessionRow() {},
  appendEvent() { throw new Error("sheet gone"); } };
`);
  assert.equal(await main(["--dispatcher", "/d"],
    { adapterSpec: badAppend, outbox: failing, skipLock: true }), 1);
});

test("main: no bridge flag is a configuration error", async () => {
  assert.equal(await main(["--db", DB], { adapterSpec: "" }), 2);
});


// A fake child for the flock re-exec:  exits with the given code.
function fakeChild(code) {
  const child = new EventEmitter();
  setImmediate(() => child.emit("exit", code));
  return child;
}

// A real flock-held lock, held by a real background process (integration, not mock).
function realHolder(lockPath) {
  const holder = spawn("flock", ["--no-fork", "--nonblock", lockPath, "sleep", "30"],
    { stdio: "ignore" });
  return holder;
}
const HAS_FLOCK = (() => {
  try { execFileSync("flock", ["--version"], { stdio: "ignore" }); return true; }
  catch { return false; }
})();
const flockTest = HAS_FLOCK ? test : test.skip;

test("the drain re-execs under flock --no-fork so the drain process IS the holder", async () => {
  const spawned = [];
  const spawnImpl = (bin, args, opts) => {
    spawned.push({ bin, args, stdio: opts.stdio });
    return fakeChild(0);
  };
  const code = await execUnderFlock(["--dispatcher", "/d"], "/locks/abc.lock",
    { spawnImpl });
  assert.equal(code, 0);
  const { bin, args, stdio } = spawned[0];
  assert.equal(bin, "flock");
  // --no-fork:  no supervisor - flock execs the drain, so the lock lives exactly as long
  // as the drain process and every exit path releases it.
  assert.deepEqual(args.slice(0, 5),
    ["--no-fork", "--nonblock", "--conflict-exit-code", "73", "/locks/abc.lock"]);
  assert.equal(args[5], process.execPath, "the command is this same script re-executed");
  assert.ok(args[6].endsWith("operator_sheets_export_run.mjs"));
  assert.deepEqual(args.slice(7), ["--dispatcher", "/d"]);
  assert.deepEqual(stdio, "inherit", "the drain's own stdout/stderr carry the result");
});

test("a busy lock maps flock's conflict code to exit 3, never a silent drain", async () => {
  assert.equal(await execUnderFlock([], "/l", { spawnImpl: () => fakeChild(73) }), 3);
});

test("every other exit code passes through unchanged", async () => {
  for (const c of [0, 1, 2]) {
    assert.equal(await execUnderFlock([], "/l", { spawnImpl: () => fakeChild(c) }), c);
  }
});

test("runLockPath is one fixed name per lock directory (store-global)", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "lockid-"));
  const a = runLockPath({ OPERATOR_EXPORT_LOCK_DIR: dir });
  assert.equal(path.basename(a), LOCK_NAME,
    "no dispatcher-path or --db spelling can split the canonical store's lock");
});

flockTest("holdingFlock proves custody from this process's own descriptors", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "custody-"));
  const lockPath = path.join(dir, LOCK_NAME);
  assert.equal(holdingFlock(lockPath), false, "nothing held yet");
  // A REAL child holds the lock, not us:  our probe must say not-held.
  const holder = realHolder(lockPath);
  try {
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(holdingFlock(lockPath), false,
      "another process's lock is not our custody");
  } finally {
    holder.kill("SIGKILL");
  }
  // THIS process holds it (via flock --no-fork running a probe of holdingFlock itself).
  const probe = spawn("flock", ["--no-fork", "--nonblock", lockPath,
    process.execPath, "-e",
    `import("${pathToFileUrl() }").then(m => process.exit(m.holdingFlock(${JSON.stringify(lockPath)}) ? 0 : 7))`],
    { stdio: "ignore" });
  const code = await new Promise((r) => probe.once("exit", r));
  assert.equal(code, 0, "the re-exec'd drain proves its own custody");
});

flockTest("a SIGKILLed holder releases the lock (crash path, real processes)", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "crash-"));
  const lockPath = path.join(dir, LOCK_NAME);
  const holder = realHolder(lockPath);
  await new Promise((r) => setTimeout(r, 300));
  // Busy while held:  flock's conflict code is 73.
  const busy = spawn("flock", ["--no-fork", "--nonblock", "--conflict-exit-code", "73",
    lockPath, "true"], { stdio: "ignore" });
  assert.equal(await new Promise((r) => busy.once("exit", r)), 73);
  holder.kill("SIGKILL"); // the hardest death there is
  await new Promise((r) => holder.once("exit", r));
  const free = spawn("flock", ["--no-fork", "--nonblock", "--conflict-exit-code", "73",
    lockPath, "true"], { stdio: "ignore" });
  assert.equal(await new Promise((r) => free.once("exit", r)), 0,
    "the lock died with the holder");
});

flockTest("main against a really-held lock exits 3 without draining (real flock)", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "mainbusy-"));
  const lockPath = runLockPath({ OPERATOR_EXPORT_LOCK_DIR: dir });
  const holder = realHolder(lockPath);
  await new Promise((r) => setTimeout(r, 300));
  try {
    const never = { async pending() { throw new Error("must not run"); },
      async markExported() {} };
    const dirA = mkdtempSync(path.join(tmpdir(), "adapter-"));
    const good = path.join(dirA, "good.mjs");
    writeFileSync(good, `export default {
  validateHeaders() {}, readRows() { return { rows: [] }; },
  appendSession() {}, updateSessionRow() {}, appendEvent() {} };
`);
    // No mocks:  the real flock binary gates the re-exec and answers busy.
    const code = await main(["--dispatcher", "/usr/bin/env"],
      { adapterSpec: good, outbox: never,
        env: { OPERATOR_EXPORT_LOCK_DIR: dir } });
    assert.equal(code, 3);
  } finally {
    holder.kill("SIGKILL");
  }
});

test("main: a spawn failure (no flock binary) fails closed, never an unlocked drain", async () => {
  const never = { async pending() { throw new Error("must not run"); },
    async markExported() {} };
  const dir = mkdtempSync(path.join(tmpdir(), "lockenv-"));
  const dirA = mkdtempSync(path.join(tmpdir(), "adapter-"));
  const good = path.join(dirA, "good.mjs");
  writeFileSync(good, `export default {
  validateHeaders() {}, readRows() { return { rows: [] }; },
  appendSession() {}, updateSessionRow() {}, appendEvent() {} };
`);
  const broken = { spawnImpl: () => {
    const child = new EventEmitter();
    setImmediate(() => child.emit("error", new Error("spawn flock ENOENT")));
    return child;
  } };
  assert.equal(await main(["--dispatcher", "/usr/bin/env"],
    { adapterSpec: good, outbox: never, env: { OPERATOR_EXPORT_LOCK_DIR: dir },
      lockDeps: broken }), 2);
});

test("main: proven in-process custody drains without a re-exec", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "lockenv-"));
  const empty = { async pending() { return []; }, async markExported() {} };
  const dirA = mkdtempSync(path.join(tmpdir(), "adapter-"));
  const good = path.join(dirA, "good.mjs");
  writeFileSync(good, `export default {
  validateHeaders() {}, readRows() { return { rows: [] }; },
  appendSession() {}, updateSessionRow() {}, appendEvent() {} };
`);
  // Custody proof supplied by the (test) holder check:  no re-exec may happen.
  const code = await main(["--dispatcher", "/usr/bin/env"],
    { adapterSpec: good, outbox: empty, env: { OPERATOR_EXPORT_LOCK_DIR: dir },
      holdingFlockImpl: () => true,
      lockDeps: { spawnImpl: () => { throw new Error("must not re-exec"); } } });
  assert.equal(code, 0, "proven custody drains in-process");
});
