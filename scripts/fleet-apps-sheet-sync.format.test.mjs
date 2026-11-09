import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { buildFormatTabRequests, isScriptEntryPoint } from "./fleet-apps-sheet-sync.mjs";

const syncScriptPath = fileURLToPath(new URL("./fleet-apps-sheet-sync.mjs", import.meta.url));

test("format requests wrap all columns and avoid auto-resize", () => {
  const rowCount = 42;
  const requests = buildFormatTabRequests(42, [0], 3, [320, 220, 320], rowCount);
  assert.equal(
    requests.some(r => r.autoResizeDimensions),
    false,
    "autoResizeDimensions widens a single column to fit the longest cell"
  );
  const wrap = requests.find(
    r => r.repeatCell?.cell?.userEnteredFormat?.wrapStrategy === "WRAP"
  );
  assert.ok(wrap);
  assert.equal(wrap.repeatCell.range.sheetId, 42);
  assert.equal(wrap.repeatCell.range.endRowIndex, rowCount);
  assert.equal(wrap.repeatCell.range.endColumnIndex, 3);
  assert.equal(wrap.repeatCell.cell.userEnteredFormat.verticalAlignment, "TOP");
});

test("format requests keep header bold and background", () => {
  const requests = buildFormatTabRequests(1, [0, 3], 9, null, 50);
  const headers = requests.filter(r => r.repeatCell?.cell?.userEnteredFormat?.textFormat?.bold);
  assert.equal(headers.length, 2);
  for (const h of headers) {
    assert.deepEqual(h.repeatCell.cell.userEnteredFormat.backgroundColor, {
      red: 0.88,
      green: 0.9,
      blue: 0.94
    });
  }
});

test("format requests set explicit column pixel widths", () => {
  const widths = [100, 200, 300];
  const requests = buildFormatTabRequests(5, [0], 3, widths, 10);
  const dim = requests.filter(r => r.updateDimensionProperties?.range?.dimension === "COLUMNS");
  assert.equal(dim.length, 3);
  assert.deepEqual(
    dim.map(d => d.updateDimensionProperties.properties.pixelSize),
    widths
  );
});

test("column widths above MAX_COL_WIDTH_PX are clamped", () => {
  const requests = buildFormatTabRequests(1, [0], 2, [400, 100], 5);
  const dim = requests.filter(r => r.updateDimensionProperties?.range?.dimension === "COLUMNS");
  assert.equal(dim[0].updateDimensionProperties.properties.pixelSize, 320);
  assert.equal(dim[1].updateDimensionProperties.properties.pixelSize, 100);
});

test("format requests freeze below the last header row index", () => {
  const requests = buildFormatTabRequests(9, [3], 9, [], 20);
  const freeze = requests.find(r => r.updateSheetProperties?.properties?.gridProperties?.frozenRowCount !== undefined);
  assert.equal(freeze.updateSheetProperties.properties.gridProperties.frozenRowCount, 4);
});

test("isScriptEntryPoint recognizes the sync script path", () => {
  assert.equal(isScriptEntryPoint(syncScriptPath), true);
  assert.equal(isScriptEntryPoint("/nonexistent/path.mjs"), false);
});
