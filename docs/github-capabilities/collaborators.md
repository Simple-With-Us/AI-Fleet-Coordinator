# Bounded repository collaborator read

`wf_repo_collaborators` / `gr_wf_repo_collaborators` reads one visible page from the fixed repository collaborator endpoint. Inputs are the policy-allowlisted repository, page1-1000, per_page1-50, optional affiliation (`all`, `direct`, `outside`) and optional permission (`pull`, `triage`, `moderate`, `push`, `maintain`, `admin`). It requests only metadata:read from the bound own-App installation. It adds no administration-write mint, live policy grant, alternate identity, generic URL or account effect.

The receipt contains collaborator ID, login, account type, highest role name, five base permission booleans, requested affiliation, page and conservative has_more. IDs must be positive safe integers and unique; logins must be unique ignoring case. Names are bounded and credential-shaped metadata is refused. Raw user records, email, node IDs, URLs and other upstream fields are not returned.

A full page sets has_more=true without claiming another page exists. This is not a complete access inventory, an atomic snapshot or a direct-grant report. GitHub combines repository, team, organization and enterprise sources; the endpoint cannot distinguish those grant sources. Custom role names are preserved as bounded text, not reduced to base roles.

Official contract: https://docs.github.com/en/rest/collaborators/collaborators#list-repository-collaborators

Source implementation and offline qualification are separate from deployment. The committed deployed catalog is not regenerated from invented source schemas. Reviewed source, host/MCP rollout, complete live tools/list capture, catalog regeneration and authorized own-App readback remain separate evidence. Collaborator invitations/removal, administration, branch protection, ruleset, webhook and Actions-settings writes remain unfinished typed families; this read does not complete them.

`wf_collaborator_permission` / `gr_wf_collaborator_permission` is the companion metadata:read action for one exact username. It returns only the matched user ID/login/type, the provider legacy base permission (`admin`, `write`, `read`, `none`) and highest role name, preserving custom roles. The username is path-encoded and response login must match it ignoring case. No membership conclusion is invented from404, and no grant source is inferred. The companion action uses the same official collaborator API contract cited above.
