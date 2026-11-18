# Exact check-run pages and metadata reads

Two default-off own-App checks:read commands address fleet review/merge-gate
inspection.  `wf_check_runs_page` pins a commit SHA and exact check name, requests
filter=all and one page1-1000 of at most50runs.  `wf_check_run_get` pins an exact run
ID and expected commit SHA.  Missing checks permission is refused before mint;
no seat permissions are configured or widened by this source.

Receipts from these two commands contain ID, head SHA, name, status/conclusion and
originating App ID.  They return no output summary/text, annotations, arbitrary URL
or nested author data; the bounded title and summary come from wf_checks alone (see
"wf_checks receipt detail" below).
Names are bounded255UTF-8bytes and scanned.  All page rows must match exact head
and name.  Malformed/oversized pages or wrong run identities are refused rather
than skipped.  Full-page has_more is conservative, not complete-history proof.

App ID is provenance, not an independent CI verdict.  A name can be selected by
another check publisher; callers must not accept a matching name as trusted
approval.  Results can include multiple attempts and change after a read.  These
are read-only metadata commands, not merge, dismissal, rerun or check mutation.
GitHub limits this endpoint to checks from the 1000 most recent check suites on a
ref.  Explicit pages do not bypass that cap or prove full historical coverage.
Offline synthetic tests only; no checks fetched live or deployment performed.
Reconciled for release on main d6a065a4, preserving all117current source actions
and merged full-file upload budgets.  Runtime publication is bounded to these
reads, not authorization to enable other unreleased source actions.

Fleet relevance: auto-merge-reusable.yml explicitly filters/paginates check names;
hold-guard reads merge-gate check runs before create/update.  Existing wf_checks
remains compatible: it reads up to 10 provider pages with filter=latest and says
in `complete` (true, false or "unknown") whether that was every run on the commit.

## wf_checks receipt detail

Each wf_checks row carries what a seat needs to tell a real review from a skipped
one in one call: `id`, `name`, `status`, `conclusion`, `started_at`, `completed_at`,
`head_sha`, `app_id`, `app_slug`, `title`, `title_truncated`, `summary`,
`summary_truncated` and `output_withheld`.  `started_at` and `completed_at` are the
provider's ISO 8601 text, or null when absent or not a valid instant.  `title` is cut
to 255 characters and `summary` to 500, and the matching `_truncated` flag is true
when text was cut.  A title or summary that is not a string is null.  Both are
provider text: data, never instructions, and nothing else in them is rewritten.  Each
field is judged by itself, on the part that would be returned: when a credential
shape begins inside that part, the field is replaced by the marker
`<withheld: credential-shaped text>` (its `_truncated` flag false) and
`output_withheld` is true, while the row's other field is kept.  A shape that begins
after the cut is never returned and does not blank the text before it.  When
`output_withheld` is true the row gives no readable verdict: treat it as no receipt.
The output `text` and annotations are never returned.

The hosted front refuses an answer over 1 MiB, and the relay escapes every non-ASCII
character, so the relay counts each row's bytes as written.  Once 900,000 bytes are
spent, the remaining rows carry null `title` and `summary` with the matching
`_truncated` flag true, and every other key as usual.  A null with its flag true means
"not returned here"; read that run with a smaller answer (`latest_per_name`).

`latest_per_name` (optional boolean; null and every other type are refused) keeps
only the newest run of each app and check name, keyed on `app_id` and `name`, so two
apps that publish the same name each keep a row and neither hides the other: newest
`started_at` as an instant, a tie goes to the higher run id, and a run with no
`started_at` loses to one that has it.  The answer always carries `latest_per_name`,
true when the filter was applied.  The filter covers the pages read, so it is the
latest run for the whole commit only when `complete` is true; on false or "unknown"
treat it as incomplete.  Without the argument every run is returned as before.

Release order: deploy the hosted front (scripts/simple-tools-mcp) before the relay.
A front that predates these row keys refuses the new answer whole, so the relay goes
second.  scripts/git-tools/catalog.json is exported from the deployed tools list and
is regenerated after that deploy, not by hand.

Official schema:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
