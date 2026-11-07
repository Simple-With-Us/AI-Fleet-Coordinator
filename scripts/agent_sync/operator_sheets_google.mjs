/** Google Sheets transport for the Operator outbox.  Importing performs no I/O. */
import { createSign } from 'node:crypto';

const OAUTH_URL = 'https://oauth2.googleapis.com/token';
const SCOPE = 'https://www.googleapis.com/auth/spreadsheets';
const MAX_ROWS = 500;
const MAX_COLUMNS = 256;
const MAX_ROW_NUMBER = 10_000_000;
const MAX_RESPONSE_BYTES = 4 * 1024 * 1024;
const MAX_RESPONSE_DEPTH = 32;
const MAX_RESPONSE_NODES = 100_000;

// Credential shapes mirrored from the fleet scanner (scripts/agent_sync/secretscan.py
// _PATTERNS).  Outbound cell text is refused before any request, never persisted externally.
const CELL_CREDENTIAL_PATTERNS = Object.freeze([
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----/, 'private key block'],
  [/\b(AKIA|ASIA)[0-9A-Z]{16}\b/, 'AWS access key'],
  [/\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b/, 'GitHub token'],
  [/\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b/, 'Anthropic or OpenAI key'],
  [/\bxox[abposr]-[A-Za-z0-9-]{10,}\b/, 'Slack token'],
  [/\bglpat-[A-Za-z0-9_-]{20,}\b/, 'GitLab token'],
  [/\bAIza[0-9A-Za-z_-]{35}\b/, 'Google API key'],
  [/\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}\b/, 'Stripe key'],
  [/\blin_(api|oauth|wh)_[A-Za-z0-9]{20,}/, 'Linear API key'],
  [/\blinear[\w -]{0,24}secret\s*[=:]\s*['"]?[A-Za-z0-9]{24,}/i, 'Linear client secret'],
  [/\bsntry[su]_[A-Za-z0-9+/=_-]{20,}/, 'Sentry token'],
  [/\bxai-[A-Za-z0-9]{20,}/, 'xAI key'],
  [/\bst\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}/, 'Infisical service token'],
  [/\binfisical[\w .-]{0,40}?(token|secret)['"]?\s*[=:]\s*['"]?[A-Za-z0-9+/=._-]{24,}/i, 'Infisical credential'],
  [/\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/, 'access token (JWT)'],
  [/\bcf(ut|at|k)_[A-Za-z0-9]{30,}/, 'Cloudflare token'],
  [/\bcloudflare[\w ]{0,24}(token|key)\s*[=:]\s*['"]?[A-Za-z0-9_-]{37,}/i, 'Cloudflare token'],
  [/(?<![A-Za-z0-9])\d{1,5}\|[A-Za-z0-9]{36,}(?![A-Za-z0-9])/, 'Coolify token'],
  [/\b[A-Z][A-Z0-9_]*(SECRET|TOKEN|PASSWORD|API_?KEY)\s*=\s*['"]?[A-Za-z0-9+/=._-]{16,}/, 'assigned credential'],
  [/\bauthorization:\s*(basic|bearer)\s+[A-Za-z0-9+/=._-]{16,}/i, 'bearer token'],
  [/\b(api[_-]?key|secret|token|password)\s*[=:]\s*['"]?[A-Za-z0-9+/=._-]{16,}/i, 'assigned secret'],
]);

export function scanCellText(text) {
  for (const [pattern, label] of CELL_CREDENTIAL_PATTERNS) {
    if (pattern.test(text)) return label;
  }
  return null;
}

// Iterative so a hostile payload cannot exhaust the stack before the depth check runs.
function assertShapeLimits(value) {
  const stack = [[value, 0]];
  let nodes = 0;
  while (stack.length) {
    const [node, depth] = stack.pop();
    nodes += 1;
    if (depth > MAX_RESPONSE_DEPTH || nodes > MAX_RESPONSE_NODES) return false;
    if (node && typeof node === 'object') {
      for (const child of Object.values(node)) stack.push([child, depth + 1]);
    }
  }
  return true;
}

async function readCappedBody(response, mutation) {
  const reader = response.body?.getReader?.();
  if (!reader) fail('invalid_response', { uncertain: mutation });
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_RESPONSE_BYTES) {
      await reader.cancel().catch(() => {});
      fail('response_too_large', { uncertain: mutation });
    }
    chunks.push(value);
  }
  return new TextDecoder().decode(Buffer.concat(chunks.map(c => Buffer.from(c))));
}

export const SESSION_HEADERS = Object.freeze([
  'Record ID', 'Label', 'Seat', 'Platform', 'Host', 'Native Session ID',
  'Title', 'Project', 'Thread Link', 'Apple Note Link', 'State',
  'Wake Capability', 'Registered At', 'Last Seen At', 'Closed At', 'Source',
]);
export const EVENT_HEADERS = Object.freeze([
  'Event ID', 'Recorded At', 'Session Record ID', 'Label', 'Event',
  'Actor', 'Outcome', 'Source', 'Details',
]);
const TABLES = Object.freeze({
  sessions: Object.freeze({ title: 'Operator Sessions', headers: SESSION_HEADERS }),
  events: Object.freeze({ title: 'Operator Events', headers: EVENT_HEADERS }),
});

// A workbook profile: tab titles and headers are configuration, not constants
// (OPS-1037). The default profile above is the original workbook; the SWU Operator
// workbook's tabs are titled 'Sessions' / 'Events' and its Sessions tab carries a
// 10-column subset of the session layout. A profile never renames anything in the
// workbook - it only tells the transport what is actually there.
function normalizeTables(tables) {
  if (tables === null || tables === undefined) return TABLES;
  if (typeof tables !== 'object') fail('invalid_config');
  const out = {};
  const titles = new Set();
  for (const table of Object.keys(TABLES)) {
    const def = tables[table];
    if (!def || typeof def.title !== 'string' || !def.title || def.title.length > 256 ||
        /[\r\n]/.test(def.title) || titles.has(def.title)) fail('invalid_config');
    if (!Array.isArray(def.headers) || def.headers.length < 1 ||
        def.headers.length > MAX_COLUMNS ||
        def.headers.some(h => typeof h !== 'string' || !h)) fail('invalid_config');
    titles.add(def.title);
    out[table] = Object.freeze({ title: def.title, headers: Object.freeze([...def.headers]) });
  }
  return Object.freeze(out);
}

// Parse a write confirmation range ('Title'!A2:J2 or Title!A2:J2) against the
// configured tab title. Returns { row, endColumn, endRow } or null.
function parseUpdatedRange(range, title) {
  if (typeof range !== 'string') return null;
  const bang = range.lastIndexOf('!');
  if (bang < 1) return null;
  let sheet = range.slice(0, bang);
  if (sheet.length >= 2 && sheet.startsWith("'") && sheet.endsWith("'")) {
    sheet = sheet.slice(1, -1).replace(/''/g, "'");
  }
  if (sheet !== title) return null;
  const m = range.slice(bang + 1).match(/^A([0-9]+):([A-Z]+)([0-9]+)$/);
  if (!m) return null;
  return { row: Number(m[1]), endColumn: m[2], endRow: Number(m[3]) };
}

/** Safe to log: no upstream bodies, cell contents, credentials or exception causes. */
export class OperatorSheetsError extends Error {
  constructor(code, { status = null, uncertain = false, retryAfterSeconds = null } = {}) {
    super(`Operator Sheets: ${code}`);
    this.name = 'OperatorSheetsError';
    this.code = code;
    this.status = status;
    this.uncertain = uncertain;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function fail(code, options) { throw new OperatorSheetsError(code, options); }
function integer(value, min, max, code) {
  if (!Number.isSafeInteger(value) || value < min || value > max) fail(code);
  return value;
}
function columnName(value) {
  let result = '';
  for (let n = value; n > 0; n = Math.floor((n - 1) / 26)) {
    result = String.fromCharCode(65 + (n - 1) % 26) + result;
  }
  return result;
}
function assertKnownTable(table) {
  if (!Object.hasOwn(TABLES, table)) fail('invalid_table');
}
function retryAfter(response) {
  const value = response.headers.get('retry-after');
  // Only numeric durations are exposed; arbitrary response header text is discarded.
  return /^\d+$/.test(value ?? '') ? Math.min(Number(value), 3600) : null;
}

// Every endpoint passes its own schema:  the decoded body is validated before any caller sees
// it, so a missing or malformed field fails closed here instead of surfacing downstream.
async function requestJSON(fetchImpl, url, init, timeoutMs, mutation = false, validate = null,
  invalidCode = 'invalid_response') {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      ...init, redirect: 'error', signal: controller.signal,
    });
    if (!response.ok) {
      // A non-success response body can contain echoed data.  Never include it.
      await response.body?.cancel();
      const rejected = [400, 401, 403, 404, 409, 412, 413, 422].includes(response.status);
      fail('http_error', {
        status: response.status, uncertain: mutation && !rejected,
        retryAfterSeconds: retryAfter(response),
      });
    }
    // A read failure (timeout, reset mid-body) is a transport error: let it propagate to the
    // outer catch as transport_or_response_error.  Only a JSON.parse failure means the
    // response shape itself was invalid.
    const text = await readCappedBody(response, mutation);
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      fail('invalid_response', { uncertain: mutation });
    }
    if (!body || typeof body !== 'object' || Array.isArray(body) ||
        !assertShapeLimits(body) || (validate !== null && !validate(body))) {
      fail(invalidCode, { uncertain: mutation });
    }
    return body;
  } catch (error) {
    if (error instanceof OperatorSheetsError) throw error;
    fail('transport_or_response_error', { uncertain: mutation });
  } finally {
    clearTimeout(timer);
  }
}

const TOKEN_RESPONSE = body => typeof body.access_token === 'string' &&
  typeof body.token_type === 'string' && Number.isFinite(body.expires_in);
const METADATA_RESPONSE = body => Array.isArray(body.sheets);
const VALUES_RESPONSE = body => body.values === undefined || Array.isArray(body.values);
const WRITE_RESPONSE = body => typeof body.spreadsheetId === 'string' &&
  (body.updates === undefined || (body.updates && typeof body.updates === 'object'));

/**
 * Credential loading is supplied by the authorized runtime, never a guessed path.
 * Concurrent refreshes share one request.  Tokens stay in memory only.
 */
export function createServiceAccountTokenProvider({
  loadServiceAccount, fetch: fetchImpl = globalThis.fetch,
  now = Date.now, timeoutMs = 15_000,
} = {}) {
  if (typeof loadServiceAccount !== 'function' || typeof fetchImpl !== 'function' ||
      typeof now !== 'function') fail('invalid_token_provider_config');
  integer(timeoutMs, 1, 60_000, 'invalid_timeout');
  let cached = null;
  let pending = null;
  async function renew() {
    try {
      const sa = await loadServiceAccount();
      if (!sa || sa.type !== 'service_account' || typeof sa.client_email !== 'string' ||
          !sa.client_email || typeof sa.private_key !== 'string' || !sa.private_key) {
        fail('invalid_service_account');
      }
      const issuedAt = Math.floor(now() / 1000);
      const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
      const input = `${encode({ alg: 'RS256', typ: 'JWT' })}.${encode({
        iss: sa.client_email, scope: SCOPE, aud: OAUTH_URL,
        iat: issuedAt, exp: issuedAt + 3600,
      })}`;
      const signer = createSign('RSA-SHA256');
      signer.update(input);
      signer.end();
      const assertion = `${input}.${signer.sign(sa.private_key, 'base64url')}`;
      const response = await requestJSON(fetchImpl, OAUTH_URL, {
        method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion,
        }).toString(),
      }, timeoutMs, false, TOKEN_RESPONSE, 'invalid_token_response');
      if (typeof response.access_token !== 'string' || !response.access_token ||
          /[\r\n]/.test(response.access_token) ||
          response.token_type?.toLowerCase() !== 'bearer' ||
          !Number.isFinite(response.expires_in) || response.expires_in <= 60) {
        fail('invalid_token_response');
      }
      cached = {
        value: response.access_token,
        // Start at issuance so network latency cannot extend the token lifetime.
        refreshAt: issuedAt * 1000 + Math.min(response.expires_in, 3600) * 1000 - 60_000,
      };
      return cached.value;
    } catch (error) {
      if (error instanceof OperatorSheetsError) throw error;
      fail('service_account_load_or_sign_failed');
    }
  }
  return async function token() {
    if (cached && now() < cached.refreshAt) return cached.value;
    if (!pending) pending = renew().finally(() => { pending = null; });
    return pending;
  };
}

/**
 * Sole-writer transport, not an idempotent exporter.  No method retries writes.
 * Caller owns leases, stable record/event IDs, deduplication and reconciliation.
 */
export class OperatorSheetsGoogle {
  #spreadsheetId;
  #sheetIds;
  #tables;
  #token;
  #fetch;
  #timeoutMs;

  constructor({ spreadsheetId, sheetIds, token, fetch: fetchImpl = globalThis.fetch,
    timeoutMs = 15_000, tables = null } = {}) {
    if (typeof spreadsheetId !== 'string' || !/^[A-Za-z0-9_-]+$/.test(spreadsheetId) ||
        typeof token !== 'function' || typeof fetchImpl !== 'function') fail('invalid_config');
    integer(timeoutMs, 1, 60_000, 'invalid_timeout');
    this.#tables = normalizeTables(tables);
    this.#sheetIds = Object.freeze(Object.fromEntries(Object.keys(TABLES).map(table => [
      table, integer(sheetIds?.[table], 0, 2_147_483_647, 'invalid_sheet_id'),
    ])));
    if (this.#sheetIds.sessions === this.#sheetIds.events) fail('duplicate_sheet_id');
    this.#spreadsheetId = spreadsheetId;
    this.#token = token;
    this.#fetch = fetchImpl;
    this.#timeoutMs = timeoutMs;
  }

  #definition(table) {
    assertKnownTable(table);
    return this.#tables[table];
  }

  #rangeFor(table, coordinates) {
    return `'${this.#definition(table).title}'!${coordinates}`;
  }

  async #request(path, { method = 'GET', body, schema = null } = {}) {
    let accessToken;
    try { accessToken = await this.#token(); }
    catch (error) {
      // An OperatorSheetsError already carries its status and retry metadata;  keep them.
      if (error instanceof OperatorSheetsError) throw error;
      fail('token_unavailable');
    }
    if (typeof accessToken !== 'string' || !accessToken || /[\r\n]/.test(accessToken)) {
      fail('invalid_access_token');
    }
    const url = `https://sheets.googleapis.com/v4/spreadsheets/${this.#spreadsheetId}${path}`;
    return requestJSON(this.#fetch, url, {
      method, headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, this.#timeoutMs, method !== 'GET', schema);
  }

  async #values(table, coordinates) {
    return this.#request(`/values/${encodeURIComponent(this.#rangeFor(table, coordinates))}` +
      '?majorDimension=ROWS&valueRenderOption=UNFORMATTED_VALUE', { schema: VALUES_RESPONSE });
  }

  /** Check immutable tab IDs, exact ordered headers, and absence of extra headers. */
  async validateHeaders() {
    await this.inspect();
    return true;
  }

  /** Physical grid bounds for complete ID reconciliation, including blank pages. */
  async inspect() {
    const metadata = await this.#request('?fields=sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))', { schema: METADATA_RESPONSE });
    if (!Array.isArray(metadata.sheets)) fail('invalid_metadata');
    const bounds = {};
    for (const [table, definition] of Object.entries(this.#tables)) {
      const matches = metadata.sheets.filter(sheet => sheet?.properties?.sheetId === this.#sheetIds[table]);
      if (matches.length !== 1 || matches[0].properties.title !== definition.title) {
        fail('sheet_identity_mismatch');
      }
      const width = integer(matches[0].properties.gridProperties?.columnCount,
        definition.headers.length, MAX_COLUMNS, 'invalid_grid_width');
      const height = integer(matches[0].properties.gridProperties?.rowCount,
        1, MAX_ROW_NUMBER, 'invalid_grid_height');
      const body = await this.#values(table, `A1:${columnName(width)}1`);
      const rows = body.values ?? [];
      if (!Array.isArray(rows) || rows.length !== 1 || !Array.isArray(rows[0])) fail('header_mismatch');
      const actual = [...rows[0]];
      while (actual.at(-1) === '') actual.pop();
      if (actual.length !== definition.headers.length ||
          actual.some((value, index) => value !== definition.headers[index])) fail('header_mismatch');
      bounds[table] = { rowCount: height, columnCount: width };
    }
    return bounds;
  }

  /** Row numbers are physical, one-based Sheet rows; the first data row is 2. */
  async readRows(table, { startRow = 2, limit = MAX_ROWS, rowCount = null } = {}) {
    const definition = this.#definition(table);
    integer(startRow, 2, MAX_ROW_NUMBER, 'invalid_start_row');
    integer(limit, 1, MAX_ROWS, 'invalid_read_limit');
    if (rowCount !== null) integer(rowCount, 1, MAX_ROW_NUMBER, 'invalid_row_count');
    // A range past the tab's grid is a non-retryable HTTP 400:  clamp to the known bounds
    // from inspect(), and answer an empty page when the start row is beyond the grid.
    if (rowCount !== null && startRow > rowCount) return [];
    const endRow = rowCount === null ? startRow + limit - 1 : Math.min(startRow + limit - 1, rowCount);
    integer(endRow, 2, MAX_ROW_NUMBER, 'invalid_end_row');
    const body = await this.#values(table,
      `A${startRow}:${columnName(definition.headers.length)}${endRow}`);
    const rows = body.values ?? [];
    if (!Array.isArray(rows) || rows.length > limit || rows.some(row =>
      !Array.isArray(row) || row.length > definition.headers.length ||
      row.some(cell => !['string', 'number', 'boolean'].includes(typeof cell)))) fail('invalid_rows_response');
    return rows.map((cells, index) => ({
      rowNumber: startRow + index,
      cells: Array.from({ length: definition.headers.length }, (_, i) => String(cells[i] ?? '')),
    }));
  }

  #row(table, row) {
    const definition = this.#definition(table);
    if (!Array.isArray(row) || row.length !== definition.headers.length ||
        Array.from(row).some(cell => typeof cell !== 'string' || cell.length > 50_000) ||
        !row[0]) fail('invalid_row');
    // Caller-controlled cell text goes to an external service:  refuse credential-shaped text
    // before any request.  The refusal names the field, never the value.
    row.forEach((cell, index) => {
      if (scanCellText(cell) !== null) {
        const error = new OperatorSheetsError('credential_in_cell');
        error.field = definition.headers[index];
        throw error;
      }
    });
    return [...row];
  }

  async #write(table, row, rowNumber = null, { validate = true } = {}) {
    const values = this.#row(table, row);
    const endColumn = columnName(values.length);
    const append = rowNumber === null;
    if (!append) integer(rowNumber, 2, MAX_ROW_NUMBER, 'invalid_row_number');
    // Revalidate each mutation unless the caller already validated this batch (drainOutbox
    // inspects once per drain); never create, clear or repair a tab automatically.
    if (validate) await this.validateHeaders();
    const range = this.#rangeFor(table, append ? `A1:${endColumn}` : `A${rowNumber}:${endColumn}${rowNumber}`);
    const result = await this.#request(`/values/${encodeURIComponent(range)}${append ? ':append' : ''}` +
      `?valueInputOption=RAW${append ? '&insertDataOption=INSERT_ROWS' : ''}&includeValuesInResponse=false`, {
      method: append ? 'POST' : 'PUT', body: { majorDimension: 'ROWS', values: [values] },
      schema: WRITE_RESPONSE,
    });
    const updates = append ? result.updates : result;
    const parsed = parseUpdatedRange(updates?.updatedRange, this.#definition(table).title);
    const confirmedRow = parsed ? parsed.row : 0;
    if (result.spreadsheetId !== this.#spreadsheetId || !parsed ||
        confirmedRow < 2 || !Number.isSafeInteger(confirmedRow) ||
        confirmedRow > MAX_ROW_NUMBER || parsed.endColumn !== endColumn ||
        parsed.endRow !== confirmedRow || (!append && confirmedRow !== rowNumber) ||
        updates.updatedRows !== 1 || updates.updatedColumns !== values.length ||
        updates.updatedCells !== values.length) fail('write_confirmation_mismatch', { uncertain: true });
    return { rowNumber: confirmedRow };
  }

  appendSession(row, options) { return this.#write('sessions', row, null, options); }
  updateSessionRow(rowNumber, row, options) { return this.#write('sessions', row, rowNumber, options); }
  appendEvent(row, options) { return this.#write('events', row, null, options); }
}
