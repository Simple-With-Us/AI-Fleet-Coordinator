import { test } from "node:test";
import assert from "node:assert/strict";
import { buildFormatTabRequests } from "./fleet-apps-sheet-sync.mjs";

test("format requests wrap all columns and avoid auto-resize", () => {
  const requests = buildFormatTabRequests(42, [0], 3, [88, 220, 420]);
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
  assert.equal(wrap.repeatCell.range.endColumnIndex, 3);
  assert.equal(wrap.repeatCell.cell.userEnteredFormat.verticalAlignment, "TOP");
});

test("format requests keep header bold and background", () => {
  const requests = buildFormatTabRequests(1, [0, 3], 9, null);
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
  const requests = buildFormatTabRequests(5, [0], 3, widths);
  const dim = requests.filter(r => r.updateDimensionProperties?.range?.dimension === "COLUMNS");
  assert.equal(dim.length, 3);
  assert.deepEqual(
    dim.map(d => d.updateDimensionProperties.properties.pixelSize),
    widths
  );
});

test("format requests freeze below the last header row index", () => {
  const requests = buildFormatTabRequests(9, [3], 9, []);
  const freeze = requests.find(r => r.updateSheetProperties?.properties?.gridProperties?.frozenRowCount !== undefined);
  assert.equal(freeze.updateSheetProperties.properties.gridProperties.frozenRowCount, 4);
});
