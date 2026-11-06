# Operator Sheets Adapter

Source contribution for AFC-428, supporting AFC-412.  This module provides Google Sheets transport for the durable exporter.  Importing it performs no network, credential, filesystem or background work.

## Construction

```js
import {
  createServiceAccountTokenProvider, OperatorSheetsGoogle,
  OperatorSheetsError, SESSION_HEADERS, EVENT_HEADERS,
} from './operator_sheets_google.mjs';

const getAccessToken = createServiceAccountTokenProvider({
  loadServiceAccount: async () => authorizedRuntimeServiceAccount(),
});
const sheets = new OperatorSheetsGoogle({
  spreadsheetId: configuredWorkbookId,
  sheetIds: { sessions: configuredSessionsTabId, events: configuredEventsTabId },
  token: getAccessToken,
});
```

The runtime supplies approved service-account credentials and destination configuration through its existing Infisical integration.  The adapter does not discover a credential store, read environment variables, provision an identity or persist tokens.  An existing authorized `sa.mjs` token function can instead be injected directly.  The built-in provider exchanges a signed service-account assertion with Google's fixed token endpoint, shares concurrent refresh requests and renews its in-memory token one minute early.  It performs no user impersonation.

Node 20 or newer is required.  Both constructors accept an injected `fetch` transport and a bounded `timeoutMs` (default 15 seconds, maximum 60 seconds).  The token provider also accepts `now`, in milliseconds, for clock injection.

## Exporter Interface

| Method | Result / Contract |
| --- | --- |
| `inspect()` | Validates both tab IDs, titles and headers, then returns `{sessions: {rowCount, columnCount}, events: {rowCount, columnCount}}` |
| `validateHeaders()` | Performs the same validation and returns `true` |
| `readRows(table, {startRow, limit})` | At most 500 rows, returned as `{rowNumber, cells}` entries; `table` is `sessions` or `events` |
| `appendSession(cells)` | Appends one complete Sessions row; returns `{rowNumber}` after response confirmation |
| `updateSessionRow(rowNumber, cells)` | Updates one complete Sessions data row; returns `{rowNumber}` after response confirmation |
| `appendEvent(cells)` | Appends one complete Events row; returns `{rowNumber}` after response confirmation |

Row numbers are physical, one-based Sheet row numbers.  Data starts at row 2.  All writes require a full, dense array of strings in the exported header order, including empty strings for cleared cells; column A must contain the stable record/event ID.  Values are submitted with `RAW` interpretation.  Header rows cannot be updated through this interface.  Only Sessions and Events have write methods.

Headers are the `SESSION_HEADERS` and `EVENT_HEADERS` constants.  A missing/renamed tab, wrong immutable tab ID, changed header, additional populated header or unsupported grid width fails closed.  Every mutation revalidates both tabs.  There is no automatic header repair, tab creation, cell deletion or write to Operator Reference.

## Reconciliation And Concurrency

The registry remains authoritative.  The exporter must hold its exclusive writer lease, reconcile immutable Record ID/Event ID values, and maintain transactional outbox state.  The adapter does not allocate labels, upsert by ID, deduplicate events or authenticate registry callers.

Before reconciliation, call `inspect()` and scan every relevant grid page through `rowCount`, capping the final `readRows` limit to that boundary.  Sheets omits trailing blank cells/rows in a response.  A short or empty page does not establish that later pages are empty.  Reject duplicate IDs rather than selecting an arbitrary match.  Re-scan after any uncertain mutation before choosing to append again.  Do not retain a row-number index across manual row insertion, deletion or reordering.

Google Sheets supplies no compare-and-swap or transaction spanning header checks and values writes.  The exclusive exporter lease and an operational prohibition on concurrent tab restructuring are prerequisites.  Another program or human can otherwise change rows between reconciliation, header validation and mutation.

## Errors And Retries

`OperatorSheetsError` exposes only a fixed `code`, numeric HTTP `status` or `null`, `uncertain`, and bounded numeric `retryAfterSeconds` or `null`.  Error messages never contain response bodies, credentials, arbitrary cell contents or nested exception causes.  Caller-supplied credential and fetch implementations must likewise avoid logging sensitive inputs.

There are no automatic API retries.  The outbox owns bounded backoff and retry policy.  `uncertain: true` means a mutation may have committed: transport/response failure, an ambiguous non-success status, or a success response whose updated range/count cannot be confirmed.  Reconcile before retrying.  Authentication or header failure before mutation is not an uncertain write.  Do not mark an outbox item exported merely because no exception was logged; require a confirmed operation result or successful ID reconciliation.

Only ordinary value rows intended for the durable fleet archive belong here.  The adapter is not a secret scanner or audit allowlist.  The exporter must project approved fields and reject credentials, tokens, private keys, raw requests, transcripts and other excluded content before invoking it.  Native session IDs and labels are routing metadata, never authorization credentials.

## Delivery Status

This is a source module and integration contract.  It does not activate a worker or modify deployed configuration.  Offline tests with an injected fetch cover it (run `node --test scripts/test_operator_sheets_google.mjs`);  live writes against a real workbook are not verified.  The existing service-account sheet setup is separate from exporter activation.

## API References

- [Values append](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append)
- [Values update](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/update)
- [Service-account OAuth](https://developers.google.com/identity/protocols/oauth2/service-account)
