# Apple Notes Through the Operator

AFC-486, Thu, Oct 22, 2026.  This replaces the Zulip `#notes-relay` transport of AFC-331 (per-call-identity.md section 5, retired the same day).

Owner direction (quoted):  "I don't want notes going from Zulip to Apple notes."  "I want Simple-Tools MCP to have tools comparable to Simple-Notes included."  "I want reading and updating abilities because there are handful of static notes like background Mac processes."  "the server should have local tool simple notes and both server and Mac local tool work the same since I'll have the same skills and instructions for agents both places."  "Need access to all since sometimes I have an agent fix formatting errors that happened across many agents' notes."  "keep a log of actions by that script too so if needed we can analyze it."

## One Tool Set, Three Surfaces

| Verb | `simple-notes` | MCP tool (stdio and ST-MCP) | What it does |
| --- | --- | --- | --- |
| create | `simple-notes create TITLE` | `notes_create` | A new note.  The title must be `[TEAM_KEY, Agent] Topic` with the caller's own agent name. |
| update | `simple-notes update TITLE` | `notes_update` | Replace a note's body.  Any Coding note.  A note whose title does not carry the caller's name is never created by an update. |
| pin, unpin | `simple-notes pin TITLE` | `notes_pin`, `notes_unpin` | Any Coding note. |
| read | `simple-notes read TITLE` | `notes_read` | One note's content, as `html` (ready to send back with `update --html`), `text` or `raw` (exactly what Notes stores).  A line that looks like a credential reads as `[withheld: secret-shaped]`. |
| list | `simple-notes list` | `notes_list` | The Coding folder's notes, newest first:  title, id, created, modified.  `query` filters titles. |
| status | `simple-notes status ID` | `notes_status` | Where a queued request stands. |
| log | `simple-notes log` | `notes_log` | The action log of `apple-notes-coding.sh`. |

The verbs, their options and the tool arguments are one contract:  `scripts/agent_sync/mcp/notes-tools.json`.  `scripts/agent_sync/tests/test_notes_parity.py` fails when `simple-notes`, the stdio server or the hosted Worker drifts from it.

The output is the same everywhere:  a write answers `status: done` with the `note_id`, a read answers the note, and only a request still waiting when the caller's wait ran out answers `queued` or `running` with its `request_id`.

## Where a Request Runs

- **On the Mac** (macOS, `osascript` and the script found):  `simple-notes`, the stdio MCP and the apple-notes skill run `apple-notes-coding.sh` directly, as before.
- **Everywhere else** (the server's CLIs and stdio MCP, the hosted ST-MCP Worker for cloud seats):  the request goes to the Operator's notes queue over signed HTTPS (below), and a seatless runner on the Mac claims it, runs the same script and reports the result.  The caller waits for that result, polling once a second or so:  `simple-notes` up to 300 seconds (`--wait SECONDS`, `--no-wait`);  the stdio MCP 25 seconds by default, which `wait_seconds` may raise to 90 (it answers one call at a time, so a long default would hold up the next);  ST-MCP up to 25 seconds.  No Zulip message is involved at any point.

The script is the one place formatting is enforced (the title row, the date row, the Session row, sentence gaps, fleet dates), so a note reads the same whoever wrote it and from wherever.

## Who May Do What

- **create:**  the title is `[TEAM_KEY, Agent] Topic` and the Agent is the caller's own canonical name (notes.title_problem).  A create or an update carries a session id unless the caller is a single-thread identity (session_ids.is_exempt).
- **Calling from the server:**  `simple-notes` acts as `AGENT_SEAT` (or `--identity`), over signed HTTPS as the SERVER principal.  That seat must be in `[notes_queue] server_seats` and pass the same title and session rules, so `AGENT_SEAT=CLAUDE simple-notes create "[AFC, Claude] Topic"` works from any server shell holding the key.  `AGENT_SEAT=OPERATOR` does not:  OPERATOR is in no seat list, the title check knows it only as `OPERATOR` (never the display form `Operator`), and it is not session-exempt.
- **update, pin, unpin, read:**  any note in the Coding folder (owner, Oct 22).  An update whose title does not carry the caller's name runs with `--existing-only`, so a missing note answers `not_found` instead of a new untagged note.
- **list:**  the whole Coding folder.
- Every title is at most 200 characters, holds no double quote, backslash or control character, and does not start with `-`.
- Write text (title, body, summary, pr) gets the fleet secret scan before it leaves the caller, and so do a list query and a log's title and seat filters.
- A read gets the same scan on the Mac, before its result leaves, and again on the Operator before it is stored:  each line it flags is replaced by `[withheld: secret-shaped]` (a `<div>` line keeps its `<div>`), a private key block goes whole (its BEGIN line through its END line, or to the end of the body), and `note.withheld` counts the lines, so a credential pasted into a note never reaches the Operator, a cloud seat or a model.  A title the scan flags reads the same way in a list and in the log, and a read cut to its 1.5 MB cap ends on a whole line, so a marker is never split.  A write whose body holds that marker is refused, so a read, edited and written back, never erases the real line;  edit such a note on the Mac.

## The Wire Protocol

Contract `agent-sync-notes/1`, on the Operator's API server (the listener API, `listener_api.py`, port 8788), under `/notes/v1/`.  The Worker reaches it at `LISTENER_API_URL`;  Python clients and the runner read the fleet knob `OPERATOR_API_URL`.  Both are the same https origin.  When the knob reads empty, the Python side asks `fleet-secrets get OPERATOR_API_URL --path / --project ai-fleet-coordinator` for the same key, the way it reads its principal's key.  This is for the server:  its agent shells carry no Infisical automation pair, so fleet_knobs there has only its last-known-good copy, which lapses after 48 hours, while `fleet-secrets` logs in for itself.

**Principals.**  Every request is signed by one of three principals, each with its own key in Infisical (project AI Fleet Coordinator, `prod`, folder `/operator`, at least 32 characters):

| Principal | Key | Held by | May |
| --- | --- | --- | --- |
| `WORKER` | `OPERATOR_NOTES_WORKER_KEY` (the Worker secret `OPERATOR_NOTES_KEY`) | the ST-MCP Worker | enqueue, read status and the log for the seats in `[notes_queue] worker_seats` |
| `SERVER` | `OPERATOR_NOTES_SERVER_KEY` | the server's CLIs and stdio MCP (`fleet-secrets get`) | the same, for `[notes_queue] server_seats` |
| `RUNNER` | `OPERATOR_NOTES_RUNNER_KEY` | the Mac runner | claim requests, report results, send log lines;  nothing else |

Each principal works on its own, on purpose:  the Operator serves the principals whose key is set and answers 404 to one without a key, so one missing key never takes the other two down.  It says so at start:  `notes-queue-started` names the principals it serves and warns when the runner has none (then nothing can claim a request), and with no usable key at all the queue stays off (`notes-queue-off`).  The doctor's `notes:queue` row shows the queue's health, and `notes:runner` shows whether the Mac runner is polling.

The three keys separate what each caller may do over the network:  a leaked Worker key cannot claim a request, and a leaked runner key cannot enqueue one.  They are not a boundary between processes on one host.  Today the server's and the Mac's Infisical identities can each read all of `/operator`, so any process on either host that can run `fleet-secrets` can take any of the three keys.

**Signing.**  Three headers:  `X-Agent-Sync-Principal`, `X-Agent-Sync-Timestamp` (unix seconds) and `X-Agent-Sync-Signature`, the lowercase hex HMAC-SHA256, keyed with the principal's key, of

    agent-sync-notes/1\n<METHOD>\n<path>\n<raw query>\n<timestamp>\n<principal>\n<sha256 hex of the body bytes>

good for 300 seconds either side of the Operator's clock.  Bodies are UTF-8 JSON, at most 4 MiB.  An unknown principal, a principal with no key, an unknown path:  404.  A bad or stale signature:  401.  A principal on a path it may not use, or a seat it may not act for:  403.

**The request object** (`v` 2), checked by `notes.check_request` on the caller, on the Operator and again on the runner:

```json
{"v": 2, "action": "update", "request_id": "nr0123456789abcdef0123", "identity": "CLAUDE",
 "title": "⭐️ Background Jobs Master List", "markdown": "...", "flags": {"html": true},
 "session": "<session-id>"}
```

`action` is one of create, update, pin, unpin, read, list.  `title` is required except for list;  `markdown` and `flags` (pin, notify, needs_owner, summary, pr, html) only for create and update;  `format` (html, text, raw) only for read;  `query` (at most 100 characters) and `limit` (1 to 500, default 100) only for list.

**Endpoints.**

| Method and path | Principal | Body | Answer |
| --- | --- | --- | --- |
| `POST /notes/v1/requests` | WORKER, SERVER | `{"seat": S, "request": {...}}` | 202 `queued`;  200 the current record when the same request was sent before;  409 when the request_id was used for a different request;  400 a rule the request breaks;  503 the queue is full |
| `GET /notes/v1/requests/<SEAT>/<request_id>` | WORKER, SERVER | none | the record |
| `GET /notes/v1/log?since_hours=&limit=&by_seat=&action=&title=` | WORKER, SERVER | none | `{"entries": [...]}`, newest first |
| `POST /notes/v1/runner/claim` | RUNNER | `{"wait": 0..25}` | 200 `{"claim": ..., "seat": ..., "request": {...}}`, the oldest queued request, held for up to `wait` seconds when none is queued;  204 none |
| `POST /notes/v1/runner/result` | RUNNER | `{"seat", "request_id", "claim", "result": {...}}` or `{..., "error": {"code", "message"}}` | 200 the record |
| `POST /notes/v1/runner/log` | RUNNER | `{"lines": [ ... ]}` (at most 200) | 200 `{"accepted": n, "duplicates": n}` |

**A record** is `{"contract", "seat", "request_id", "action", "title", "principal", "state", "queued_at", "claimed_at", "finished_at", "result" | "error"}`.  `state` is one of:

- `queued`, `running`:  waiting for the runner, or claimed by it;
- `done`:  the result is in `result` (for a read, `result.note`;  for a list, `result.notes`);
- `refused`:  `error` names the code (invalid_argument, not_found, unavailable, internal) and a message that never holds the body;
- `unknown`:  a write whose outcome cannot be known (the runner's lease ran out, the script timed out, or a create or update failed in a way that does not prove nothing was written, such as an exit the script does not plan for).  It is never retried:  check the note, then send it again with a new request_id;
- `expired`:  nobody claimed it within 24 hours, so nothing was written.  Both clients answer `unavailable` and say to send it again with a new `request_id`:  the same id answers this record again for 14 days.

A claimed write that does not report within the lease (1,500 seconds, above the script's 1,200-second cap) becomes `unknown`;  if the runner later reports a definite outcome for that same claim (the Mac slept, say), the record takes it.  Only a result, or an error that proves nothing was written (invalid_argument, not_found, session_required or unavailable), is definite;  an internal error or one with no code leaves the write unknown.  A claimed read or list goes back to the queue (at most three tries), because reading twice changes nothing:  when its lease runs out, and when the runner reports it `unavailable` (its script timed out, say);  the third try's answer is final.  A caller whose POST got no answer (a network failure, a timeout, a 5xx, a 408, or a status the queue never sends) answers `outcome_unknown` with the `request_id`, never retryable:  the request may or may not be queued, so check it with `notes_status`, or send it again with the same `request_id`, never a new one.  The queue's own 503 (full, or not running, with its JSON error) is a definite refusal instead:  nothing was queued, so it answers `unavailable`, and the same `request_id` may be sent again later.  A caller whose poll fails stops waiting and answers `queued` with the id.  A write's body is dropped from the record as soon as it finishes, and a read's or list's result after one hour.  Finished records are kept 14 days;  the queue holds at most 1,000 waiting requests.

## The Mac Runner

`simple-notes runner` runs resident under the LaunchAgent `com.jay.operator-notes-runner` (KeepAlive).  It acts as no seat and holds no Zulip key:  its only secret is `OPERATOR_NOTES_RUNNER_KEY`, read from its environment variable or, when that is unset, with `fleet-secrets get`, and kept in memory;  a key it cannot find is reported by that name.  Each loop it claims one request (waiting up to 25 seconds), checks it again with the same rules, runs the script, and reports the result.  Before it runs a request it records it in `~/.local/state/operator/notes-runner.json` (mode 600);  after a crash, a recorded write with no outcome is reported `unknown`, never run again, and a recorded outcome that did not reach the Operator is sent again.  A read or list is recorded without its result (the note's text never goes to that file) and is run again after a crash.  A result the Operator refuses for good (a note too large to send back) is replaced by an error report, so the runner never stalls on it.  The runner lets a report go only on the queue's own final answer (the record, "no such request", another claim, or a refusal it always gives);  any other answer, a proxy's page or an empty body included, keeps it for the next pass.  An unexpected failure is reported by its type name (a create's or update's as `outcome_unknown`), and nothing ends the loop:  it backs off and goes on, and shipping the log has its own boundary, so a line it cannot send never holds up a claim.  A write the runner makes for another host carries a last line naming the requester:  `Written for <SEAT> through the Operator on <date> (request <id>).`  An update replaces any older such line, and the old relay footer, instead of adding one.

## The Action Log

`apple-notes-coding.sh` appends one JSON line for every run, whoever called it, to `~/Library/Logs/fleet/apple-notes-actions.jsonl` (mode 600, `APPLE_NOTES_ACTION_LOG` overrides;  rotated at 5 MB, five files kept).  A line holds `id`, `ts`, `host`, `mode`, `title`, `note_id`, `exit`, `outcome`, `duration_ms`, `seat`, `session`, `caller` (script, cli, mcp, runner), `request_id`, `requester`, `input_bytes`, `body_bytes`, `body_sha256`, `reformatted` (dates and sentence gaps the script rewrote), `pin` and the flags.  It never holds a note's text.  Each field keeps only its own type (a count a whole number, the rest text, a flag or null), text that UTF-8 cannot hold is replaced, and a secret-shaped title reads as the withheld marker.  The runner sends new lines to the Operator, which keeps them (`notes-actions.jsonl`, rotated at 5 MB, ten copies kept), so `simple-notes log` and `notes_log` read the same entries on the Mac (the file itself) and everywhere else (the Operator's copy).  The script starts each line on a line of its own, so a line an earlier write left torn (a full disk, a crash) never swallows the next one, and a line that is not an entry is passed over.  The runner's cursor names a file by its inode and two places in it:  just past the last whole line it read, and just past the last entry, whose `id` it keeps.  The runner lists the five copies and the current log under the script's own log lock, so a rotation never lands in the middle of the list, and it follows the file through rotations:  while the file is still kept and the line before the second place is still that entry, it goes on from the first place, then sends each newer file whole, oldest first.  When it cannot trust that place (the file rotated out of reach while the runner was off, or was replaced or cut short), it looks the `id` up in every kept file and goes on just after it.  When no kept file holds the `id`, or there is no cursor yet, it sends every kept line again, oldest first, so no line is ever skipped.  The Operator drops each line it already holds by its `id`, and it remembers 100,000 ids, more than the Mac's six files can hold.  A kept file the runner cannot read stops the pass with an error the doctor shows, so nothing behind it is skipped, and the doctor counts unsent bytes from the file the cursor names.

## Operator Configuration

```toml
[listener_api]
enabled = true
bind = "0.0.0.0"
port = 8788

[notes_queue]
enabled = true
worker_seats = ["JET", "GROK-WEB", "ECHO", "WISE", "CURSOR", "CLAUDE"]
server_seats = ["CLAUDE", "CODEX"]
```

The keys reach the container from Infisical at start (`server/infisical_env.py` reads `/operator` for exactly the three names).  The public route sends `/listener/v1/` and `/notes/v1/` to port 8788 and nothing else.
