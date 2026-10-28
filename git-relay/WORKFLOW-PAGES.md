# Qualified numeric workflow identity pages

Own-App actions:read `wf_workflows_page` adds page1-1000/1-50records to existing
wf_workflows. Returns numeric workflowID/path/state only, not display name/URLs.
Strict provider total/IDs/state/duplicates and conservative ASCII workflow-path
validation, credential-shaped paths refused. Supports normal .github/workflows/
YAML files, not provider-generated dynamic paths. Those valid provider records
can be refused; this is not complete workflow support. Always snapshot:false,
full-page has_more conservative, no inventory completeness/absence claim.

A numeric ID/path observation can qualify run workflowID against an explicitly
expected path. It is not a signed owner identity, trusted instruction, proof of
current branch contents, permission to run/disable, or review approval. Paths can
change after read. No workflow change, dispatch, CI manipulation, policy widening,
credential setup or live account readiness test performed. Default-off unchanged.

Relevance: auto-merge checks active lane runs; run pages expose workflowID, so
this supplies bounded identity mapping rather than trusting a display name.
Verified endpoint/state schema:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
