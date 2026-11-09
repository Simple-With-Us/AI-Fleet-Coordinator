#!/usr/bin/env node
"use strict";

// Thin ACP stdio proxy in front of agy-acp-turbo / agy-acp.
// Advertises session/list so Shellular's generic ACP runtime will enumerate
// Antigravity sessions.  Does not rewrite agy-acp.  Does not implement a
// standalone JSONL agent.  Diagnostics go to stderr only.

const { execFileSync, spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const LIST_METHODS = new Set(["session/list", "sessions/list"]);
const DEFAULT_LIST_LIMIT = 100;
const MAX_TRANSCRIPT_BYTES = 64 * 1024;
const TITLE_MAX = 80;

function log(message) {
  process.stderr.write(`agy-acp-list-wrapper: ${message}\n`);
}

function resolveRoots(env) {
  const home = env.HOME || os.homedir();
  const root =
    env.AGY_ACP_HOME ||
    env.ANTIGRAVITY_CLI_ROOT ||
    path.join(home, ".gemini", "antigravity-cli");
  const geminiParent = path.dirname(root);
  return {
    root,
    lastConversations:
      env.AGY_ACP_LAST_CONVERSATIONS ||
      path.join(root, "cache", "last_conversations.json"),
    brainDir: env.AGY_ACP_BRAIN_DIR || path.join(root, "brain"),
    guiBrainDir:
      env.AGY_ACP_GUI_BRAIN_DIR ||
      path.join(geminiParent, "antigravity", "brain"),
    ideBrainDir:
      env.AGY_ACP_IDE_BRAIN_DIR ||
      path.join(geminiParent, "antigravity-ide", "brain"),
    conversationsDir:
      env.AGY_ACP_CONVERSATIONS_DIR || path.join(root, "conversations"),
    guiConversationsDir:
      env.AGY_ACP_GUI_CONVERSATIONS_DIR ||
      path.join(geminiParent, "antigravity", "conversations"),
    summariesDb:
      env.AGY_ACP_SUMMARIES_DB ||
      path.join(root, "conversation_summaries.db"),
    listLimit: Math.max(1, Number(env.AGY_ACP_LIST_LIMIT) || DEFAULT_LIST_LIMIT),
    // v1 stays last-per-cwd so the list cannot go stale.  Extra *.db history
    // is opt-in after last_conversations.json exists.
    listExtraDbs: env.AGY_ACP_LIST_EXTRA_DBS === "1",
    fallbackCwd: home && path.isAbsolute(home) ? home : "/",
  };
}

function rewriteInitializeResult(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    return result;
  }
  const caps =
    result.agentCapabilities &&
    typeof result.agentCapabilities === "object" &&
    !Array.isArray(result.agentCapabilities)
      ? { ...result.agentCapabilities }
      : {};
  const sessionCaps =
    caps.sessionCapabilities &&
    typeof caps.sessionCapabilities === "object" &&
    !Array.isArray(caps.sessionCapabilities)
      ? { ...caps.sessionCapabilities }
      : {};
  sessionCaps.list = true;
  caps.sessionCapabilities = sessionCaps;
  return { ...result, agentCapabilities: caps };
}

function isoFromMtime(mtimeMs) {
  if (!Number.isFinite(mtimeMs) || mtimeMs <= 0) {
    return undefined;
  }
  return new Date(mtimeMs).toISOString();
}

function readJsonFile(filePath) {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    return JSON.parse(raw);
  } catch {
    return undefined;
  }
}

function addCwdMapping(map, cwd, sessionId) {
  if (typeof sessionId !== "string" || !sessionId.trim()) {
    return;
  }
  if (typeof cwd !== "string" || !path.isAbsolute(cwd)) {
    return;
  }
  const id = sessionId.trim();
  if (!map.has(id)) {
    map.set(id, cwd);
  }
}

function ingestLastConversations(value, map) {
  // Live file is only { "<cwd>": "<uuid>" }.  Do not invent other shapes.
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return;
  }
  for (const [cwd, sessionId] of Object.entries(value)) {
    if (typeof sessionId === "string") {
      addCwdMapping(map, cwd, sessionId);
    }
  }
}

function extractUserTitle(content) {
  if (typeof content !== "string" || !content.trim()) {
    return undefined;
  }
  const match = content.match(/<USER_REQUEST>\s*([\s\S]*?)\s*<\/USER_REQUEST>/);
  const text = (match && match[1] ? match[1] : content)
    .replace(/<ADDITIONAL_METADATA>[\s\S]*?<\/ADDITIONAL_METADATA>/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) {
    return undefined;
  }
  return text.length > TITLE_MAX ? `${text.slice(0, TITLE_MAX - 1)}…` : text;
}

function transcriptPath(brainDir, sessionId) {
  return path.join(
    brainDir,
    sessionId,
    ".system_generated",
    "logs",
    "transcript.jsonl",
  );
}

function conversationDbPath(conversationsDir, sessionId) {
  return path.join(conversationsDir, `${sessionId}.db`);
}

function readTranscriptRows(filePath, fromEnd) {
  const rows = [];
  try {
    const st = fs.statSync(filePath);
    const start = fromEnd ? Math.max(0, st.size - MAX_TRANSCRIPT_BYTES) : 0;
    const fd = fs.openSync(filePath, "r");
    try {
      const buf = Buffer.alloc(Math.min(MAX_TRANSCRIPT_BYTES, st.size || MAX_TRANSCRIPT_BYTES));
      const n = fs.readSync(fd, buf, 0, buf.length, start);
      const text = buf.slice(0, n).toString("utf8");
      for (const line of text.split(/\r?\n/)) {
        if (!line.trim()) {
          continue;
        }
        try {
          const row = JSON.parse(line);
          if (row && typeof row === "object") {
            rows.push(row);
          }
        } catch {
          // Skip a torn JSONL line.
        }
      }
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return rows;
  }
  return rows;
}

function titleFromTranscript(filePath) {
  for (const row of readTranscriptRows(filePath, false)) {
    const type = String(row.type || "");
    const source = String(row.source || "");
    if (type === "USER_INPUT" || source === "USER_EXPLICIT") {
      const title = extractUserTitle(row.content);
      if (title) {
        return title;
      }
    }
  }
  return undefined;
}

function lastCreatedAtFromTranscript(filePath) {
  let last;
  for (const row of readTranscriptRows(filePath, true)) {
    const raw = row.created_at || row.createdAt || row.ts;
    if (typeof raw === "string" && raw.trim()) {
      const ms = Date.parse(raw);
      if (Number.isFinite(ms)) {
        last = ms;
      }
    } else if (typeof raw === "number" && Number.isFinite(raw)) {
      last = raw < 1e12 ? raw * 1000 : raw;
    }
  }
  return last;
}

function titleFromSummaries(dbPath, sessionId) {
  if (!dbPath || !sessionId) {
    return undefined;
  }
  try {
    if (!fs.existsSync(dbPath)) {
      return undefined;
    }
  } catch {
    return undefined;
  }
  const escaped = String(sessionId).replace(/'/g, "''");
  const sql =
    "SELECT title, preview FROM conversation_summaries " +
    `WHERE conversation_id = '${escaped}' OR id = '${escaped}' LIMIT 1;`;
  try {
    const out = execFileSync("sqlite3", ["-json", dbPath, sql], {
      encoding: "utf8",
      timeout: 1500,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const rows = JSON.parse(out || "[]");
    if (!Array.isArray(rows) || !rows[0]) {
      return undefined;
    }
    const title = extractUserTitle(rows[0].title) || extractUserTitle(rows[0].preview);
    return title;
  } catch {
    return undefined;
  }
}

function upsertSession(byId, sessionId, patch) {
  const current = byId.get(sessionId) || { sessionId };
  if (patch.cwd && !current.cwd) {
    current.cwd = patch.cwd;
  }
  if (patch.title && !current.title) {
    current.title = patch.title;
  }
  if (
    Number.isFinite(patch.mtimeMs) &&
    (!Number.isFinite(current.mtimeMs) || patch.mtimeMs > current.mtimeMs)
  ) {
    current.mtimeMs = patch.mtimeMs;
  }
  byId.set(sessionId, current);
}

function dbMtimeMs(conversationsDir, sessionId) {
  try {
    const st = fs.statSync(conversationDbPath(conversationsDir, sessionId));
    if (st.isFile()) {
      return st.mtimeMs;
    }
  } catch {
    return undefined;
  }
  return undefined;
}

function firstTranscriptPath(roots, sessionId) {
  for (const brainDir of [roots.brainDir, roots.guiBrainDir, roots.ideBrainDir]) {
    const filePath = transcriptPath(brainDir, sessionId);
    try {
      if (fs.existsSync(filePath)) {
        return filePath;
      }
    } catch {
      // Keep looking in the next brain root.
    }
  }
  return undefined;
}

function enrichSession(roots, sessionId, cwd) {
  const transcript = firstTranscriptPath(roots, sessionId);
  const title =
    titleFromSummaries(roots.summariesDb, sessionId) ||
    (transcript ? titleFromTranscript(transcript) : undefined) ||
    "Untitled";
  const mtimeMs =
    dbMtimeMs(roots.conversationsDir, sessionId) ||
    (transcript ? lastCreatedAtFromTranscript(transcript) : undefined);
  return { sessionId, cwd: cwd || roots.fallbackCwd, title, mtimeMs };
}

function scanExtraDbs(conversationsDir, byId) {
  let names;
  try {
    names = fs.readdirSync(conversationsDir);
  } catch {
    return;
  }
  for (const name of names) {
    if (!name.toLowerCase().endsWith(".db")) {
      continue;
    }
    const sessionId = name.slice(0, -3);
    if (!sessionId || sessionId.startsWith(".") || byId.has(sessionId)) {
      continue;
    }
    let st;
    try {
      st = fs.statSync(path.join(conversationsDir, name));
    } catch {
      continue;
    }
    if (!st.isFile()) {
      continue;
    }
    upsertSession(byId, sessionId, { mtimeMs: st.mtimeMs });
  }
}

function listAntigravitySessions(options) {
  const env = (options && options.env) || process.env;
  const filterCwd = options && options.cwd;
  const roots = resolveRoots(env);
  try {
    if (!fs.existsSync(roots.lastConversations)) {
      return [];
    }
    const last = readJsonFile(roots.lastConversations);
    if (last === undefined) {
      return [];
    }
    const cwdById = new Map();
    ingestLastConversations(last, cwdById);
    const byId = new Map();
    for (const [sessionId, cwd] of cwdById.entries()) {
      upsertSession(byId, sessionId, { cwd });
    }
    if (roots.listExtraDbs) {
      scanExtraDbs(roots.conversationsDir, byId);
      scanExtraDbs(roots.guiConversationsDir, byId);
    }

    const sessions = [];
    for (const rec of byId.values()) {
      const enriched = enrichSession(roots, rec.sessionId, rec.cwd);
      if (typeof filterCwd === "string" && filterCwd && enriched.cwd !== filterCwd) {
        continue;
      }
      const item = {
        sessionId: enriched.sessionId,
        cwd: enriched.cwd,
        title: enriched.title,
      };
      const updatedAt = isoFromMtime(enriched.mtimeMs);
      if (updatedAt) {
        item.updatedAt = updatedAt;
      }
      sessions.push(item);
    }
    sessions.sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")));
    return sessions.slice(0, roots.listLimit);
  } catch (err) {
    log(`list failed closed: ${err && err.message ? err.message : err}`);
    return [];
  }
}

function writeJson(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function reply(id, result) {
  writeJson({ jsonrpc: "2.0", id, result });
}

function resolveChild(env, extraArgs) {
  const args = extraArgs.slice();
  if (env.AGY_ACP_CHILD) {
    return { cmd: env.AGY_ACP_CHILD, args };
  }
  const turbo = path.join(__dirname, "agy-acp-turbo.sh");
  try {
    fs.accessSync(turbo, fs.constants.X_OK);
    return { cmd: turbo, args };
  } catch {
    // Fall through to the live binary.
  }
  const agy = "/usr/local/bin/agy-acp";
  try {
    fs.accessSync(agy, fs.constants.X_OK);
    return { cmd: agy, args: ["--skip-naration", ...args] };
  } catch {
    return null;
  }
}

function runProxy() {
  const childSpec = resolveChild(process.env, process.argv.slice(2));
  if (!childSpec) {
    log("no child: set AGY_ACP_CHILD or install agy-acp-turbo.sh / /usr/local/bin/agy-acp");
    process.exit(1);
  }

  const pendingInitIds = new Set();
  const queuedLists = [];
  let initFailed = false;
  let initSettled = false;

  const child = spawn(childSpec.cmd, childSpec.args, {
    stdio: ["pipe", "pipe", "inherit"],
    env: process.env,
  });

  child.on("error", (err) => {
    log(`failed to spawn ${childSpec.cmd}: ${err.message}`);
    process.exit(1);
  });

  const flushLists = () => {
    const pending = queuedLists.splice(0, queuedLists.length);
    for (const msg of pending) {
      if (initFailed) {
        reply(msg.id, { sessions: [] });
        continue;
      }
      const params = msg.params && typeof msg.params === "object" ? msg.params : {};
      const cwd = typeof params.cwd === "string" ? params.cwd : undefined;
      reply(msg.id, { sessions: listAntigravitySessions({ env: process.env, cwd }) });
    }
  };

  const failClosedLists = () => {
    initFailed = true;
    initSettled = true;
    flushLists();
  };

  child.on("exit", (code, signal) => {
    if (!initSettled) {
      failClosedLists();
    }
    if (signal) {
      process.exit(1);
    }
    process.exit(code == null ? 1 : code);
  });

  const sendChild = (line) => {
    if (!child.stdin || child.stdin.destroyed) {
      return;
    }
    child.stdin.write(`${line}\n`);
  };

  const handleList = (msg) => {
    if (!initSettled) {
      queuedLists.push(msg);
      return;
    }
    queuedLists.push(msg);
    flushLists();
  };

  const onClientLine = (line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      return;
    }
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      sendChild(trimmed);
      return;
    }
    if (msg && LIST_METHODS.has(msg.method) && msg.id != null) {
      handleList(msg);
      return;
    }
    if (msg && msg.method === "initialize" && msg.id != null) {
      pendingInitIds.add(msg.id);
    }
    sendChild(trimmed);
  };

  const onChildLine = (line) => {
    const trimmed = line.trim();
    if (!trimmed) {
      return;
    }
    let msg;
    try {
      msg = JSON.parse(trimmed);
    } catch {
      process.stdout.write(`${trimmed}\n`);
      return;
    }
    if (msg && msg.id != null && pendingInitIds.has(msg.id)) {
      pendingInitIds.delete(msg.id);
      if (msg.error) {
        writeJson(msg);
        failClosedLists();
        return;
      }
      if (msg.result !== undefined) {
        writeJson({ ...msg, result: rewriteInitializeResult(msg.result) });
        initSettled = true;
        flushLists();
        return;
      }
    }
    writeJson(msg);
  };

  const clientRl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });
  const childRl = readline.createInterface({
    input: child.stdout,
    crlfDelay: Infinity,
  });

  clientRl.on("line", onClientLine);
  childRl.on("line", onChildLine);
  clientRl.on("close", () => {
    if (child.stdin && !child.stdin.destroyed) {
      child.stdin.end();
    }
  });

  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => {
      try {
        child.kill(sig);
      } catch {
        // Child may already be gone.
      }
    });
  }
}

module.exports = {
  listAntigravitySessions,
  resolveRoots,
  rewriteInitializeResult,
};

if (require.main === module && path.resolve(process.argv[1] || "") === __filename) {
  runProxy();
}
