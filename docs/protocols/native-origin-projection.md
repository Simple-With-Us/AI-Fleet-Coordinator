# Review-origin projection trust and availability

For CODEX `gr_open_pr` with per-call native context, the hosted tool requires a service-owned exact projection before creation. Invalid or conflicting metadata, failed projection, and explicit claims disagreeing with the projection hold creation. `sessionId` is never substituted for the concrete `threadId`.

Callers without native context retain the existing ordinary PR and explicit three-claim compatibility path. An explicit session/letter/generation envelope on this path is caller-supplied data, not verified per-call custody. A shared seat credential authenticates the seat, not a particular chat. Neither explicit claims nor native metadata alone authorize a review alert. The protected first-confirmed creation receipt and independent canonical registry binding checks remain mandatory. Missing creation-time generation claims are not filled later.

The dedicated HTTPS projection returns exactly:

- `origin_session_claim`: lowercase UUID, equal to the requested concrete session.
- `origin_letter_claim`: two uppercase letters.
- `origin_generation_claim`: positive safe-integer JSON number, not a string.

The network client validates this strict envelope before returning it. `resolveNativeOriginClaims` independently checks the envelope and requested-session match and copies/freezes the accepted claims. No response title, route, credential or extra field becomes an origin authority.

`REVIEW_ORIGIN_CODEX_READ_KEY` is an optional capability. The loader sources it only from its approved `/operator` listing and strips container copies. A missing, malformed or reused optional key disables the projection, not the general listener, registry or sweeper. The daemon log names the key variable and fixed reason, never the key value. Exact-context PR calls still hold if projection is unavailable. Required shared source/identity failures retain their existing startup behavior.

Source tests and isolated transport evidence are not live receipt, binding or alert-delivery qualification. Deployment and live checks remain separate.
