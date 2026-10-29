# Bounded environment metadata reads

Two default-off own-App actions:read commands list environments or fetch an exact
name.  Name input deliberately supports a bounded conservative subset (ASCII,
1-100 characters); slash is encoded as part of the environment name, never a URL.
Pages1-1000, size1-50, conservative has_more on full page.  Compact metadata and
protection TYPE names only, not full reviewers/wait policy.  Missing optional
protection fields are null, never asserted unprotected.  No raw policy writes,
approvals, deployment effects, grants, or live smoke tests.  Existing repo/action
allowlists/ceiling/response secret scans apply.
This is not full environment management; create/update/delete/protection rules,
environment variables/secrets and approvals remain unimplemented.
Sources: https://docs.github.com/en/rest/deployments/environments
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
