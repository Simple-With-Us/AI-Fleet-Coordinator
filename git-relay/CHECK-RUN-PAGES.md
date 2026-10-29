# Exact check-run pages and metadata reads

Two default-off own-App checks:read commands address fleet review/merge-gate
inspection. `wf_check_runs_page` pins a commit SHA and exact check name, requests
filter=all and one page1-1000 of at most50runs. `wf_check_run_get` pins an exact run
ID and expected commit SHA. Missing checks permission is refused before mint;
no seat permissions are configured or widened by this source.

Receipts contain ID, head SHA, name, status/conclusion and originating App ID.
No output summary/text, annotations, arbitrary URL or nested author data returns.
Names are bounded255UTF-8bytes and scanned. All page rows must match exact head
and name. Malformed/oversized pages or wrong run identities are refused rather
than skipped. Full-page has_more is conservative, not complete-history proof.

App ID is provenance, not an independent CI verdict. A name can be selected by
another check publisher; callers must not accept a matching name as trusted
approval. Results can include multiple attempts and change after a read. These
are read-only metadata commands, not merge, dismissal, rerun or check mutation.
GitHub limits this endpoint to checks from the 1000 most recent check suites on a
ref. Explicit pages do not bypass that cap or prove full historical coverage.
Offline synthetic tests only; no checks fetched live or deployment performed.
This slice was rebuilt on current main f07ca2e4, not the old held integration union.
The existing 115 actions and merged full-file upload budgets remain unchanged.

Fleet relevance: auto-merge-reusable.yml explicitly filters/paginates check names;
hold-guard reads merge-gate check runs before create/update. Existing wf_checks
remains compatible, but its first-page result is not full paginated parity.
Official schema:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
