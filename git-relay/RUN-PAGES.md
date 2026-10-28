# Qualified Actions run pages

Default-off own-App actions:read `wf_runs_page` adds explicit paging and strict
receipts to existing run listing. Existing wf_runs already filters branch/headSHA/
status. New command supports the documented waiting/pending/requested statuses and
conclusion filters, numeric workflowID/runID/attempt, head branch/SHA, status/
conclusion and up to100PRnumbers per run. No names, workflow path, actor, URLs,
logs, timestamps or arbitrary provider fields. Filter/duplicate/shape validation
fails closed. Expected branch grammar is bounded conservative ASCII; other valid
GitHub refs can therefore be refused. No reviewer or workflow identity inference
from text. workflowID needs a separately grounded mapping before decisions.

Pages1-1000,1-50records. GitHub limits searches filtered by branch/headSHA/status
(and other filters not exposed here) to1000results. search_cap:1000 names that
limit; null means this search restriction is not invoked, not infinite history.
Requests starting outside the filtered window are refused before provider reads.
A short page or count cannot prove all runs known: mutable state/list membership,
provider retention and search limits remain. snapshot:false always; full-page
has_more conservative. No polling, live workflow execution or permission widening.

Fleet relevance: auto-merge-reusable.yml reads active runs and their PR association.
This command is evidence for triage, not approval to ignore a pending run or merge.
No files/branches/reviews/CI gates changed by this implementation.
Verified provider paging/status/1000-result limits:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
