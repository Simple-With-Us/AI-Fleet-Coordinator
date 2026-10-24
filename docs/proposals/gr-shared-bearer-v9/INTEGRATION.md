# GR-MCP shared-bearer integration - both services implemented and tested (v9)

Owner direction this implements (Jay's own WhatsApp messages, Oct 23 2026 -
re-verifiable in the observation record, not established by this document):
- 1:29:40 PM "we need all apps to be in the admin panel actually and bearer token
  creation for git-relay mcp" -> all apps in the admin panel IS the original ask.
- 1:29:53 PM agent recommendation accepted: one credential PER IDENTITY, not one
  shared across agents.
- 1:34:20 PM "the shared one is shared between ST-MCP and GR-MCP but unique per
  identity" -> the same credential is honored by BOTH services.
- The 1:39:51 "same as Jet's" belongs to the Codex-arming callback thread
  (PR #890 records it as such), not to this design.

## Contents

git-relay (Python, stdlib only):
- git_relay/shared_bearer.py  NEW - resolves a presented bearer to its identity via
  the Worker's signed verify route.  Both admin-minted families:  gbb_ (GB personas)
  and sbt_ (seats).  Hash-only transport:  the raw bearer never leaves the relay;
  the signed body carries identity + SHA-256 hex + sent_at (wake signature scheme).
  FAIL CLOSED everywhere;  no token or hash in any error string.  Bounded cache
  (1024, oldest evicted);  positive answers not cached by default so a revoke or
  pause in /admin takes effect on the next call;  refusals cache 30s.
- gr-policy.py.diff  Seat config gains "auth": "shared" (+ optional "identity"
  alias).  Config load REFUSES two seats claiming the same verified identity.
  Static-token seats unchanged.  Q7 (app_slug == seat) untouched.
- gr-server.py.diff  from_env builds the verifier when GIT_RELAY_VERIFY_URL is set;
  the route key (default GR_BEARER_VERIFY_KEY) reads through fleet-secrets only;
  missing/short key = startup error.
- test_shared_bearer.py  NEW - 20 tests.

ST-MCP Worker:
- gr-verify.js  NEW - POST /internal/gr-bearer-verify.  Wake signing scheme (HMAC
  over raw body, 5-min age / 60s skew;  read-only idempotent route).  Checks the
  hash against the identity's own SeatGate (constant-time) plus the paused flag;
  fixed refusal codes;  only an expired-but-matching bearer audits;  body/hash
  never echoed or logged.  Registered in policy.js classifyPath as "gr_verify".
- worker-gb-bearer.js.diff  Seat bearer family sbt_<seat>_<43 base64url>
  (seat lower-cased):  newSeatBearer / parseSbtBearer / sbtBearerFromRequest,
  keyed off KNOWN_SEATS so seats added by #889 (JET-CHAT) and #890 (CODEX) become
  mintable automatically when those merge.
- worker-seat-state.js.diff  The bearer row gains mcp_accept.  Minted rows carry
  it, the mint audit records it, gbBearerCheck and status return it;  pre-field
  rows read back false, never undefined-accepts.  Adds gbBearerMintChecked:
  single-use mint forms.  Every rendered mint form carries a mint_id (12 hex ms
  timestamp + 20 random hex) and a one-hour validity (60s future skew).  The
  mint id is checked, consumed, and written atomically with the bearer row and
  audit entry in ONE Durable Object transaction, so concurrent submissions of
  the same form mint exactly once and a mid-mint failure rolls back cleanly
  (no bearer row, no consumed marker; the form stays usable and the retry
  succeeds).  A stale (older than one hour, or future-dated past the 60s skew)
  or already-consumed form refuses WITHOUT mutation, and a fresh form always
  mints deliberately.  Replay rejection spans the form's whole validity
  window: consumed records are evicted only once they are past their window,
  so no still-valid form can ever mint twice.  The record is fail-closed
  bounded (64 per identity): with 64 still-valid forms outstanding, a further
  fresh mint refuses as "busy" rather than evicting the oldest tombstone, and
  minting resumes as windows expire.  An evicted form then refuses as stale,
  never mints.
- worker-seat-gate.js.diff  SeatGate wrapper for gbBearerMintChecked.
- worker-forms.js.diff  Admin identity regex widened to seat-shaped names;
  optional mcp_accept checkbox field (literal "1");  optional mint_id field
  (32 hex).  Other admin actions (arm, revoke, ...) are untouched.
- worker-pages.js.diff  The bearer section is generalized from Grok Bots to all
  identities (personas + seats).  The preamble is acceptance-accurate:  every
  bearer is honored by GR-MCP;  a persona's bearer is also accepted on ST-MCP's
  /mcp (its only credential);  a seat's bearer reaches ST-MCP's /mcp ONLY when
  minted with the box ticked, otherwise ST-MCP refuses it and GR-MCP still
  honors it.  A seat mint form carries the mcp_accept checkbox with the
  disclosure wording;  when the current bearer is /mcp-accepted, a block-level
  warning above the actions states BEFORE the commit that an unticked rotation
  drops that access (wide and narrow viewport qualified).  The status column
  shows "also accepted on /mcp" or "GR-MCP only".  The reveal page is shown
  exactly once and says so, including that a resubmit is refused and the bearer
  stays as it is;  its send line branches on what the minted bearer can actually
  do (persona/ticked seat:  ST-MCP /mcp + GR-MCP;  unticked seat:  GR-MCP only,
  with the refusal stated).  The Nothing-Was-Minted refusal page shows no token
  and states the current bearer is unchanged.  Visible product name follows the
  canonical rename map (fleet-tool-renames):  ST-MCP Admin.
- worker-index.js.diff  serveSbtBearerMcp:  parses an sbt_ bearer, requires the
  seat hosted with its key configured, checks the SeatGate bearer row, REFUSES
  unless mcp_accept is true and the seat is not paused, then serves /mcp as that
  seat only (createIdentity: null - no acting as another identity, even shared
  group members).  Generic 401 for every refusal.  /mcp dispatch detects sbt_
  alongside gbb_;  gr_verify dispatch line next to the wake route.  Admin rows
  assembly covers GB_IDENTITIES + KNOWN_SEATS;  the mint handler validates
  kind-specific prerequisites, then calls gbBearerMintChecked:  a refused form
  renders the Nothing-Was-Minted page and changes nothing;  a fresh deliberate
  form mints and renders the reveal.  The mint response is the only page that
  ever carries the token (no redirect, no log line, nothing stored).
- worker-policy.js.diff  classifyPath case for the verify route literal.
- Tests:  gr-verify.test.mjs NEW (6), sbt-bearer.test.mjs NEW (5),
  worker-gb-bearer.test.mjs.diff (updated assertions + new:  reveal send-line
  branches per actual acceptance, once-only/single-use copy, refusal page,
  checked-mint consumed/stale/future refusals with bearer-state-unchanged
  assertions, 64+1 whole-window replay rejection, fail-closed busy refusal,
  concurrent-submission single mint, mid-mint rollback with retry), worker-integration.workerd.mjs.diff
  (mint is a single 200 with the token;  seat-bearer step asserts the unticked
  reveal says /mcp refuses it;  replay step:  the exact resubmit and a stale
  form both render Nothing-Was-Minted with no token while the live bearer keeps
  answering, then a fresh deliberate form still rotates on purpose;  the
  rotation-consequence copy renders while a /mcp-accepted bearer exists).

## Verification actually run (clean clone, main head f4f89972 + this bundle)

- Worker unit suite:  620 tests, 612 pass, 0 fail (8 pre-existing environment
  skips:  python-parity cases;  identical skip set before the patch).  Current
  main baseline before the patch:  603 tests, 595 pass, 0 fail.
- workerd integration suite (test/integration.workerd.mjs):  FULL PASS,
  including the persona-bearer, seat-bearer, wake-only-seat-bearer,
  replay-refusal and rotation-consequence steps.
- Python suite:  136 tests, OK (113 upstream + 23 shared-bearer).
- Wake-only split composition (#889):  the seat-bearer integration step runs
  its full round-trip as JET-CHAT (the normal-access half of the Jet split);
  a dedicated step pins that a JET (events-only) bearer minted WITH the /mcp
  acceptance still gets an empty tools/list and a protocol error on every
  tools/call - the acceptance records consent-flow replacement, never a
  capability grant, and never widens a wake-only seat's surface.
- Rendered visual inspection (headless Chrome over pages captured from the
  real page code):  admin bearer section with persona + seat rows (accepted,
  GR-MCP only, none), seat reveal ticked and unticked, both refusal pages.
  All tokens shown are ephemeral test values.
- #890 differs from main only in src/config.js, which this bundle does not
  touch - no conflict surface.
- Reconstruction note (v9):  v8's bundle bytes were lost in a workspace reset.
  This bundle was reconstructed from the original build's own recorded tool
  calls onto the 3836446 base, proven behaviorally identical to v8's recorded
  as-shipped results on that base (603/595/0 + integration pass + 136 OK),
  then ported to f4f89972.  It drops three spurious EOF blank-line hunks the
  v8 diffs carried in policy.py, server.py and seat-state.js;  shared_bearer.py
  may differ from v8's by one comment/blank line (66 bytes).  Treat as new
  unreviewed work, not a byte-identical v8.

## Security semantics

- The bearer hash is credential-equivalent for the verify route:  the route
  requires its own HMAC caller key, a freshness window, and binds the answer to
  the asked identity (the relay refuses an answer naming a different identity).
  The hash is checked constant-time inside the SeatGate against THAT identity's
  row only.  Hashes are never logged or echoed.
- Mint operation semantics (single-use forms):  no raw bearer is ever stored
  (SHA-256 hash only, reveal-once), and every mint form carries a single-use
  id that is consumed atomically with the mint in one Durable Object
  transaction (tested under concurrency and under a mid-mint fault with
  rollback: no partial state, the form stays usable).  Stale or consumed
  forms refuse without mutation; a fresh deliberate mint always succeeds.
  Replay rejection covers the form's entire one-hour validity window: consumed
  tombstones evict only after expiry, and the bounded record (64 per identity)
  is fail-closed - with the window full, further fresh mints refuse as "busy"
  instead of evicting a still-valid tombstone.  No residual replay/rotation
  path: a form can never mint twice while valid, and an evicted form refuses
  as stale.
- mcp_accept records a choice;  it does not create one.  For an OAuth-capable
  seat, the bearer bypasses the consent flow only when Jay actually mints it
  with the checkbox ticked, seeing the disclosure.  An existing row, a default,
  or this code's existence authorizes nothing.  GR-MCP acceptance is GR's own
  policy decision (a shared-auth seat entry) because GR has no OAuth path to
  bypass.

## Enrollment grants (original grants are authority;  history is leads only)

The interim path (AFC-460) carries no written per-seat repo/action restriction,
and recent PR history or action frequency is NOT a grant.  Authority for each
seat's enrollment matrix derives from Jay's original grants and task scope;  PR
history is only a lead to what those covered.  Anything the originals do not
clearly cover goes to the cutover matrix review with the derivation shown - the
cutover decision itself stays held for Jay.  No org-wide wildcard:  the App
installation being org-wide is capability, not a per-seat grant of every repo.
  app_slug:  the seat's own swu-<seat> App;  mint_permissions = that App's live
  granted permissions, capped by MINT_PERMISSION_CEILING.

## Provisioning (owner-gated - Jay's stops, no ask until host scope verified)

- One new HMAC key GR_BEARER_VERIFY_KEY (openssl rand -hex 32 shape, like the
  wake keys):  Infisical entry (his click) + Worker secret on the next hand
  deploy.  The only new secret;  existing keys cannot be reused without breaking
  the fleet's key-separation pattern.
- GIT_RELAY_VERIFY_URL = https://agents.simplewithus.com/internal/gr-bearer-verify
  on the relay host.

## Not done here

Nothing is deployed, no key is created, no seat is enrolled, no admin change is
live.  This bundle is tested code for the owning writer's lane, pending
independent review.