# Bounded issue relation reads

Four default-off own-App issues:read commands read parent, subissues, blocking and
blocked-by relations on an exact repo issue number.  Lists are1-50 per page,
pages1-1000, with conservative has_more when a full page is returned, not a false
complete-history claim.  Parent absence remains an upstream refusal, not an invented empty parent; callers cannot distinguish absence from other upstream refusals.
Returns compact issue IDs, numbers, titles and states, no bodies or arbitrary URLs.
Repo/action allowlists, credential ceiling and returned secret scans remain.
No permissions configured and no live relation changed.  Capability is source only.
Relationship writes, replacing parents, issue transfer and cross-repo search remain
unimplemented.  Foreign or unverifiable related repository bindings are refused before metadata extraction.
Credential-shaped titles are replaced per item by a fixed placeholder, so the rest of the page remains readable.
Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
