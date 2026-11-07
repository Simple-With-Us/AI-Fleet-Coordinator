// Tests for the OPS-1037 drain runner's static contract: the workbook profile it
// ships must stay projectable from the canonical payload layout, and the writer
// lease must exclude a second live drain while recovering from a stale one.
// Run: node --test scripts/test_operator_sheets_drain.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { PAYLOAD_HEADERS, projectCells } from "./agent_sync/operator_sheets_export.mjs";
import {
  acquireLease, EVENTS_HEADERS, SESSIONS_HEADERS, TABLES, workbookProfileFromEnv,
} from "./agent_sync/operator_sheets_drain.mjs";

test("runner profile: workbook identity resolves from env and fails closed", () => {
  const profile = workbookProfileFromEnv({
    OPERATOR_SHEETS_WORKBOOK_ID: "workbook_ABC-123",
    OPERATOR_SHEETS_SESSIONS_SHEET_ID: "1000000001",
    OPERATOR_SHEETS_EVENTS_SHEET_ID: "1000000002",
  });
  assert.deepEqual(profile, {
    spreadsheetId: "workbook_ABC-123",
    sheetIds: { sessions: 1000000001, events: 1000000002 },
  });
  assert.throws(() => workbookProfileFromEnv({}), /OPERATOR_SHEETS_WORKBOOK_ID is required/);
  assert.throws(() => workbookProfileFromEnv({
    OPERATOR_SHEETS_WORKBOOK_ID: "workbook_ABC-123",
    OPERATOR_SHEETS_SESSIONS_SHEET_ID: "5",
    OPERATOR_SHEETS_EVENTS_SHEET_ID: "5",
  }), /distinct/);
});

test("runner profile: every workbook header projects from the payload layout", () => {
  for (const table of ["sessions", "events"]) {
    const layout = table === "sessions" ? SESSIONS_HEADERS : EVENTS_HEADERS;
    assert.deepEqual(TABLES[table].headers, layout);
    assert.equal(layout[0], PAYLOAD_HEADERS[table][0]); // ID column stays first
    const cells = PAYLOAD_HEADERS[table].map((_, i) => `v${i}`);
    assert.equal(projectCells(table, cells, layout).length, layout.length);
  }
  assert.deepEqual(EVENTS_HEADERS, [...PAYLOAD_HEADERS.events]); // events need no projection loss
});

test("writer lease: a live holder excludes, a stale lock is stolen", async () => {
  const dir = await mkdtemp(join(tmpdir(), "drain-lease-"));
  const lock = join(dir, "drain.lock");
  const first = await acquireLease(lock);
  assert.ok(first);
  assert.equal(await acquireLease(lock), null); // our own pid is alive
  await first.close();
  // Simulate a crashed holder: lock file names a pid that cannot exist.
  await writeFile(lock, "999999999");
  const stolen = await acquireLease(lock);
  assert.ok(stolen);
  assert.equal((await readFile(lock, "utf8")).trim(), String(process.pid));
  await stolen.close();
});
