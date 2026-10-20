/** Google Sheets transport for the Operator outbox.  Importing performs no I/O. */
import { createSign } from 'node:crypto';

const OAUTH_URL = 'https://oauth2.googleapis.com/token';
const SCOPE = 'https://www.googleapis.com/auth/spreadsheets';
const MAX_ROWS = 500;
const MAX_COLUMNS = 256;
const MAX_ROW_NUMBER = 10_000_000;

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
function tableDefinition(table) {
  if (!Object.hasOwn(TABLES, table)) fail('invalid_table');
  return TABLES[table];
}
function rangeFor(table, coordinates) {
  return `'${tableDefinition(table).title}'!${coordinates}`;
}
function retryAfter(response) {
  const value = response.headers.get('retry-after');
  // Only numeric durations are exposed; arbitrary response header text is discarded.
  return /^\d+$/.test(value ?? '') ? Math.min(Number(value), 3600) : null;
}

async function requestJSON(fetchImpl, url, init, timeoutMs, mutation = false) {
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
    const body = await response.json();
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      fail('invalid_response', { uncertain: mutation });
    }
    return body;
  } catch (error) {
    if (error instanceof OperatorSheetsError) throw error;
    fail('transport_or_response_error', { uncertain: mutation });
  } finally {
    clearTimeout(timer);
  }
}

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
      }, timeoutMs);
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
  #token;
  #fetch;
  #timeoutMs;

  constructor({ spreadsheetId, sheetIds, token, fetch: fetchImpl = globalThis.fetch,
    timeoutMs = 15_000 } = {}) {
    if (typeof spreadsheetId !== 'string' || !/^[A-Za-z0-9_-]+$/.test(spreadsheetId) ||
        typeof token !== 'function' || typeof fetchImpl !== 'function') fail('invalid_config');
    integer(timeoutMs, 1, 60_000, 'invalid_timeout');
    this.#sheetIds = Object.freeze(Object.fromEntries(Object.keys(TABLES).map(table => [
      table, integer(sheetIds?.[table], 0, 2_147_483_647, 'invalid_sheet_id'),
    ])));
    if (this.#sheetIds.sessions === this.#sheetIds.events) fail('duplicate_sheet_id');
    this.#spreadsheetId = spreadsheetId;
    this.#token = token;
    this.#fetch = fetchImpl;
    this.#timeoutMs = timeoutMs;
  }

  async #request(path, { method = 'GET', body } = {}) {
    let accessToken;
    try { accessToken = await this.#token(); }
    catch { fail('token_unavailable'); }
    if (typeof accessToken !== 'string' || !accessToken || /[\r\n]/.test(accessToken)) {
      fail('invalid_access_token');
    }
    const url = `https://sheets.googleapis.com/v4/spreadsheets/${this.#spreadsheetId}${path}`;
    return requestJSON(this.#fetch, url, {
      method, headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, this.#timeoutMs, method !== 'GET');
  }

  async #values(table, coordinates) {
    return this.#request(`/values/${encodeURIComponent(rangeFor(table, coordinates))}` +
      '?majorDimension=ROWS&valueRenderOption=UNFORMATTED_VALUE');
  }

  /** Check immutable tab IDs, exact ordered headers, and absence of extra headers. */
  async validateHeaders() {
    await this.inspect();
    return true;
  }

  /** Physical grid bounds for complete ID reconciliation, including blank pages. */
  async inspect() {
    const metadata = await this.#request('?fields=sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))');
    if (!Array.isArray(metadata.sheets)) fail('invalid_metadata');
    const bounds = {};
    for (const [table, definition] of Object.entries(TABLES)) {
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
  async readRows(table, { startRow = 2, limit = MAX_ROWS } = {}) {
    const definition = tableDefinition(table);
    integer(startRow, 2, MAX_ROW_NUMBER, 'invalid_start_row');
    integer(limit, 1, MAX_ROWS, 'invalid_read_limit');
    integer(startRow + limit - 1, 2, MAX_ROW_NUMBER, 'invalid_end_row');
    const body = await this.#values(table,
      `A${startRow}:${columnName(definition.headers.length)}${startRow + limit - 1}`);
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
    if (!Array.isArray(row) || row.length !== tableDefinition(table).headers.length ||
        Array.from(row).some(cell => typeof cell !== 'string' || cell.length > 50_000) ||
        !row[0]) fail('invalid_row');
    return [...row];
  }

  async #write(table, row, rowNumber = null) {
    const values = this.#row(table, row);
    const endColumn = columnName(values.length);
    const append = rowNumber === null;
    if (!append) integer(rowNumber, 2, MAX_ROW_NUMBER, 'invalid_row_number');
    // Revalidate each mutation; never create, clear or repair a tab automatically.
    await this.validateHeaders();
    const range = rangeFor(table, append ? `A1:${endColumn}` : `A${rowNumber}:${endColumn}${rowNumber}`);
    const result = await this.#request(`/values/${encodeURIComponent(range)}${append ? ':append' : ''}` +
      `?valueInputOption=RAW${append ? '&insertDataOption=INSERT_ROWS' : ''}&includeValuesInResponse=false`, {
      method: append ? 'POST' : 'PUT', body: { majorDimension: 'ROWS', values: [values] },
    });
    const updates = append ? result.updates : result;
    const match = typeof updates?.updatedRange === 'string' && updates.updatedRange.match(
      /^'?Operator (Sessions|Events)'?!A([0-9]+):([A-Z]+)([0-9]+)$/);
    const confirmedRow = match ? Number(match[2]) : 0;
    if (result.spreadsheetId !== this.#spreadsheetId || !match ||
        `Operator ${match[1]}` !== tableDefinition(table).title ||
        confirmedRow < 2 || !Number.isSafeInteger(confirmedRow) ||
        confirmedRow > MAX_ROW_NUMBER || match[3] !== endColumn ||
        Number(match[4]) !== confirmedRow || (!append && confirmedRow !== rowNumber) ||
        updates.updatedRows !== 1 || updates.updatedColumns !== values.length ||
        updates.updatedCells !== values.length) fail('write_confirmation_mismatch', { uncertain: true });
    return { rowNumber: confirmedRow };
  }

  appendSession(row) { return this.#write('sessions', row); }
  updateSessionRow(rowNumber, row) { return this.#write('sessions', row, rowNumber); }
  appendEvent(row) { return this.#write('events', row); }
}
