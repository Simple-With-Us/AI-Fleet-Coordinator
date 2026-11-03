#!/usr/bin/env node
// operator_sheets_export_run: drain the Operator registry outbox to the fleet reference workbook.
//
// The runner wires drainOutbox (operator_sheets_export.mjs) to the registry's Python bridge
// (pending-outbox / mark-exported) and to the AFC-428 sheets adapter selected by
// OPERATOR_SHEETS_ADAPTER.  Outbox rows stay pending until the workbook write confirms, so a
// crash mid-run never loses a row.
//
// BRIDGE.  Two ways to reach the registry verbs, chosen by flags:
//   --dispatcher PATH  (the reviewed registry dispatch):  argv becomes
//                      [PATH, "operator-registry", VERB, ...] and the dispatcher pins the
//                      canonical store itself.  Use this on the registry host.
//   local module       python3 -m MODULE VERB --db DB ...:  only with --cwd DIR, which the
//                      runner chdirs the child into with a scrubbed environment (PATH and
//                      PYTHONNOUSERSITE only) - a local python must never pick up the caller's
//                      cwd, PYTHONPATH or ambient SIMPLE_TOOLS_* steering.  The local bridge
//                      accepts only the canonical store path itself.
//
// SINGLE RUNNER.  One exporter at a time for the canonical store:  the drain re-execs
// itself under flock(2) --no-fork and proceeds only when its OWN descriptor provably holds
// the lock (checked in /proc/self/fd, never an env marker), so the holder is the drain
// itself and every exit releases.  Two overlapping runners would read the same pending rows
// before either marks them exported and append both to the sheet, so a held lock refuses
// the second run (exit 3).  The host install must pin OPERATOR_EXPORT_LOCK_DIR to one
// host-wide directory.  The within-run ordering rule still stands:  the drain stops at the
// first failure so a later snapshot never overtakes an unexported earlier one.
//
// ADAPTER SELECTION FAILS CLOSED:  OPERATOR_SHEETS_ADAPTER names an ESM module whose default
// export is { createAdapter(): adapter } (or an adapter object).  Without it the runner
// refuses loudly.  The shipped adapter is operator_sheets_adapter.mjs.
//
// Flags:  --once (one drain, default), --loop (drain every --interval-ms, default 30000),
// --limit N (rows per drain, default 50), --dispatcher PATH (bridge through the registry
// dispatch;  --db does not apply - the dispatch pins the store), or the local bridge
// --db CANONICAL --cwd DIR [--python BIN --module M].  Exit 0 when a drain exported
// everything pending, 1 on a drain failure, 2 on bad configuration, 3 when another runner
// holds the lock.
import { execFile as execFileCb, spawn } from "node:child_process";
import { promisify } from "node:util";
import { mkdirSync, readdirSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { pathToFileURL, fileURLToPath } from "node:url";
import { drainOutbox } from "./operator_sheets_export.mjs";

const execFile = promisify(execFileCb);

// The one store the drain may serve.  The registry dispatch pins the same path in-container
// (operator_registry_dispatch.CANONICAL_DB);  the local bridge is refused anything else, so a
// drain can never be steered at an existing-but-wrong database by an argument.
export const CANONICAL_DB = "/data/operator/registry.sqlite3";

// The scrubbed child environment for the local bridge:  enough to start python, nothing
// that steers module resolution or registry selection.
const LOCAL_CHILD_ENV = Object.freeze({
  PATH: process.env.PATH || "/usr/local/bin:/usr/bin:/bin",
  PYTHONNOUSERSITE: "1",
});

export function parseArgs(argv) {
  const out = { db: "", once: false, loop: false, limit: 50, intervalMs: 30000,
    python: "python3", module: "simple_tools.operator_registry", dispatcher: "", cwd: "" };
  for (let i = 0; i < argv.length; i += 1) {
    const take = (name) => {
      const v = argv[i + 1];
      if (v === undefined || v.startsWith("--")) throw new Error(`${name} needs a value`);
      i += 1;
      return v;
    };
    const a = argv[i];
    if (a === "--db") out.db = take(a);
    else if (a === "--once") out.once = true;
    else if (a === "--loop") out.loop = true;
    else if (a === "--limit") out.limit = Number(take(a));
    else if (a === "--interval-ms") out.intervalMs = Number(take(a));
    else if (a === "--python") out.python = take(a);
    else if (a === "--module") out.module = take(a);
    else if (a === "--dispatcher") out.dispatcher = take(a);
    else if (a === "--cwd") out.cwd = take(a);
    else throw new Error(`unknown flag ${a}`);
  }
  if (out.once && out.loop) throw new Error("--once and --loop exclude each other");
  if (!out.loop) out.once = true;
  if (!Number.isInteger(out.limit) || out.limit < 1 || out.limit > 500)
    throw new Error("--limit must be 1..500");
  if (!Number.isFinite(out.intervalMs) || out.intervalMs < 1000)
    throw new Error("--interval-ms must be at least 1000");
  out.db = out.db || process.env.SIMPLE_TOOLS_OPERATOR_REGISTRY || "";
  if (!/^[a-z0-9_]+(\.[a-z0-9_]+)+$/.test(out.module)) throw new Error("bad --module");
  if (out.dispatcher && out.cwd)
    throw new Error("--dispatcher and --cwd exclude each other: pick one bridge");
  if (!out.dispatcher && !out.cwd)
    throw new Error("no bridge: pass --dispatcher PATH, or --cwd DIR for the local module bridge");
  if (out.dispatcher) {
    // The dispatch pins the canonical store itself;  a --db here routes nothing and is
    // refused so a caller cannot believe it steered the drain.
    if (out.db)
      throw new Error("--db does not apply with --dispatcher: the dispatch pins the store");
  } else {
    if (!out.db)
      throw new Error("no registry db: pass --db or set SIMPLE_TOOLS_OPERATOR_REGISTRY");
    if (out.db !== CANONICAL_DB)
      throw new Error(`the local bridge drains only the canonical store (${CANONICAL_DB});  `
        + "an existing wrong database must fail closed, not drain");
  }
  return out;
}

// The local module bridge.  cwd and the scrubbed environment are REQUIRED:  a bare
// `python3 -m` inherits the caller's cwd, PYTHONPATH and ambient steering, which is how a
// drain ends up reading a different store than the dispatcher would pin.
export function cliOutbox({ python, module, db, cwd, execFileImpl = execFile }) {
  if (!cwd) throw new Error("cliOutbox requires cwd: the local bridge never inherits one");
  const run = async (args) => {
    const { stdout } = await execFileImpl(python, ["-m", module, ...args],
      { timeout: 20000, cwd, env: { ...LOCAL_CHILD_ENV } });
    return JSON.parse(stdout);
  };
  return {
    async pending(limit) {
      // --db= form: a db value starting with "-" can never parse as a fresh option.
      return run(["pending-outbox", `--db=${db}`, "--limit", String(limit)]);
    },
    async markExported(ids) {
      // "--" ends option parsing: an id starting with "-" stays a positional.
      await run(["mark-exported", `--db=${db}`, "--", ...ids]);
    },
  };
}

// The dispatcher bridge:  the registry dispatch pins the canonical store, the fixed uid and
// the workdir;  no --db ever crosses this argv.
export function dispatchOutbox({ dispatcher, limit = 50, execFileImpl = execFile }) {
  const run = async (args) => {
    const { stdout } = await execFileImpl(dispatcher, ["operator-registry", ...args],
      { timeout: 25000 });
    return JSON.parse(stdout);
  };
  return {
    async pending(n) {
      return run(["pending-outbox", "--limit", String(n)]);
    },
    async markExported(ids) {
      await run(["mark-exported", "--", ...ids]);
    },
  };
}

// The shipped, reviewed AFC-428 adapter.  Production selection (the OPERATOR_SHEETS_ADAPTER
// environment variable) must resolve to exactly this module;  any other module is refused.
// Test injection goes through deps.adapterSpec, which never crosses this pin.
const SHIPPED_ADAPTER_PATH = fileURLToPath(new URL("./operator_sheets_adapter.mjs", import.meta.url));

export function resolveEnvAdapterSpec(spec) {
  if (!spec || !spec.trim())
    throw new Error("no adapter: set OPERATOR_SHEETS_ADAPTER to the reviewed adapter module");
  let resolved;
  try {
    resolved = realpathSync(path.resolve(spec.trim()));
  } catch {
    throw new Error("OPERATOR_SHEETS_ADAPTER does not resolve to a readable module");
  }
  if (resolved !== SHIPPED_ADAPTER_PATH)
    throw new Error("OPERATOR_SHEETS_ADAPTER must name the shipped reviewed adapter (operator_sheets_adapter.mjs)");
  return pathToFileURL(resolved).href;
}

export async function loadAdapter(spec) {
  if (!spec || !spec.trim())
    throw new Error("no adapter: set OPERATOR_SHEETS_ADAPTER to the reviewed adapter module");
  const url = spec.startsWith(".") || spec.startsWith("/")
    ? pathToFileURL(path.resolve(spec)).href
    : spec;
  const mod = await import(url);
  const candidate = mod.default ?? mod;
  const adapter = typeof candidate.createAdapter === "function"
    ? await candidate.createAdapter()
    : candidate;
  for (const fn of ["validateHeaders", "readRows", "appendSession", "updateSessionRow", "appendEvent"])
    if (typeof adapter[fn] !== "function")
      throw new Error(`adapter from OPERATOR_SHEETS_ADAPTER lacks ${fn}`);
  return adapter;
}

// The single-runner lock:  the drain re-execs ITSELF under flock(2) with --no-fork, so the
// drain process holds the lock on its own inherited descriptor - no supervisor, no helper
// child, nothing to strand.  Review rejected both earlier designs:  the pid file (empty-file
// and stale-generation races, no fencing) and the `flock -c "sleep infinity"` supervisor
// (orphanable holder, no acquisition handshake, unobserved holder death).  Here flock's gate
// is the handshake (the command starts only after acquisition), and every drain exit path -
// normal, error, crash, SIGKILL - closes the descriptor with the process.  --nonblock with
// -E 73 makes "busy" its own code so it can never pass for a drain result.
//
// Custody is PROVEN, never attested:  an environment marker could be set by anything, so the
// re-exec'd drain verifies from /proc/self/fd that THIS process owns an flock'd descriptor
// for the lock file (holdingFlock).  No matching descriptor means no lock, and the run
// re-execs under flock instead of draining unlocked.
const FLOCK_BUSY = 73;

// One canonical store per host install, so one lock NAME:  keying on a dispatcher path or a
// --db spelling let two wrapper copies drain the same store past each other.  The host
// install must pin OPERATOR_EXPORT_LOCK_DIR to a single host-wide directory so every
// invocation shares this one lock.
export const LOCK_NAME = "operator-registry-export.lock";

export function lockBusyError(lockPath) {
  const err = new Error(
    `another exporter holds ${lockPath}: refusing to run two drains over one store`);
  err.code = "LOCK_BUSY";
  return err;
}

// True only when THIS process holds an flock(2) write lock on lockPath:  an open descriptor
// whose /proc/self/fd target is the lock file and whose fdinfo records the FLOCK lock.
// Anything unreadable or absent means NOT held - custody doubts fail closed.
export function holdingFlock(lockPath, procRoot = "/proc/self/fd") {
  let wanted;
  try {
    wanted = realpathSync(lockPath);
  } catch {
    return false; // no lock file at all:  nothing can be held on it
  }
  let fds;
  try {
    fds = readdirSync(procRoot);
  } catch {
    return false;
  }
  for (const fd of fds) {
    let target;
    try {
      target = realpathSync(`${procRoot}/${fd}`);
    } catch {
      continue;
    }
    if (target !== wanted) continue;
    let info;
    try {
      info = readFileSync(`${procRoot}info/${fd}`, "utf8");
    } catch {
      continue;
    }
    if (/lock:\s+\d+:\s+FLOCK\s+ADVISORY\s+WRITE/.test(info)) return true;
  }
  return false;
}

// Re-exec this same script under flock --no-fork:  the child IS the drain and IS the holder.
// Returns the drain's exit code, or 3 when busy.  A spawn failure (no flock binary) fails
// closed as a configuration error (2).
export async function execUnderFlock(argv, lockPath, deps = {}) {
  const spawnImpl = deps.spawnImpl ?? spawn;
  const self = fileURLToPath(import.meta.url);
  const child = spawnImpl("flock",
    ["--no-fork", "--nonblock", "--conflict-exit-code", String(FLOCK_BUSY), lockPath,
      process.execPath, self, ...argv],
    { stdio: "inherit" });
  const code = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (c) => resolve(c ?? 1));
  });
  if (code === FLOCK_BUSY) {
    console.error(`operator-sheets-export-run: ${lockBusyError(lockPath).message}`);
    return 3;
  }
  return code;
}

// The lock is per host install:  a fixed name in the pinned (or default state) directory.
export function runLockPath(env = process.env) {
  const dir = env.OPERATOR_EXPORT_LOCK_DIR
    || path.join(env.XDG_STATE_HOME || path.join(homedir(), ".local", "state"),
      "operator-sheets-export");
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  return path.join(dir, LOCK_NAME);
}

export async function runOnce({ outbox, adapter, limit, log = (m) => console.error(`export: ${m}`) }) {
  return drainOutbox({ outbox, adapter, limit, log });
}

export async function main(argv, deps = {}) {
  let args;
  try {
    args = parseArgs(argv);
  } catch (err) {
    console.error(`operator-sheets-export-run: ${err.message}`);
    return 2;
  }
  let adapter;
  try {
    const adapterSpec = deps.adapterSpec !== undefined
      ? deps.adapterSpec
      : resolveEnvAdapterSpec(process.env.OPERATOR_SHEETS_ADAPTER ?? "");
    adapter = await loadAdapter(adapterSpec);
  } catch (err) {
    console.error(`operator-sheets-export-run: ${err.message}`);
    return 2;
  }
  const outbox = deps.outbox ?? (args.dispatcher
    ? dispatchOutbox(args)
    : cliOutbox(args));
  if (!deps.skipLock) {
    let lockPath;
    try {
      lockPath = runLockPath(deps.env ?? process.env);
    } catch (err) {
      console.error(`operator-sheets-export-run: ${err.message}`);
      return 2;
    }
    // Custody is proven from this process's own descriptors, never from an env marker:
    // only a drain actually holding the flock proceeds in-process.
    if (!(deps.holdingFlockImpl ?? holdingFlock)(lockPath)) {
      try {
        return await execUnderFlock(argv, lockPath, deps.lockDeps ?? {});
      } catch (err) {
        console.error(`operator-sheets-export-run: ${err.message}`);
        return 2;
      }
    }
  }
  const once = async () => {
    try {
      const { exported, failed } = await runOnce({ outbox, adapter, limit: args.limit });
      console.log(JSON.stringify({ exported, failed }));
      return failed ? 1 : 0;
    } catch (err) {
      // A bridge failure is a clean logged exit 1, never an unhandled rejection.
      console.error(`operator-sheets-export-run: ${err.message}`);
      return 1;
    }
  };
  if (args.once) return once();
  // --loop: a stopped drain never overtakes itself - each pass is a full reconcile.
  for (;;) {
    await once();
    await new Promise((resolve) => setTimeout(resolve, args.intervalMs));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then((code) => process.exit(code));
}
