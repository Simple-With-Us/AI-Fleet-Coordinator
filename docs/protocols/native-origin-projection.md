# Review-origin projection trust and availability

For CODEX `gr_open_pr` with per-call native context, the hosted tool requires a service-owned exact projection before creation. Invalid or conflicting metadata, failed projection, and explicit claims disagreeing with the projection hold creation. `sessionId` is never substituted for the concrete `threadId`.

Callers without native context retain the existing ordinary PR and explicit three-claim compatibility path. An explicit session/letter/generation envelope on this path is caller-supplied data, not verified per-call custody. A shared seat credential authenticates the seat, not a particular chat. Neither explicit claims nor native metadata alone authorize a review alert. The protected first-confirmed creation receipt and independent canonical registry binding checks remain mandatory. Missing creation-time generation claims are not filled later.

The dedicated HTTPS projection returns exactly:

- `origin_session_claim`: lowercase UUID, equal to the requested concrete session.
- `origin_letter_claim`: two uppercase letters.
- `origin_generation_claim`: positive safe-integer JSON number, not a string.

The network client validates this strict envelope before returning it. `resolveNativeOriginClaims` independently checks the envelope and requested-session match and copies/freezes the accepted claims. No response title, route, credential or extra field becomes an origin authority.

`REVIEW_ORIGIN_CODEX_READ_KEY` is an optional capability with one canonical record: Fleet-OPS prod `/zulip`. The listener retains this one name from its existing secondary-project `/zulip` read. That source uses the already-bound listener reader, not the primary AFC `/operator` reader. A record under either primary source holds the capability instead of becoming a fallback. The loader strips container copies and keeps its existing printable-length, unresolved-reference and key-reuse checks. A missing or unusable optional key disables the projection, not the general listener, registry or sweeper. Required shared-source and identity failures retain their existing startup behavior.

The Worker lazily reads that same key and `REVIEW_ORIGIN_READ_URL` through its existing Fleet-OPS `/zulip` runtime loader only when a verified exact CODEX creation invocation needs them. The URL is the existing public HTTPS projection endpoint with the exact `/registry/v1/review-origin` path. The shared runtime cache applies. Neither value falls back to a persistent Cloudflare binding, including in `off` or transitional `fallback` mode. Do not change the global Worker workspace or add a second Infisical record to make this capability work.

The listener and Worker use their own existing scoped readers. No reader token is shared, no grant is widened, and no new transport or registry path is introduced. The daemon logs fixed reasons and variable names, never values. Exact-context PR calls still hold if the projection or its canonical source is unavailable.

Source tests and isolated transport evidence are not live receipt, binding or alert-delivery qualification. Deployment and live checks remain separate.
