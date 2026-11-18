# Collaborator administration custody preparation

Status: inactive source preparation. No relay dispatch, MCP tool, policy-loader field, live grant, App-permission acceptance or deployment is added by this module. It does not complete the collaborator-write family.

`git_relay.collaborator_custody` prepares an additional exact seat/repository/action gate for future collaborator set/remove handlers. The server must first authenticate the caller and apply its ordinary action, repository and quota checks. The optional grant data must come from protected server policy, never request parameters. Absent or empty grants deny every operation. Repository wildcards do not inherit a grant. Grant repositories must also exist explicitly in the ordinary repository scope. Own-App identity is mandatory.

The preparation uses a separate one-operation installation lease. The generic `token_for` mint ceiling and `SeatPolicy` stay unchanged, including rejection of administration:write. Namespace creation is not reused. The lease asks for exactly one repository and metadata:read plus administration:write. It checks own-App identity, installation account, accepted administration permission, suspension status, returned permission set, returned repository and bounded expiry. It never caches the installation token. Setup errors are fixed safe errors. Every path after receiving a usable token attempts revocation, including malformed returned scope and handler errors. A failed revocation raises terminal `CustodyCleanupPending`; it must never lead to replay of a potentially completed write. The token may remain valid until its verified expiry in that case.

The future handler contract still needs to cover:

- Explicit target username, safe path encoding and identity checks. No arbitrary endpoint, caller credential or permission map.
- Explicit role for collaborator set, including supported custom roles. Never inherit the provider's default push role silently.
- Distinct invitation-created and provider-applied outcomes. A successful set can invite someone or change an existing collaborator's access and can trigger notifications.
- Explicit acknowledgement of removal effects. Removal may affect forks, projects, assignments and package access. It is not a promise of total access removal because organization-derived access can remain and updates can propagate later.
- Existing durable write-outcome handling and no blind replay after an uncertain response or cleanup failure. A transport error is not proof that no write happened.
- Typed MCP/CLI parity, strict projected receipts, final source review, and separate host/MCP deployment qualification.

A later integration must not accept a grant merely because this parser can read it. Before any deployment or configuration changes effective authorization, recover the authority for that exact boundary change and review the final config. Availability of the tool will not itself authorize invitations or access changes for particular people.

Official collaborator API contract, checked November 18, 2026:
https://docs.github.com/en/rest/collaborators/collaborators#add-a-repository-collaborator
https://docs.github.com/en/rest/collaborators/collaborators#remove-a-repository-collaborator
