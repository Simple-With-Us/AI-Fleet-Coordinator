# Bounded issue relation reads

Four default-off own-App issues:read commands read parent, subissues, blocking and
blocked-by relations on an exact repo issue number.  Lists are1-50 per page,
pages1-1000, with conservative has_more when a full page is returned, not a false
complete-history claim.  Parent404 is propagated rather than invented as no parent.
Returns compact issue IDs, numbers, titles and states, no bodies or arbitrary URLs.
Repo/action allowlists, credential ceiling and returned secret scans remain.
No permissions configured and no live relation changed.  Capability is source only.
Relationship writes, replacing parents, issue transfer and cross-repo search remain
unimplemented.  Read results can include another repo under GitHub relationships;
returned number alone must not be treated as a bound mutation destination.
Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
