# AGENT-SYNC Rewrite Rule Map

Every rule in the previous `AGENT-SYNC.md` (2,230 lines, `origin/main` before the Wed, Oct 7, 2026 rewrite), mapped to its home in the [rewritten document](../../AGENT-SYNC.md) or to the reason it was retired.  Generated with Python from the rule ledger (1,091 atomic rules), the writers' carried-id lists, and the outline's retire list.  Old line numbers refer to the previous version.

- Carried into the new document:  1031 rules.
- Retired:  60 rules.
- Unplaced (in neither list):  0.

The [Recent Decisions (2026-10-07)](../../AGENT-SYNC.md#recent-decisions-2026-10-07) section holds the rulings and defaults behind the rewrite and has no ledger rules of its own.

## Unplaced

None.

## Rules per Section

| New section | Rules |
| --- | --- |
| [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) | 7 |
| [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) | 30 |
| [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) | 67 |
| [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) | 13 |
| [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) | 33 |
| [Posting](../../AGENT-SYNC.md#posting) | 63 |
| [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) | 95 |
| [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) | 38 |
| [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) | 84 |
| [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) | 11 |
| [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) | 115 |
| [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) | 67 |
| [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) | 28 |
| [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) | 47 |
| [Apple Notes](../../AGENT-SYNC.md#apple-notes) | 29 |
| [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) | 35 |
| [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) | 19 |
| [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) | 24 |
| [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) | 21 |
| [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) | 39 |
| [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) | 29 |
| [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) | 20 |
| [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) | 45 |
| [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) | 27 |
| [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) | 15 |
| [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) | 30 |
| Retired | 60 |

## Every Rule

| Rule id | Old lines | Old section | New home |
| --- | --- | --- | --- |
| `L3-audience-roster` | 3-4 | Header | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L4-app-scope` | 4-7 | Header | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L9-key-by-id` | 9 | Header | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L9-slack-channel-id` | 9 | Header | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L10-zulip-move` | 10 | Header | retired:  Transition note ('until Zulip lands, the Slack protocol stands') is superseded by the owner's own 2026-10-07 hard-cut ruling.  The rewrite states Zulip as the only chat, so nothing the owner still wants is dropped. |
| `L11-pointer-files` | 11 | Header | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L17-coordination-purpose` | 17-20 | Overview | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L18-effort-board-source-of-truth` | 17-19 | Overview | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L22-channel-never-replaces-board` | 22 | Overview | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L22-reserve-before-work` | 22-24 | Overview | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L30-acronym-ST` | 30 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L31-acronym-CT` | 31 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L32-acronym-UM` | 32 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L33-acronym-CTS` | 33 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L34-acronym-DD` | 34 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L35-acronym-AFC` | 35 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L36-acronym-PS` | 36 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L37-acronym-AR` | 37 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L38-acronym-CL` | 38 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L39-acronym-BF` | 39 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L40-acronym-CK` | 40 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L41-acronym-HH` | 41 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L42-acronym-OPS` | 42 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L44-registry-source` | 44-45 | App acronyms | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L45-acronym-usage` | 45-46 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L48-afc-signing-tag` | 48-49 | App acronyms | [Posting](../../AGENT-SYNC.md#posting) |
| `L49-fleet-broadcast-wake` | 49-51 | App acronyms | [Posting](../../AGENT-SYNC.md#posting) |
| `L51-afl-fleet-aliases-retired` | 51-52 | App acronyms | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L56-absolute-rules-scope` | 56 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L58-root-cause-real-solution` | 58-59 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L59-no-suppress-hide-defer` | 59 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L59-silenced-loop-example` | 59 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L61-no-bury-in-prose` | 61-62 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L63-no-bury-option-solve` | 63 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L64-no-bury-option-track` | 64 | Absolute Problem-Solving & Communication Rules | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L70-infisical-canonical-all-keys` | 70-72 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L72-infisical-first-then-propagate` | 72-75 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L75-no-mint-outside-infisical` | 75-76 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L78-owner-drops-file-not-chat` | 78-80 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L81-agent-never-print` | 80-82 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L82-prefer-scoped-credential` | 82-83 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L83-remind-revoke` | 83-84 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L84-handoff-all-platforms` | 84-86 | Secret handoff | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L91-global-keys-canonical-path` | 91-93 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L93-env-sibling-retired-history` | 93-98 | Global API keys | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L98-superseded-file-do-not-use` | 98-100 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L100-no-env-sibling` | 100-101 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L103-key-names-endpoint` | 103-104 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L104-file-not-served-http` | 104 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L105-god-token-history` | 105-106 | Global API keys | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L106-cloud-seats-use-infisical` | 106-107 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L107-local-seats-read-file` | 107-108 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L108-bearer-header-only` | 108-109 | Global API keys | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L113-runtime-secrets-in-infisical` | 111-114 | Infisical sole source for app runtime secrets | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L114-handoff-convenience-copy` | 114-116 | Infisical sole source for app runtime secrets | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L116-cross-app-key-copy` | 116-119 | Infisical sole source for app runtime secrets | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L119-r2-digest-bug-history` | 119-120 | Infisical sole source for app runtime secrets | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L122-coolify-tokens-do-not-mix` | 122 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L124-coolify-handoff-file` | 124 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L128-coolify-server-stats` | 128 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L129-coolify-deploy` | 129 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L130-coolify-agents` | 130 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L133-never-agents-as-api-token` | 133 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L134-api-token-equals-server-stats` | 134 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L135-store-named-keys` | 135 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L136-stats-first-in-code` | 136 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L137-github-autodeploy-webhook` | 137 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L137-prefer-deploy-token` | 137 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L138-coolify-team-scoped-only` | 138 | Coolify tokens | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L140-coolify-operator-guide` | 140-143 | Coolify tokens | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L143-prefer-live-api-over-uuids` | 143 | Coolify tokens | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L145-cf-no-dead-without-resource-call` | 145 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L147-cf-ct-night-history` | 147-148 | Cloudflare credential testing | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L148-um-same-pattern-history` | 148-149 | Cloudflare credential testing | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L150-cf-rule-out-both-mistakes` | 150 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L152-cf-user-tokens-verify` | 152-155 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L156-cf-9103-wrong-email` | 156-158 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L158-cf-four-logins` | 158-162 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L160-cf-enumerate-accounts` | 160-162 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L162-cf-four-accounts` | 162-164 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L165-cf-dns-registrars-doc` | 164-165 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L167-cf-bearer-empty-not-dead` | 167-168 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L170-cf-credential-map-location` | 170-171 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L171-attack-map` | 171-173 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L173-no-copy-attack-map-public` | 173-174 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L174-use-fleet-api-token` | 174-177 | Cloudflare credential testing | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L181-infisical-bare-secrets-forbidden` | 181-182 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L185-cmd-infisical-secrets` | 185 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L186-cmd-infisical-output-dump` | 186 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L187-cmd-infisical-get-plain` | 187 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L192-cmd-infisical-set-silent` | 192-193 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L196-safe-helper-has` | 195-196 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L197-safe-helper-names` | 197 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L198-safe-helper-set` | 198 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L201-secret-safety-skill` | 201-202 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L201-verify-writes-presence-length` | 201 | Infisical CLI forbidden patterns | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L204-grep-trap-binding` | 204 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L206-multi-secret-file-leak` | 206-208 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L213-forbidden-grep-rg-handoff` | 210-216 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L216-forbidden-rg-secrets-dir` | 216 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L217-forbidden-cat-read-open` | 217-218 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L225-allowed-names-only-grep` | 224-225 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L228-allowed-one-key-into-variable` | 227-228 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L229-redact-later-output` | 227,229 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L232-grep-without-o-is-leak` | 232-233 | Handoff-file grep trap | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L233-grok-grep-incident` | 233-235 | Handoff-file grep trap | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L239-loaded-variable-still-secret` | 237-239 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L239-byte-dump-tools` | 239-241 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L241-printf-last-command` | 241-242 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L244-siliconflow-od-incident` | 244-246 | Loaded-key byte dumps | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L248-forbidden-name-match` | 248 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L251-forbidden-od-xxd-hexdump` | 251-253 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L254-forbidden-cut-c` | 254 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L255-forbidden-printf-q` | 255 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L256-forbidden-xxd-file` | 256 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L259-allowed-shape-length-only` | 259-265 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L268-secret-guard-hook` | 268-269 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L270-seats-without-hook-still-follow` | 270 | Loaded-key byte dumps | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L276-prior-not-dropped` | 276 | Prior messages stay in scope | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L278-prior-scope-platforms` | 278-279 | Prior messages stay in scope | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L281-prior-full-conversation-active` | 281-283 | Prior messages stay in scope | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L284-prior-followups-add` | 284-285 | Prior messages stay in scope | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L286-prior-todo-park` | 286-288 | Prior messages stay in scope | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L289-prior-peer-no-cancel` | 289 | Prior messages stay in scope | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L291-prior-ruling-date-mirror` | 291 | Prior messages stay in scope | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L292-prior-also-in` | 292-293 | Prior messages stay in scope | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L299-commit-no-wait` | 299-300 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L300-commit-rationale` | 300-302 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L304-commit-reaffirm-auto` | 304-305 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L305-commit-no-permission` | 305-306 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L306-commit-solo-velocity` | 306-307 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L309-commit-scope` | 309-310 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L312-commit-by-default` | 312-314 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L315-commit-not-working-tree-only` | 315-316 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L316-commit-report-uncommitted-why` | 316-317 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L317-commit-later-forbidden` | 317-318 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L319-commit-one-logical` | 319-320 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L320-commit-repo-protocol` | 320-321 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L321-commit-no-force-amend` | 321 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L321-commit-no-secrets` | 321 | Always commit + land finished work | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L322-pr-push-default` | 322-323 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L323-pr-preferred-path` | 323-324 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L324-pr-branch-without-pr-unfinished` | 324-327 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L326-pr-no-branch-parking` | 326-327 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L328-land-merge-when-green` | 328-330 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L330-land-no-park` | 330-331 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L332-land-delete-branch` | 332-333 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L334-destructive-pause` | 334-336 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L337-subagent-commit` | 337-339 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L340-board-claim-closeout` | 340-341 | Always commit + land finished work | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L344-anti-you-can-commit` | 344 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L345-anti-dirty-worktree` | 345 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L346-anti-local-only` | 346 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L347-anti-no-pr` | 347 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L348-anti-triple-halfimpl` | 348 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L349-anti-passive-waiting` | 349 | Always commit + land finished work | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L351-commit-history` | 351-352 | Always commit + land finished work | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L358-wait-never-poll` | 356-358 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L359-wait-rationale` | 359 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L362-wait-drive-prs` | 362 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L363-wait-resolve-conflicts` | 363 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L364-wait-resolve-ci-review` | 364 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L365-wait-merge-deploy` | 365 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L366-wait-no-idle-loops` | 366 | Never wait and watch for PRs to merge / Idle-polling forbidden | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L370-code-ruling` | 370-371 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L372-code-integration-tree` | 372-374 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L374-code-worktrees-in-apps` | 374-375 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L375-code-extends-never-work` | 375-377 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L378-code-no-new-toplevel` | 378-380 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L380-code-binding-incl-owner` | 380-382 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L382-code-stray-belong-apps` | 382-384 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L386-lane-new-location` | 386-389 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L387-lane-review-location` | 387-388 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L389-lane-new-command` | 389 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L390-lane-seat-token` | 390-391 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L392-lane-no-tmp` | 391-392 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L393-lane-flat-retire` | 392-393 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L393-code-rule-unchanged` | 393-394 | Hard rule: ~/Code/ is for integration trees only | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L398-forbid-linked-worktrees` | 398-400 | Forbidden top-level entries under ~/Code/ | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L401-forbid-scratch-copies` | 401-402 | Forbidden top-level entries under ~/Code/ | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L403-forbid-lane-names` | 403-404 | Forbidden top-level entries under ~/Code/ | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L405-forbid-backups` | 405-406 | Forbidden top-level entries under ~/Code/ | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L410-code-exception-data-only` | 408-412 | Permitted exceptions | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L412-code-no-denylist-app-names` | 412-414 | Permitted exceptions | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L418-keeper-daemon` | 418-420 | Enforcement | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L420-keeper-detection` | 420-423 | Enforcement | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L423-keeper-stray-header` | 423-424 | Enforcement | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L425-keeper-no-autoprune` | 425-428 | Enforcement | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L429-cleanup-worktree-remove` | 429-431 | Enforcement | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L434-cleanup-worktree-prune` | 433-435 | Enforcement | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L437-cleanup-branch-preserved` | 437-439 | Enforcement | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L439-cleanup-dirty-lost` | 439-440 | Enforcement | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L441-cleanup-stash-first` | 441-442 | Enforcement | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L446-why-references` | 446-449 | Why "unbreakable" | retired:  Propagation bookkeeping ('why unbreakable': which docs mirror the rule).  The rewrite states the rule once and the onboarding section owns propagation, so the cross-reference list adds nothing. |
| `L449-why-pm2-visibility` | 449-451 | Why "unbreakable" | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L457-notes-ruling` | 457-460 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L462-notes-scope` | 462-466 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L468-notes-create` | 468-470 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L471-notes-folder` | 471-473 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L474-notes-pin` | 474 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L476-notes-helper-paths` | 475-477 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L478-notes-helper-update` | 478 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L479-notes-helper-html` | 479 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L480-notes-helper-notify` | 480 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L481-notes-helper-needs-owner` | 481 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L482-notes-helper-summary` | 482 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L483-notes-no-markdown` | 483 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L487-notes-title-full-text-pointer` | 487 | Title + structure standard | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L487-notes-title-moved-history` | 487 | Title + structure standard | retired:  Doc-housekeeping history (the standard was moved out of the always-loaded doc on 2026-09-01).  The pointer to the full standard survives in L487-notes-title-standard. |
| `L487-notes-title-standard` | 487 | Title + structure standard | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L491-completion-open-living` | 491-492 | Completion / work-complete notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L494-completion-write` | 494-496 | Completion / work-complete notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L497-completion-update-same` | 497-500 | Completion / work-complete notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L498-completion-exempt` | 498-499 | Completion / work-complete notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L501-notes-qualifies` | 501-502 | Completion / work-complete notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L504-notes-skip` | 504-505 | Completion / work-complete notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L507-pin-methods-intro` | 507-509 | Pinning & Unpinning Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L511-pin-opt1-setup` | 511-524 | Pinning & Unpinning Apple Notes | retired:  Interactive keyboard-shortcut pinning is superseded.  The owner's global rules say the helper auto-pins headlessly and agents never pin via GUI or AppleScript. |
| `L526-pin-opt1-usage` | 526 | Pinning & Unpinning Apple Notes | retired:  Same as L511: the GUI keyboard-shortcut route is superseded by headless helper pinning. |
| `L528-pin-opt2-purpose` | 528-529 | Pinning & Unpinning Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L531-pin-opt2-shortcut1` | 531-536 | Pinning & Unpinning Apple Notes | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L537-pin-opt2-shortcut2` | 537-540 | Pinning & Unpinning Apple Notes | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L543-pin-opt2-cli` | 542-546 | Pinning & Unpinning Apple Notes | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L548-pin-opt2-helper` | 548-551 | Pinning & Unpinning Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L552-pin-opt2-first-run` | 552 | Pinning & Unpinning Apple Notes | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L554-pin-opt3-fallback` | 554-555 | Pinning & Unpinning Apple Notes | retired:  The System Events menu-click fallback steals focus and contradicts the owner's global rule never to pin via GUI or AppleScript.  The headless Shortcuts path (L531) is the only supported route. |
| `L557-notes-no-mac` | 557 | Apple Notes for owner-facing review docs | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L559-notes-canonical-mirror` | 559-560 | Apple Notes for owner-facing review docs | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L559-notes-codified-dates` | 559 | Apple Notes for owner-facing review docs | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L564-proc-scope-dates` | 564 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L566-proc-master-list` | 566-567 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L568-proc-owner-note` | 568-569 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L571-proc-trigger-row` | 571-575 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L574-proc-always-on-label` | 574-576 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L576-proc-retire-in-place` | 576-577 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L579-proc-not-optional` | 579-580 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L582-proc-claude-remote-control` | 582-584 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L586-proc-cloud-agents` | 586-587 | Mac local processes | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L591-ver-scope` | 591-593 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L596-ver-patch-sequence` | 596 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L597-ver-every-build` | 597 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L598-ver-ban-0x` | 598 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L601-tf-notes-required` | 600-601 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L602-tf-build-header` | 602 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L603-tf-release-date` | 603-604 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L604-tf-no-agent-names` | 604-606 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L605-tf-summary` | 605 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L607-tf-no-force-ship` | 607 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L607-tf-wtt-body` | 607 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L607-tf-wtt-mandatory` | 607 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L607-tf-wtt-no-empty` | 607 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L607-tf-wtt-publish` | 607 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L609-tf-template` | 609-618 | App Versioning & TestFlight Build Policy | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L620-tf-automation` | 620-621 | App Versioning & TestFlight Build Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L621-tf-two-spaces` | 621 | App Versioning & TestFlight Build Policy | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L624-two-space-core` | 624-628 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L626-two-space-trigger-history` | 626-628 | Two spaces between sentences | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L630-two-space-not-optional` | 630-632 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L634-surface-inapp-ui` | 634 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L635-surface-asc` | 635-637 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L638-surface-testflight` | 638 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L639-surface-push-email-help` | 639 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L640-surface-notes-rollouts-readme` | 640 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L641-surface-doc-boards` | 641 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L641-surface-slack-owner-posts` | 641 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L643-two-space-all-paragraphs` | 643-646 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L646-two-space-platforms` | 646-648 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L647-two-space-prose-surfaces` | 647-650 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L648-two-space-slack-agent-sync` | 648 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L651-single-space-abbrev` | 651-652 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L652-html-collapse` | 652-653 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L653-md-hardbreak` | 653-654 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L656-how-visible-history` | 656-659 | Two spaces between sentences | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L660-chat-nbsp-entity` | 660-663 | Two spaces between sentences | retired:  Superseded.  The owner verified two literal ASCII spaces in the Claude Code desktop app on 2026-09-04 and in Zulip, and the owner must never see the literal text &nbsp;.  The surviving per-surface table lives in L664-files-ascii-spaces, L664-slack-posts-ascii-spaces and L652-html-collapse. |
| `L664-files-ascii-spaces` | 664-666 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L664-slack-posts-ascii-spaces` | 664 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L667-html-render-nbsp` | 667-668 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L668-notes-helper-converts` | 669-670 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L671-tested-not-work` | 671-674 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L676-portable-skill` | 676-679 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L681-cursor-user-rule` | 681-683 | Two spaces between sentences | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L684-cursor-local-paths` | 684-685 | Two spaces between sentences | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L685-grok-paths` | 685-686 | Two spaces between sentences | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L688-how-example` | 688-690 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L690-brand-period` | 690-691 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L693-does-not-apply` | 693-694 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L696-accuracy-matches-truth` | 696-697 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L697-ct-coverage` | 697-699 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L699-trial-length` | 699-700 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L702-copy-detail-pointer` | 702 | Two spaces between sentences | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L706-ios-loop-scope` | 706-708 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L708-ios-fulltext-pointer` | 708 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L708-ios-no-hand-edit` | 708 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L708-ios-no-xcode-mcp` | 708 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L708-ios-screenshot-before-claim` | 708 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L708-ios-xcodebuild-preapproved` | 708 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L710-cloud-needs-mac-command` | 710 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L710-mac-seat-watch-poller` | 710 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L710-needs-mac-slack-post` | 710 | iOS agent build loop | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L714-macapp-dev-builds` | 714 | Mac app builds: exactly one installed copy | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L714-macapp-dist-staging` | 714 | Mac app builds: exactly one installed copy | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L714-macapp-history` | 714 | Mac app builds: exactly one installed copy | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L714-macapp-install-location` | 714 | Mac app builds: exactly one installed copy | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L714-macapp-install-prune` | 714 | Mac app builds: exactly one installed copy | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L714-macapp-no-touch-owner-copy` | 714 | Mac app builds: exactly one installed copy | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L714-macapp-unfinished` | 714 | Mac app builds: exactly one installed copy | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L716-ts-ruling-history` | 716 | Timestamps: Central Time | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L718-ts-core` | 718-719 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L719-ts-scope-all-writers` | 719-721 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L720-ts-scope-slack` | 720 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L721-ts-no-zulu` | 721-722 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L724-ts-no-zone-abbr` | 724-726 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L726-ts-calendar-day` | 726 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L726-ts-covers-surfaces` | 726-728 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L727-ts-covers-slack` | 727 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L730-ts-name-zone-only-nonlocal` | 730-734 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L732-ts-market-bells` | 732-733 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L733-ts-no-guess-conversion` | 733-734 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L736-ts-offset-dst` | 736-738 | Timestamps: Central Time | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L738-ts-offset-std` | 738 | Timestamps: Central Time | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L739-ts-midnight-utc` | 739-741 | Timestamps: Central Time | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L743-ts-abbr-not-in-prose` | 743-744 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L746-ts-machine-fields-utc` | 746-747 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L749-ts-exception-device-local` | 749-752 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L752-ts-exception-limits` | 752-754 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L753-ts-console-pins-chicago` | 753-756 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L758-ts-related` | 758 | Timestamps: Central Time | [Writing for the Owner: Sentence Gap, Timestamps, Copy Accuracy](../../AGENT-SYNC.md#writing-for-the-owner-sentence-gap-timestamps-copy-accuracy) |
| `L762-icon-scope` | 762-764 | App Icon & Logo Design Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L766-icon-fullbleed-master` | 766 | App Icon & Logo Design Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L767-icon-rationale` | 767 | App Icon & Logo Design Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L768-icon-mockup-rule` | 768 | App Icon & Logo Design Policy | [Releases, Versioning, and Brand Assets](../../AGENT-SYNC.md#releases-versioning-and-brand-assets) |
| `L774-universal-intro` | 772-774 | Universal Fleet Coordination Processes | retired:  The 'Universal Fleet Coordination Processes' template intro (with placeholders like <YOUR_PROJECT_NAME>) is dropped.  Every process rule under it is merged into its single fleet-specific home. |
| `L777-p1-hub` | 777 | Process 1: Inter-Agent Communication | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L778-p1-header-format` | 778-779 | Process 1: Inter-Agent Communication | [Posting](../../AGENT-SYNC.md#posting) |
| `L780-p1-afc-not-fleet` | 780 | Process 1: Inter-Agent Communication | [Posting](../../AGENT-SYNC.md#posting) |
| `L780-p1-fleet-history` | 780 | Process 1: Inter-Agent Communication | retired:  History of a reading the owner already retired on 2026-09-13 (FLEET waking only Grok Bot seats).  The live part of that ruling (a fleet wake reaches every seat) stays in L1384-fleet-use-only, and the Zulip fleet user group replaces FLEET semantics. |
| `L780-p1-fleet-wake` | 780 | Process 1: Inter-Agent Communication | [Posting](../../AGENT-SYNC.md#posting) |
| `L780-p1-peer-tag-reach` | 780 | Process 1: Inter-Agent Communication | [Posting](../../AGENT-SYNC.md#posting) |
| `L780-p1-standard-tags` | 780 | Process 1: Inter-Agent Communication | [Posting](../../AGENT-SYNC.md#posting) |
| `L781-p1-startup-poll-command` | 781 | Process 1: Inter-Agent Communication | retired:  agent-sync-poll.py is retired by the hard cut.  The session-start duty survives in L781-p1-startup-poll-duty as `agent-sync inbox`. |
| `L781-p1-startup-poll-duty` | 781 | Process 1: Inter-Agent Communication | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L782-p1-fleet-fullread` | 782 | Process 1: Inter-Agent Communication | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L782-p1-peer-messages-data` | 782 | Process 1: Inter-Agent Communication | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L782-p1-skim-and-fullread` | 782 | Process 1: Inter-Agent Communication | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L785-p2-claim-effort-board` | 785-786 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L787-p2-claim-github-issue` | 787 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L788-p2-claim-slack` | 788 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L790-p2-closeout-effort-board` | 790 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L791-p2-closeout-github-issue` | 791 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L792-p2-closeout-slack` | 792 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L793-p2-board-preservation` | 793 | Process 2: Shared Effort Board & Task Reservation | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L796-p3-feature-branches` | 796 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L796-p3-no-commit-main` | 796 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L797-p3-local-verify` | 797 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L797-p3-no-broken-push` | 797 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L798-p3-auto-merge` | 798 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L798-p3-no-arm-pending-codex` | 798 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L798-p3-open-pr` | 798 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L799-p3-prod-deploy-default` | 799 | Process 3: Isolation, Branching, Verification, PR & Deployment | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L802-p4-notes-mandate` | 802-803 | Process 4: Owner Review Surface via Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L803-p4-title-standard` | 803 | Process 4: Owner Review Surface via Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L804-p4-timestamp-line` | 804 | Process 4: Owner Review Surface via Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L805-p4-html-format` | 805 | Process 4: Owner Review Surface via Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L806-p4-pinning` | 806 | Process 4: Owner Review Surface via Apple Notes | [Apple Notes](../../AGENT-SYNC.md#apple-notes) |
| `L809-p5-use-subagents` | 809 | Process 5: Model Economics & Tiered Model Allocation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L810-p5-right-size` | 810 | Process 5: Model Economics & Tiered Model Allocation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L811-p5-tier1` | 811 | Process 5: Model Economics & Tiered Model Allocation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L812-p5-tier2` | 812 | Process 5: Model Economics & Tiered Model Allocation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L813-p5-tier3` | 813 | Process 5: Model Economics & Tiered Model Allocation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L814-p5-failure-escalation` | 814 | Process 5: Model Economics & Tiered Model Allocation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L817-p6-file-handoff` | 817 | Process 6: Secret Handoff & Credential Security | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L818-p6-infisical-canonical` | 818 | Process 6: Secret Handoff & Credential Security | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L819-p6-token-scope` | 819 | Process 6: Secret Handoff & Credential Security | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L820-p6-safe-secret-cli` | 820 | Process 6: Secret Handoff & Credential Security | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L823-p7-outage-log` | 823 | Process 7: Agent Outage & Capacity Management | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L824-p7-lane-reassign` | 824 | Process 7: Agent Outage & Capacity Management | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L825-p7-recovery` | 825 | Process 7: Agent Outage & Capacity Management | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L828-p8-persistent-scope` | 828 | Process 8: Context Continuity & Scope Retention | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L831-p9-mac-process-inventory` | 831-834 | Process 9: Mac local process inventory | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L837-p10-agents-account-only` | 837 | Process 10: Outbound iMessage account boundary | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L837-p10-jay-account-boundary` | 837 | Process 10: Outbound iMessage account boundary | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L837-p10-no-relay-under-jay` | 837 | Process 10: Outbound iMessage account boundary | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L837-p10-sole-sender` | 837 | Process 10: Outbound iMessage account boundary | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L843-seat-preface-lanes` | 843 | Agent Seat Specifics & Execution Profiles | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L847-seat-ag-identity` | 847 | Seat table: Antigravity (AG) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L847-seat-ag-startup-script` | 847 | Seat table: Antigravity (AG) | retired:  agent-sync-poll.py is retired by the hard cut.  The session-start duty is stated once in L781-p1-startup-poll-duty. |
| `L847-seat-ag-subagents` | 847 | Seat table: Antigravity (AG) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L848-seat-codex-identity` | 848 | Seat table: Codex (CODEX) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L848-seat-codex-quota` | 848 | Seat table: Codex (CODEX) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L848-seat-codex-startup-script` | 848 | Seat table: Codex (CODEX) | retired:  agent-sync-poll.py is retired by the hard cut.  The session-start duty is stated once in L781-p1-startup-poll-duty. |
| `L849-seat-claude-duties` | 849 | Seat table: Claude (CLAUDE) | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L849-seat-claude-identity` | 849 | Seat table: Claude (CLAUDE) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L850-seat-grok` | 850 | Seat table: Grok (GROK) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L851-seat-grok-build` | 851 | Seat table: Grok Build (GROK-BUILD) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L851-seat-grok-build-no-grok-identity` | 851 | Seat table: Grok Build (GROK-BUILD) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L852-seat-monet-retired` | 852 | Seat table: Monet (MONET) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L853-seat-cursor` | 853 | Seat table: Cursor / Copilot (CURSOR) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L854-seat-gb-prefix-docs` | 854 | Seat table: Grok Bot (GB roles) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L854-seat-gb-role` | 854 | Seat table: Grok Bot (GB roles) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L854-seat-gb-superseded` | 854 | Seat table: Grok Bot (GB roles) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L854-seat-gb-tag-list` | 854 | Seat table: Grok Bot (GB roles) | retired:  Slack signing-tag list for Grok Bot roles.  Slack tags end with the hard cut, and any surviving role identity becomes a provisioned Zulip bot (see the BotFleet owner question). |
| `L855-seat-bf-loop` | 855 | Seat table: BotFleet bots (BF-<ROLE>) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L855-seat-bf-role` | 855 | Seat table: BotFleet bots (BF-<ROLE>) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L855-seat-bf-tags` | 855 | Seat table: BotFleet bots (BF-<ROLE>) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L856-seat-renoir-retired` | 856 | Seat table: Renoir (RENOIR) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L857-seat-kimi-retired` | 857 | Seat table: Kimi (KIMI) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L858-seat-deepseek-in-cursor` | 858 | Seat table: DeepSeek Harness (DSH) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L858-seat-dsh-retired` | 858 | Seat table: DeepSeek Harness (DSH) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L859-seat-harness-retired` | 859 | Seat table: Harness (HARNESS) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L860-seat-clutch-config` | 860 | Seat table: Clutch (CLUTCH) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L860-seat-clutch-dsh-import` | 860 | Seat table: Clutch (CLUTCH) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L860-seat-clutch-identity` | 860 | Seat table: Clutch (CLUTCH) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L861-seat-mm-bypass` | 861 | Seat table: MiniMax (MM) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L861-seat-mm-config` | 861 | Seat table: MiniMax (MM) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L861-seat-mm-identity` | 861 | Seat table: MiniMax (MM) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L861-seat-mm-old-tag` | 861 | Seat table: MiniMax (MM) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L861-seat-mm-rules-pointer` | 861 | Seat table: MiniMax (MM) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L861-seat-mm-subagents` | 861 | Seat table: MiniMax (MM) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L862-seat-fx-config` | 862 | Seat table: Fx (FX) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L862-seat-fx-delegation` | 862 | Seat table: Fx (FX) | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L862-seat-fx-full-access` | 862 | Seat table: Fx (FX) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L862-seat-fx-identity` | 862 | Seat table: Fx (FX) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L862-seat-fx-model-never-changes-seat` | 862 | Seat table: Fx (FX) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L863-seat-mc-config` | 863 | Seat table: Muse Code (MC) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L863-seat-mc-identity` | 863 | Seat table: Muse Code (MC) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L863-seat-mc-rules-skills` | 863 | Seat table: Muse Code (MC) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L864-seat-ma-config` | 864 | Seat table: Muse Assist (MA) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L864-seat-ma-identity` | 864 | Seat table: Muse Assist (MA) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L864-seat-ma-tag-migration` | 864 | Seat table: Muse Assist (MA) | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L865-seat-any-universal` | 865 | Seat table: Universal Seat (ANY) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L872-avail-purpose` | 870-876 | Agent availability / outages | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L875-avail-keep-current` | 875-877 | Agent availability / outages | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L877-avail-absolute-times` | 877 | Agent availability / outages | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L880-avail-active-seats` | 880 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L880-avail-kimi-retired` | 880 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L881-avail-monet-renoir-harness-retired` | 881 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L881-avail-retired-readable` | 881 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L882-avail-codex-stale-row` | 882 | Agent availability / outages | retired:  Stale outage note about a 2026-07-19 Codex cap that no longer applies.  Live availability belongs in the outage list, not the protocol. |
| `L882-avail-coolify-writer` | 882 | Agent availability / outages | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L882-avail-oracle-cutover` | 882 | Agent availability / outages | retired:  Completed-migration note (the Oracle cutover finished 2026-08-07).  No behavior depends on it, and the current host fact survives in L882-avail-coolify-writer. |
| `L884-avail-available-normal` | 884-885 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L885-avail-botfleet-gb` | 885 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L885-avail-list` | 885 | Agent availability / outages | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L888-avail-codex-resumed` | 887-889 | Agent availability / outages | retired:  Historical 'Available again' row from 2026-07-08 that also breaks the timestamp rule (CDT/UTC stamps).  Nothing depends on it. |
| `L894-ci-coolify-runners-only` | 894 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L895-ci-mac-runner-banned` | 895 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L895-ci-mac-runner-reason` | 895 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L896-ci-ios-hosted` | 896 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L896-ci-ios-ship-not-installed` | 896 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L896-ci-no-local-substitute` | 896 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L897-ci-reconcile-fixed` | 897 | CI Runner Infrastructure Policy | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L897-ci-reconcile-history` | 897 | CI Runner Infrastructure Policy | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L897-ci-sanity-check` | 897 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L897-ci-violation-detector` | 897 | CI Runner Infrastructure Policy | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L900-avail-row-format` | 900 | Agent availability / outages | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L904-by-seat-zips` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-cursor-signed-monet` | 904 | Fleet Skills Catalog | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L904-home-antigravity` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-home-claude-shared` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-home-codex` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-home-cursor` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-home-deepseek` | 904 | Fleet Skills Catalog | retired:  The ~/.deepseek home belongs to DeepSeek Harness (DSH), retired 2026-09-19.  CLUTCH is the seat now.  The rewriter should confirm against install-fleet-skills.py which home CLUTCH uses. |
| `L904-home-grok` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-home-kimi` | 904 | Fleet Skills Catalog | retired:  The ~/.kimi skills home belongs to KIMI, retired by the owner on 2026-08-21. |
| `L904-home-minimax` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-home-monet-upload` | 904 | Fleet Skills Catalog | retired:  The ~/Desktop/fleet-skills upload location exists only for MONET, which the owner retired on 2026-10-07. |
| `L904-home-renoir` | 904 | Fleet Skills Catalog | retired:  The ~/.renoir skills home belongs to RENOIR, which the owner retired on 2026-10-07. |
| `L904-installer-identity` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-no-copy-monet-pack` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L904-skills-sources` | 904 | Fleet Skills Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L907-skill-fleet-coordination` | 907 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L908-skill-session-start` | 908 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L909-skill-board-ops` | 909 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L910-skill-secret-handoff` | 910 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L911-skill-sentence-gap` | 911 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L912-skill-owner-copy` | 912 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L913-skill-apple-notes` | 913 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L914-skill-land-lane` | 914 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L915-skill-unstick-pr` | 915 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L916-skill-codex-triage` | 916 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L917-skill-pickup-seat` | 917 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L918-skill-deploy-verify` | 918 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L919-skill-fleet-infra` | 919 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L920-dns-zone-account` | 920 | Complete Catalog | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L920-skill-dns` | 920 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L921-skill-mac-cleanup` | 921 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L922-skill-closeout` | 922 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L924-compiler-owns-ios` | 924 | Complete Catalog | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L924-dealdex-hosted` | 924 | Complete Catalog | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L924-ios-ship-omitted` | 924 | Complete Catalog | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L924-kimi-not-installed` | 924 | Complete Catalog | retired:  Install note for KIMI, which the owner retired on 2026-08-21. |
| `L924-no-local-ios-ship` | 924 | Complete Catalog | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L924-renoir-not-installed` | 924 | Complete Catalog | retired:  Its condition ('until the seat is active') can never be met, because the owner retired RENOIR on 2026-10-07. |
| `L926-install-command` | 926-929 | Complete Catalog | [Appendix B: Fleet Skills Catalog](../../AGENT-SYNC.md#appendix-b-fleet-skills-catalog) |
| `L935-handoff-directive` | 933-935 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L937-living-outline` | 937-938 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L939-outline-objective` | 939 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L940-outline-wip` | 940 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L941-outline-done` | 941 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L942-outline-next` | 942 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L943-outline-gotchas` | 943 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L945-outline-to-closeout` | 945 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L947-stop-trigger` | 947 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L949-stop-halt` | 949 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L950-stop-preserve` | 950 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L951-stop-effort-row` | 951 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L952-stop-apple-note` | 952 | Living Handoff Reports & Substitute Agent Protocol | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L955-note-title` | 955 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L956-note-prefix` | 956 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L957-note-acronyms` | 957 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L958-note-tag` | 958 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L959-note-second-line` | 959 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L961-note-sec1` | 961 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L962-note-sec2` | 962 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L963-note-sec3` | 963 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L964-note-sec4` | 964 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L965-note-sec5` | 965 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L966-note-sec6` | 966 | Apple Notes Handoff Report Standard | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L968-sub-notify-duty` | 968-970 | Substitute Agent Direct Slack Notification | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L971-sub-notify-header` | 971-972 | Substitute Agent Direct Slack Notification | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L973-sub-notify-fields` | 973-977 | Substitute Agent Direct Slack Notification | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L979-sub-notify-example` | 979 | Substitute Agent Direct Slack Notification | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L980-sub-notify-purpose` | 980 | Substitute Agent Direct Slack Notification | [Outages, Handoffs, and Substitute Seats](../../AGENT-SYNC.md#outages-handoffs-and-substitute-seats) |
| `L984-merge-enforced` | 984-986 | Merge requirements | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L986-merge-two-conditions` | 986 | Merge requirements | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L990-table-project-a` | 990 | Merge requirements | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L991-table-project-b` | 991 | Merge requirements | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L992-table-cts` | 992 | Merge requirements | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L993-table-usage` | 993 | Merge requirements | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L996-no-blind-resolve` | 996 | Merge requirements | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L996-resolve-threads` | 996 | Merge requirements | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L997-arm-auto-merge` | 997 | Merge requirements | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L998-done-means-merged` | 998 | Merge requirements | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1002-no-merge-pending` | 1000-1002 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1004-pending-definition` | 1004 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1005-protection-gap` | 1005 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1006-automerge-arm-after` | 1006 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1006-automerge-disable` | 1006 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1007-check-once-command` | 1007 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1007-waiting-not-polling` | 1007 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1008-triage-after-codex` | 1008 | No merge while Codex review is pending | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1009-codex-history` | 1009 | No merge while Codex review is pending | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1013-squash-facts` | 1011-1013 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1013-ancestry-false-positive` | 1013 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1013-false-panic-history` | 1013 | Squash-merge vs abandoned-branch detection | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1018-landed-check-commands` | 1015-1022 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1021-branch-landed-helper` | 1021 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1024-ancestry-valid-use` | 1024 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1024-disk-janitor` | 1024 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1024-two-dot-misleading` | 1024 | Squash-merge vs abandoned-branch detection | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1030-idle-watch-ruling` | 1028-1032 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1034-no-polling` | 1034-1036 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1038-action-not-time` | 1038-1040 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1044-row-threads` | 1044 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1045-row-conflict` | 1045 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1046-row-check-failing` | 1046 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1047-row-never-dispatched` | 1047 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1048-row-automerge-unarmed` | 1048 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1049-row-behind` | 1049 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1053-loop-step1` | 1051-1053 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1054-loop-step2` | 1054-1056 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1057-loop-step3` | 1057-1058 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1060-bot-threads-block` | 1060-1062 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1062-threads-wont-self-resolve` | 1062-1064 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1066-background-same-rule` | 1066-1069 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1071-related-skills` | 1071-1072 | Never idle-watch a PR | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1078-coordinator-appointed` | 1076-1078 | Coordinator authority | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1078-coordinator-discipline` | 1078 | Coordinator authority | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1078-coordinator-enforce` | 1078 | Coordinator authority | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1078-coordinator-reassign` | 1078 | Coordinator authority | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1078-owner-supersedes` | 1078 | Coordinator authority | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1078-peers-follow` | 1078 | Coordinator authority | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1082-section-standard` | 1082-1082 | Delegation & model economics | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-fleet-mode-default` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-fleet-mode-doc` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-fleet-mode-platforms` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-fleet-mode-printer` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-fleet-mode-skill` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-fleet-mode-triggers` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1084-policy-home` | 1084-1084 | Delegation & model economics > Fleet mode | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1086-two-goals-equal` | 1086-1086 | Delegation & model economics > Two goals | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1088-goal-spend-less` | 1088-1088 | Delegation & model economics > Two goals | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1089-goal-fit-owner` | 1089-1092 | Delegation & model economics > Two goals | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1091-unavailable-is-cost` | 1091-1092 | Delegation & model economics > Two goals | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1094-say-when-goals-conflict` | 1094-1095 | Delegation & model economics > Two goals | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1097-inline-grind-unreachable` | 1097-1101 | Delegation & model economics > Short turns | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1097-keep-turns-short` | 1097-1097 | Delegation & model economics > Short turns | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1101-end-turns-often` | 1101-1102 | Delegation & model economics > Short turns | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1101-prefer-spawn-and-return` | 1101-1103 | Delegation & model economics > Short turns | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1103-interruption-handling` | 1103-1105 | Delegation & model economics > Short turns | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1107-delegation-protects-work` | 1107-1112 | Delegation & model economics > Interruption protection | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1114-delegate-more-not-ask-less` | 1114-1117 | Delegation & model economics > Interruption protection | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1119-standing-directives-header` | 1119-1119 | Delegation & model economics > Standing directives | retired:  A structural note about the source's inconsistent numbering ('Two standing owner directives' followed by six items).  The rewrite renumbers the directives cleanly, and every directive keeps its own home. |
| `L1121-use-subagents-whenever-help` | 1121-1123 | Delegation & model economics > Directive 1: Use sub-agents | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1123-expected-to-decompose` | 1123-1126 | Delegation & model economics > Directive 1: Use sub-agents | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1125-team-shapes` | 1125-1126 | Delegation & model economics > Directive 1: Use sub-agents | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1127-do-not-serialize` | 1127-1128 | Delegation & model economics > Directive 1: Use sub-agents | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1128-teams-board-reservations` | 1128-1129 | Delegation & model economics > Directive 1: Use sub-agents | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1128-teams-channel-claims` | 1128-1129 | Delegation & model economics > Directive 1: Use sub-agents | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1131-right-size-model` | 1131-1134 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1134-frontier-hands-mechanical-down` | 1134-1135 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1135-mid-escalates-money-path` | 1135-1136 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1137-tier-small` | 1137-1138 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1139-tier-mid` | 1139-1140 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1141-tier-frontier` | 1141-1142 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1142-scope-hard-kernel-small` | 1142-1143 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1144-escalate-on-failed-verification` | 1144-1145 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1146-same-bar-any-model` | 1146-1147 | Delegation & model economics > Directive 2: Right-size the model | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1147-track-record` | 1147-1148 | Delegation & model economics > Directive 2: Right-size the model | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1150-delegate-when-cheaper-can` | 1150-1153 | Delegation & model economics > Directive 3: Delegate to cheaper models | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1155-decision-rule-90pct` | 1155-1158 | Delegation & model economics > Directive 3: Decision rule | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1160-total-tokens-not-price` | 1160-1163 | Delegation & model economics > Directive 3: Decision rule | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1163-cheapness-that-fails` | 1163-1165 | Delegation & model economics > Directive 3: Decision rule | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1164-90pct-real-threshold` | 1164-1165 | Delegation & model economics > Directive 3: Decision rule | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1167-expected-value-rule` | 1167-1170 | Delegation & model economics > Directive 3: Expected value | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1170-one-overrun-not-evidence` | 1170-1171 | Delegation & model economics > Directive 3: Expected value | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1171-no-quiet-inline-reflex` | 1171-1173 | Delegation & model economics > Directive 3: Expected value | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1174-take-the-bet` | 1174-1174 | Delegation & model economics > Directive 3: Expected value | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1176-nontoken-benefit` | 1176-1181 | Delegation & model economics > Directive 3: Availability benefit | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1183-escalate-upward-expected` | 1183-1187 | Delegation & model economics > Directive 3: Escalating upward | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1186-sizing-bidirectional` | 1186-1187 | Delegation & model economics > Directive 3: Escalating upward | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1189-struggle-hand-up-early` | 1189-1192 | Delegation & model economics > Directive 3: Hand it up before struggling | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1192-five-failed-attempts` | 1192-1194 | Delegation & model economics > Directive 3: Hand it up before struggling | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1194-not-contradiction-needs-named-reason` | 1194-1197 | Delegation & model economics > Directive 3: Hand it up before struggling | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1199-30pct-rule-scope` | 1199-1203 | Delegation & model economics > Directive 3: The 30% rule | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1201-30pct-default-hand-off` | 1201-1202 | Delegation & model economics > Directive 3: The 30% rule | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1205-work-out-own-ladder` | 1205-1208 | Delegation & model economics > Directive 3: Own ladder | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1208-do-not-wait-for-table` | 1208-1210 | Delegation & model economics > Directive 3: Own ladder | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1210-unclear-lineup-use-judgement` | 1210-1211 | Delegation & model economics > Directive 3: Own ladder | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1211-state-model-picked` | 1211-1212 | Delegation & model economics > Directive 3: Own ladder | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1214-ladder-both-ways` | 1214-1216 | Delegation & model economics > Directive 3: Own ladder | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1218-ladder-claude-code` | 1218-1219 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1218-ladders-caveat` | 1218-1218 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1219-ladder-codex` | 1219-1220 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1220-ladder-antigravity` | 1220-1220 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1220-ladder-deepseek` | 1220-1221 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1221-ladder-cursor` | 1221-1222 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1221-ladder-kimi` | 1221-1221 | Delegation & model economics > Known ladders | retired:  Ladder for KIMI, which the owner retired on 2026-08-21. |
| `L1222-ladder-minimax` | 1222-1222 | Delegation & model economics > Known ladders | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1224-grok-exemption` | 1224-1226 | Delegation & model economics > Grok exemption | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1226-exemption-narrow` | 1226-1227 | Delegation & model economics > Grok exemption | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1227-other-agents-exempt` | 1227-1228 | Delegation & model economics > Grok exemption | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1230-same-tier-still-pays` | 1230-1232 | Delegation & model economics > Same-tier delegation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1232-why-same-tier-saves` | 1232-1236 | Delegation & model economics > Same-tier delegation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1236-same-tier-worker-cheaper` | 1236-1238 | Delegation & model economics > Same-tier delegation | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1240-sharp-test-question` | 1240-1241 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1242-not-read-yet-delegate` | 1242-1245 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1246-already-in-context-inline` | 1246-1247 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1248-strongest-case` | 1248-1250 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1250-weakest-case` | 1250-1251 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1252-grok-large-context` | 1252-1254 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1254-exempt-thinner-margins` | 1254-1257 | Delegation & model economics > Sharp test | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1259-only-exception-costs-more` | 1259-1263 | Delegation & model economics > When not to delegate | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1263-mechanical-delegation-wins` | 1263-1266 | Delegation & model economics > When not to delegate | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1268-brief-thoroughly` | 1268-1270 | Delegation & model economics > Briefing | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1270-brief-contents` | 1270-1273 | Delegation & model economics > Briefing | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1273-mark-established-facts` | 1273-1276 | Delegation & model economics > Briefing | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1278-only-needed-tools` | 1278-1281 | Delegation & model economics > Tools | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1280-search-report-readonly-tools` | 1280-1281 | Delegation & model economics > Tools | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1283-assume-possible` | 1283-1285 | Delegation & model economics > Tools | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1285-grok-bot-proof` | 1285-1287 | Delegation & model economics > Tools | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1287-claude-code-agent-defs` | 1287-1289 | Delegation & model economics > Tools | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1289-registry-caveat` | 1289-1291 | Delegation & model economics > Tools | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1291-look-for-mechanism-record-it` | 1291-1292 | Delegation & model economics > Tools | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1294-then-stay-available` | 1294-1296 | Delegation & model economics > Stay available | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1295-idle-is-free` | 1295-1297 | Delegation & model economics > Stay available | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1297-no-block-main-loop` | 1297-1298 | Delegation & model economics > Stay available | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1299-cache-expiry-caveat` | 1299-1300 | Delegation & model economics > Stay available | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1302-supervise-on-evidence` | 1302-1303 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1303-thrash-triggers` | 1303-1304 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1304-thrash-exception` | 1304-1305 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1305-step-in-remedies` | 1305-1308 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1307-looping-most-expensive` | 1307-1309 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1309-escalate-looper-ok` | 1309-1310 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1312-never-poll` | 1312-1315 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1317-supervision-piggybacks` | 1317-1321 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1321-single-bounded-look` | 1321-1322 | Delegation & model economics > Directive 4: Supervise | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1324-worktrees-when-needed` | 1324-1327 | Delegation & model economics > Directive 5: Worktrees | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1327-worktree-create-cases` | 1327-1329 | Delegation & model economics > Directive 5: Worktrees | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1330-worktree-avoid-reflex` | 1330-1332 | Delegation & model economics > Directive 5: Worktrees | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1332-never-stack-worktrees` | 1332-1334 | Delegation & model economics > Directive 5: Worktrees | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1334-worktree-cost` | 1334-1336 | Delegation & model economics > Directive 5: Worktrees | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1338-hook-enforced` | 1338-1339 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1339-hook-deny-no-model` | 1339-1340 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1340-hook-deny-doubled-worktree` | 1340-1341 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1340-hook-deny-foreground` | 1340-1340 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1340-hook-deny-unknown-tier` | 1340-1340 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1341-hook-deny-all-frontier` | 1341-1341 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1341-hook-deny-workflow-no-model` | 1341-1341 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1342-why-hook-exists` | 1342-1344 | Delegation & model economics > Directive 6: Hook enforcement | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1344-secret-guard-comparison` | 1344-1345 | Delegation & model economics > Directive 6: Hook enforcement | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1345-deny-message-content` | 1345-1346 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1346-hook-processes-row` | 1346-1347 | Delegation & model economics > Directive 6: Hook enforcement | [Delegation and Model Economics](../../AGENT-SYNC.md#delegation-and-model-economics) |
| `L1351-terse` | 1351 | Message Structure | [Posting](../../AGENT-SYNC.md#posting) |
| `L1353-post-start-end` | 1353-1357 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1355-post-on-change` | 1355-1356 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1357-silent-work` | 1357 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1359-claim-effort-board` | 1359-1361 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1360-claim-github-issue` | 1360-1361 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1361-claim-slack` | 1361-1362 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1362-claim-date-slack` | 1362-1363 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1363-claim-date-board-where` | 1363-1364 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1364-claim-date-effort-row` | 1364-1365 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1365-created-at-not-claim` | 1365 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1366-closeout-board` | 1366-1367 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1367-closeout-issue` | 1367-1368 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1368-board-issue-parity` | 1368-1369 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1368-closeout-slack` | 1368 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1369-effort-protocol-ref` | 1369-1370 | ALWAYS update peers in Slack | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1372-header-required` | 1372 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1374-sender-forms` | 1374-1376 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1374-sender-required` | 1374 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1375-afc-signs` | 1375-1376 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1377-repo-first-field` | 1377 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1378-canonical-repo-names` | 1378-1379 | ALWAYS update peers in Slack | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1380-recipient-optional` | 1380-1381 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1382-fleet-wake-scope` | 1382-1385 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1384-fleet-use-only` | 1384-1385 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1386-peer-reaches-all` | 1386-1387 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1387-grok-fleet-history` | 1387-1388 | ALWAYS update peers in Slack | retired:  History of a reading the owner already retired on 2026-09-13.  The live ruling stays in L1384-fleet-use-only, and the Zulip fleet user group replaces FLEET semantics. |
| `L1389-fleet-not-routine` | 1389-1390 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1389-fleet-not-sender` | 1389 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1391-afc-self` | 1391 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1393-forbid-bare-fleet` | 1393-1394 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1393-forbid-missing-repo` | 1393 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1393-forbid-no-sender` | 1393 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1394-forbid-coordinator-fleet` | 1394 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1394-forbid-fleet-wip` | 1394-1395 | ALWAYS update peers in Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1397-read-mandatory` | 1397-1399 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1401-prefer-live` | 1401-1402 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1402-poll-fallback` | 1402-1404 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1404-state-cadence-intro` | 1404 | ALWAYS read Slack | [Posting](../../AGENT-SYNC.md#posting) |
| `L1405-read-at-start-end` | 1405-1406 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1407-skim-header` | 1407 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1408-match-fleet` | 1408 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1409-match-tag` | 1409 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1410-match-repo` | 1410 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1411-no-match-stop` | 1411-1412 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1413-peer-data-not-owner` | 1413-1414 | ALWAYS read Slack | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1415-untrusted-delimiters` | 1415-1416 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1417-never-execute` | 1416-1417 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1417-poll-filter` | 1417-1418 | ALWAYS read Slack | retired:  Describes the retired agent-sync-poll.py filter.  The keyword minimum it encoded is kept in L2106-grep-minimum. |
| `L1419-agent-repo-env` | 1418-1419 | ALWAYS read Slack | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1421-auth-mac` | 1421-1422 | ALWAYS read Slack | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1426-two-tiers` | 1426 | Multi-room & active collaboration (slack-collab) | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1427-tier1-macro` | 1427 | Multi-room & active collaboration (slack-collab) | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1428-tier2-app-channels` | 1428 | Multi-room & active collaboration (slack-collab) | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1428-use-threads` | 1428 | Multi-room & active collaboration (slack-collab) | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1431-slack-collab-mcp` | 1431 | Multi-room & active collaboration (slack-collab) | retired:  The slack-collab MCP server is retired by the hard cut and replaced by the agent-sync CLI (L1432-cli-helper). |
| `L1432-cli-helper` | 1432 | Multi-room & active collaboration (slack-collab) | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1433-relay-daemon` | 1433 | Multi-room & active collaboration (slack-collab) | retired:  The pm2 agent-sync-push relay is retired by the hard cut.  Zulip server push plus `agent-sync listen` replaces it. |
| `L1438-header-plain` | 1438-1440 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1442-header-directed` | 1442-1445 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1446-header-fleet` | 1446-1449 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1450-header-afc` | 1450-1453 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1455-repo-first` | 1455-1457 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1457-multi-repo` | 1457 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1459-sender-always` | 1459 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1459-tags-identity-based` | 1459-1462 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1461-fable-history` | 1461-1462 | Header | retired:  Trivia that early posts used the tag FABLE for CLAUDE.  Fable is a model name, and the seat is CLAUDE. |
| `L1462-state-capabilities` | 1462-1463 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1463-two-accounts` | 1463-1465 | Header | retired:  Describes the separate CLAUDE and MONET accounts.  It is superseded by the owner's 2026-10-07 ruling retiring MONET and RENOIR and leaving CLAUDE as the only Claude seat. |
| `L1466-seat-cloud` | 1466-1470 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1471-seat-local-no-signal` | 1471-1475 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1475-seat-precedence` | 1475-1477 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1478-never-flip-on-inference` | 1478-1481 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1480-pingpong-history` | 1480-1481 | Header | retired:  The incident note about CLAUDE and MONET ping-pong involves a seat the owner retired on 2026-10-07.  The lesson survives in L1478-never-flip-on-inference. |
| `L1481-hooks-no-rebrand` | 1481 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1482-tag-ag` | 1482 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1482-tag-codex` | 1482 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1482-tag-cursor` | 1482 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1482-tag-grok` | 1482 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1482-tag-grok-build` | 1482 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1482-tag-kimi` | 1482 | Header | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1483-new-agent-intro` | 1483-1484 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1483-new-agent-tag` | 1483 | Header | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L1485-recipient-omit` | 1485-1486 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1486-recipient-fleet` | 1486-1488 | Header | [Posting](../../AGENT-SYNC.md#posting) |
| `L1489-sync-n` | 1489-1490 | Header | retired:  The optional per-session `sync-N` serial counter is redundant once each unit of work has its own Zulip topic and sessions carry a `[SEAT·session8]` tag. |
| `L1494-body-compact` | 1494 | Body | [Posting](../../AGENT-SYNC.md#posting) |
| `L1497-field-claim` | 1497 | Body | [Posting](../../AGENT-SYNC.md#posting) |
| `L1498-field-claimed` | 1498 | Body | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1499-field-state` | 1499 | Body | [Posting](../../AGENT-SYNC.md#posting) |
| `L1500-field-keepout` | 1500 | Body | [Posting](../../AGENT-SYNC.md#posting) |
| `L1501-field-collision` | 1501 | Body | [Posting](../../AGENT-SYNC.md#posting) |
| `L1502-field-ack-counter` | 1502 | Body | [Posting](../../AGENT-SYNC.md#posting) |
| `L1508-example-claim-keepout` | 1508-1512 | Body | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L1515-example-collision` | 1515-1518 | Body | retired:  This example names retired MONET and uses a FLEET wake for a single-file collision, which contradicts the owner's 2026-09-13 FLEET ruling.  The collision example is kept once as L1959-collision-post. |
| `L1521-example-blocked` | 1521-1525 | Body | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L1529-reactions-lightweight` | 1529 | Reactions | [Posting](../../AGENT-SYNC.md#posting) |
| `L1530-react-ack` | 1530 | Reactions | [Posting](../../AGENT-SYNC.md#posting) |
| `L1531-react-coordinate` | 1531 | Reactions | [Posting](../../AGENT-SYNC.md#posting) |
| `L1532-react-merge` | 1532 | Reactions | [Posting](../../AGENT-SYNC.md#posting) |
| `L1533-react-warn` | 1533 | Reactions | [Posting](../../AGENT-SYNC.md#posting) |
| `L1540-with-slack` | 1539-1540 | Access & Reading | retired:  Slack app or CLI access is retired by the hard cut.  Seats use the agent-sync CLI with their own Zulip bot credentials (L1421-auth-mac). |
| `L1543-tunnel-post` | 1543-1546 | Access & Reading | retired:  The agent-sync.jays.services Slack tunnel POST endpoint is retired by the hard cut.  Cloud seats use ZULIP_EMAIL, ZULIP_API_KEY and ZULIP_SITE with the agent-sync CLI. |
| `L1546-tunnel-token-isolation` | 1546-1548 | Access & Reading | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1549-local-helper` | 1549-1551 | Access & Reading | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1552-direct-api-last-resort` | 1552-1553 | Access & Reading | retired:  Direct Slack API posting with SLACK_BOT_TOKEN is retired by the hard cut. |
| `L1554-read-only-access` | 1554-1555 | Access & Reading | retired:  The shared read-only Slack bot env file is retired.  Each seat has its own Zulip bot credentials. |
| `L1559-relay-primary` | 1559-1563 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1567-ws-relay-daemon` | 1565-1572 | Real-time Sync | retired:  The Slack Socket Mode relay daemon on ws://127.0.0.1:8787 is retired by the hard cut. |
| `L1574-pm2-events-jsonl` | 1574-1575 | Real-time Sync | retired:  The relay's events.jsonl log is retired along with the pm2 agent-sync-push relay. |
| `L1575-no-per-agent-socket` | 1575-1577 | Real-time Sync | retired:  This Slack Socket Mode event-distribution hazard does not apply to Zulip's per-client event queues. |
| `L1578-relay-post-endpoint` | 1578 | Real-time Sync | retired:  The relay's authenticated POST /post endpoint is retired with the relay. |
| `L1580-consumer-optional` | 1580-1587 | Real-time Sync | retired:  consumer.mjs is retired by the hard cut and replaced by `agent-sync listen`. |
| `L1587-no-pm2-consumer` | 1587 | Real-time Sync | retired:  This rule about persistent consumer.mjs processes is moot because consumer.mjs is retired. |
| `L1590-feat-no-polling` | 1590 | Real-time Sync | retired:  This describes a feature of the retired Slack relay. |
| `L1591-feat-cursor` | 1591-1592 | Real-time Sync | retired:  The private cursor file and events.jsonl replay belong to the retired relay.  Any replay behavior now comes from `agent-sync inbox` and `agent-sync read`. |
| `L1593-feat-selffilter` | 1593 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1594-feat-reconnect` | 1594 | Real-time Sync | retired:  This describes a feature of the retired Slack relay. |
| `L1596-selffilter-required` | 1596-1599 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1600-self-app-delivered` | 1600-1603 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1604-startswith-never-matches` | 1604-1608 | Real-time Sync | retired:  A Slack-only bug note about start-of-string self filters on one shared bot.  Zulip identifies the sender bot, and session separation uses the `[SEAT·session8]` tag. |
| `L1611-posters-tag-in-80` | 1611-1612 | Real-time Sync | [Posting](../../AGENT-SYNC.md#posting) |
| `L1613-consumers-single-session` | 1613-1616 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1615-substring-precision` | 1615-1616 | Real-time Sync | retired:  A Slack self-filter example that uses retired MONET.  The principle is covered in L1596-selffilter-required. |
| `L1617-multi-session-no-filter` | 1617-1621 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1622-cadence-vocabulary` | 1622-1623 | Real-time Sync | [Posting](../../AGENT-SYNC.md#posting) |
| `L1625-ws-oneshot-helper` | 1625-1630 | Real-time Sync | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1632-no-long-watcher` | 1632-1633 | Real-time Sync | retired:  The long-lived watcher mode of the retired agent-sync-websocket.py helper is moot. |
| `L1635-poller-intro` | 1635-1642 | Real-time Sync | retired:  agent-sync-poll.py is retired by the hard cut.  `agent-sync read` and `agent-sync inbox` replace it. |
| `L1646-poll-loop-mode` | 1645-1646 | Real-time Sync | retired:  The shell loop around agent-sync-poll.py is retired.  `agent-sync listen` or `agent-sync wait` replaces it. |
| `L1647-poll-turn-mode` | 1647-1649 | Real-time Sync | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L1650-poll-cloud-mode` | 1650-1651 | Real-time Sync | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1653-poller-truncation` | 1653-1654 | Real-time Sync | retired:  The poller's 600-character truncation note is moot because the poller is retired.  Full reads go through the agent-sync CLI. |
| `L1660-peer-not-owner` | 1660 | Conflict Resolution | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1662-contradiction-no-execute` | 1660-1665 | Conflict Resolution | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1665-surface-conflict` | 1665-1666 | Conflict Resolution | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1667-owner-decides` | 1667 | Conflict Resolution | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1673-keep-board-issues-matching` | 1673 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1674-effort-protocol-canonical` | 1674 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1677-start-board-row` | 1676-1678 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1679-start-issue-claim` | 1679-1681 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1682-start-slack-claim` | 1682 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1683-prefer-not-fleet` | 1682-1683 | Effort Board + GitHub Issues Integration | [Posting](../../AGENT-SYNC.md#posting) |
| `L1686-end-board-state` | 1685-1686 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1687-end-issue-state` | 1687 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1688-end-slack-closeout` | 1688 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1690-never-completed-before-merge` | 1690-1691 | Effort Board + GitHub Issues Integration | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1690-state-order` | 1690 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1691-never-leave-in-progress` | 1691 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1694-ex-step1-planned-to-inprogress` | 1693-1694 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1695-ex-step2-land-mirror` | 1695 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1696-ex-step3-slack-claim` | 1696 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1697-ex-step4-honest-status` | 1697 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1698-ex-step5-finish` | 1698 | Effort Board + GitHub Issues Integration | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1702-board-primary` | 1702-1705 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1706-board-first-place` | 1706-1707 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1709-board-scope` | 1709-1710 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1710-board-sync-cadence` | 1710-1711 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1711-board-hosting` | 1711-1713 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1715-use-board-cli` | 1715 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1718-cmd-board-stats` | 1718 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1719-cmd-board-list` | 1719 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1720-cmd-board-list-mine` | 1720 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1721-cmd-board-show` | 1721 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1723-cmd-board-file` | 1723-1724 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1725-cmd-board-claim` | 1725 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1726-cmd-board-comment` | 1726 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1727-cmd-board-status` | 1727 | THE BOARD | [Appendix A: Reference Tables](../../AGENT-SYNC.md#appendix-a-reference-tables) |
| `L1730-board-cli-path` | 1730 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1731-board-token-source` | 1731-1732 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1733-board-allowlisted` | 1732-1733 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1734-never-paste-token` | 1733-1734 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1736-invoke-literally` | 1736 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1737-allow-once-forever` | 1736-1740 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1740-owner-hit-allow-once` | 1740 | THE BOARD | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1741-old-secret-dance-avoid` | 1741-1742 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1742-wrap-secret-in-cli` | 1742-1744 | THE BOARD | [Secrets and Credentials](../../AGENT-SYNC.md#secrets-and-credentials) |
| `L1744-raw-rest-endpoints` | 1744-1746 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1746-prefer-cli-on-mac` | 1746 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1748-human-board-url` | 1748-1749 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1749-board-short-link` | 1749-1750 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1750-page-gated` | 1750-1751 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1751-new-item-composer` | 1751-1752 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1754-login-fallback` | 1754-1757 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1757-per-seat-token` | 1757-1759 | THE BOARD | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1759-github-outbox-bridge` | 1759-1761 | THE BOARD | [Chat: The Zulip Contract](../../AGENT-SYNC.md#chat-the-zulip-contract) |
| `L1765-before-work-board-list` | 1765 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1766-claim-or-file` | 1766-1768 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1768-claim-date-in-where` | 1768-1769 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1769-claim-fields` | 1769-1771 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1771-keep-claim-accurate` | 1771 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1772-done-status-resolution` | 1772-1773 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1774-never-leave-in-progress` | 1773-1774 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1775-comment-on-peer-items` | 1775-1776 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1776-peer-review-expected` | 1776-1777 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1779-no-manual-effort-updates` | 1779-1780 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1780-writeback-process` | 1780-1781 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1782-land-mirror-in-app-pr` | 1781-1783 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1783-board-write-surface` | 1783 | THE BOARD - What every seat owes | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1787-kind-agent-report` | 1787-1789 | THE BOARD - Item kinds | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1790-kind-review-finding` | 1790-1791 | THE BOARD - Item kinds | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1792-kind-effort-row` | 1792-1793 | THE BOARD - Item kinds | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1794-kind-github-issue` | 1794-1795 | THE BOARD - Item kinds | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1797-writeback-grace-window` | 1797-1800 | THE BOARD - Item kinds | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1800-report-finding-board-authoritative` | 1800-1801 | THE BOARD - Item kinds | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1805-seat-marks` | 1805-1807 | THE BOARD - Seats, and who is actually who | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1809-claude-instances-names` | 1809-1810 | THE BOARD - Seats, and who is actually who | retired:  States that Monet and Renoir keep their own seat names on board marks.  The owner retired both on 2026-10-07, and CLAUDE is the only Claude seat. |
| `L1811-grok-bot-own-seat` | 1811-1812 | THE BOARD - Seats, and who is actually who | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1813-cursor-renders-both-marks` | 1812-1814 | THE BOARD - Seats, and who is actually who | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1814-cursor-chat-surfaces` | 1814-1815 | THE BOARD - Seats, and who is actually who | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1817-env-mac-or-cloud` | 1817-1818 | THE BOARD - Seats, and who is actually who | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1822-two-way-sync-dates` | 1822 | THE BOARD - Relationship to effort boards and GitHub Issues | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1824-forward-sync` | 1824-1825 | THE BOARD - Relationship to effort boards and GitHub Issues | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1826-back-sync` | 1826-1828 | THE BOARD - Relationship to effort boards and GitHub Issues | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1828-writeback-limits` | 1828-1829 | THE BOARD - Relationship to effort boards and GitHub Issues | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1830-fallback-sync-script` | 1830-1831 | THE BOARD - Relationship to effort boards and GitHub Issues | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1833-board-is-write-surface` | 1833 | THE BOARD - Relationship to effort boards and GitHub Issues | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1834-seats-without-board-use-copies` | 1834-1835 | THE BOARD - Relationship to effort boards and GitHub Issues | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1839-recall-scope` | 1839 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1841-recall-at-turn-start` | 1841-1842 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1842-recall-rationale` | 1842-1844 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1844-recall-first-tool-call` | 1844-1845 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1845-recall-before-actions` | 1845-1847 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1847-hit-is-lead-1` | 1847 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1850-four-triggers-evaluate` | 1849-1850 | Fleet recall - 4 Mandatory Search Triggers | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1851-trigger1-error` | 1851 | Fleet recall - 4 Mandatory Search Triggers | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1852-trigger2-infra` | 1852 | Fleet recall - 4 Mandatory Search Triggers | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1853-trigger3-crossrepo` | 1853 | Fleet recall - 4 Mandatory Search Triggers | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1854-trigger4-pre-owner` | 1854 | Fleet recall - 4 Mandatory Search Triggers | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1857-permitted-bypass` | 1856-1857 | Fleet recall - 4 Mandatory Search Triggers | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1859-fleet-agents-collection` | 1859-1863 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1862-contribute-after-learning` | 1862-1863 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1865-mcp-registered-seats` | 1865-1866 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1867-recall-cli-paths` | 1867-1868 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1868-recall-cli-search` | 1868 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1869-recall-cli-contribute` | 1869 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1869-recall-cli-stats-doctor` | 1869 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1870-recall-cloud-access` | 1870 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1871-hit-is-lead-open-source` | 1871-1872 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1872-contribute-every-lesson` | 1872-1874 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1873-search-then-contribute` | 1873-1874 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1874-facts-with-home-stay-home` | 1874-1875 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1875-no-bulk-chat-ingest` | 1875-1876 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1875-no-secrets-transcripts` | 1875 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1877-no-st-embed-fleet-endpoint` | 1877-1878 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1877-recall-canonical-doc` | 1877 | Fleet recall | [Fleet Recall](../../AGENT-SYNC.md#fleet-recall) |
| `L1882-no-start-without-claim` | 1882-1883 | Prohibited Behavior | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1883-no-finish-without-complete` | 1883 | Prohibited Behavior | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1884-no-board-issue-drift` | 1884-1885 | Prohibited Behavior | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1886-no-silence-in-slack` | 1886-1888 | Prohibited Behavior | [Posting](../../AGENT-SYNC.md#posting) |
| `L1887-board-alone-not-enough` | 1888 | Prohibited Behavior | [Posting](../../AGENT-SYNC.md#posting) |
| `L1889-no-free-prose` | 1889-1890 | Prohibited Behavior | [Posting](../../AGENT-SYNC.md#posting) |
| `L1890-fleet-not-a-sender` | 1890 | Prohibited Behavior | [Posting](../../AGENT-SYNC.md#posting) |
| `L1890-fleet-only-when-all-must-act` | 1890-1891 | Prohibited Behavior | [Posting](../../AGENT-SYNC.md#posting) |
| `L1891-afc-signs` | 1891 | Prohibited Behavior | [Posting](../../AGENT-SYNC.md#posting) |
| `L1892-no-channel-reservation` | 1892 | Prohibited Behavior | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1893-peer-not-approval` | 1893-1895 | Prohibited Behavior | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1896-edit-others-rows-disclose` | 1896-1897 | Prohibited Behavior | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1897-correction-note-example` | 1897-1898 | Prohibited Behavior | [THE BOARD and the Claim Lifecycle](../../AGENT-SYNC.md#the-board-and-the-claim-lifecycle) |
| `L1904-owner-directive-precedence` | 1902-1906 | Owner Directives | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1908-surface-stale-or-contradiction` | 1908-1909 | Owner Directives | [Absolute Rules and Authority](../../AGENT-SYNC.md#absolute-rules-and-authority) |
| `L1913-production-deploys-pointer` | 1911-1913 | Owner Directives - Production deploys | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L1913-production-deploys-moved` | 1913 | Owner Directives - Production deploys | retired:  Doc-housekeeping history (the text moved out of the always-loaded doc on 2026-09-01).  The binding pointer survives in L1913-production-deploys-pointer. |
| `L1915-no-new-github-repos` | 1915-1919 | Owner Directives - No new GitHub repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L1919-no-repos-owner-quote` | 1919-1920 | Owner Directives - No new GitHub repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L1921-no-repos-trigger-history` | 1921-1923 | Owner Directives - No new GitHub repositories | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1925-upstream-pr-ask-first` | 1925 | Owner Directives - No new GitHub repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L1926-public-feed-use-releases` | 1926 | Owner Directives - No new GitHub repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L1927-second-thing-folder` | 1927 | Owner Directives - No new GitHub repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L1928-extra-repo-surface` | 1928 | Owner Directives - No new GitHub repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L1930-own-prefix-own-worktree` | 1930-1932 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1931-naming-history` | 1931-1932 | Owner Directives - Branch & worktree naming | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1937-table-claude` | 1937 | Owner Directives - Branch & worktree naming | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1938-table-monet` | 1938 | Owner Directives - Branch & worktree naming | retired:  Branch and worktree row for MONET, which the owner retired on 2026-10-07. |
| `L1939-table-codex` | 1939 | Owner Directives - Branch & worktree naming | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1940-table-ag` | 1940 | Owner Directives - Branch & worktree naming | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1942-no-other-seat-prefix` | 1942 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1943-seat-tag-matches-prefix` | 1943 | Owner Directives - Branch & worktree naming | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1944-undetermined-ask-owner` | 1944-1946 | Owner Directives - Branch & worktree naming | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1945-sessionstart-hook-enforces` | 1945-1946 | Owner Directives - Branch & worktree naming | [Seats and Identity](../../AGENT-SYNC.md#seats-and-identity) |
| `L1947-always-own-worktree` | 1947-1948 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1948-no-shared-checkout` | 1948-1949 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1949-shared-checkout-history` | 1949-1950 | Owner Directives - Branch & worktree naming | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L1950-lane-new-command` | 1950-1951 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1951-lane-path-nested` | 1951-1952 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1952-branch-own-prefix-work-there` | 1952-1953 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1952-flat-lanes-stay` | 1952 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1953-shared-checkout-readonly` | 1953 | Owner Directives - Branch & worktree naming | [Lanes, Worktrees, and Branches](../../AGENT-SYNC.md#lanes-worktrees-and-branches) |
| `L1959-collision-post` | 1959-1967 | Examples | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L1969-collision-ack` | 1969-1975 | Examples | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L1977-unblock-post` | 1977-1986 | Examples | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L1988-unblock-reply` | 1988-1993 | Examples | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L1995-broadcast-claim` | 1995-2000 | Examples | retired:  This example uses a FLEET wake for a routine session-start claim, which contradicts the owner's 2026-09-13 FLEET ruling.  It also names retired MONET.  The claim example is kept once as L1508-example-claim-keepout. |
| `L2002-broadcast-polling-eta` | 2002 | Examples | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L2003-keepout-example` | 2003 | Examples | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L2010-observability-split` | 2008-2012 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2012-rollout-doc-adoption` | 2012-2013 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2013-rollout-doc-org` | 2013-2015 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2015-rollout-doc-max-features` | 2015-2016 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2016-default-full-sentry` | 2016-2017 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2017-designer-no-project` | 2017-2019 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2019-designer-replay-rates` | 2019-2020 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2020-designer-android` | 2020-2021 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2020-designer-seer-botfleet` | 2020 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2021-kill-switches` | 2021-2022 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2023-workflow-filters-410` | 2023-2024 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2024-detector-ids` | 2024-2025 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2025-pagerduty-workflow` | 2025-2026 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2027-no-second-pagerduty` | 2027-2028 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2027-slack-workflow` | 2027 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2030-sentry-org` | 2030 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2030-sentry-projects` | 2030-2031 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2034-no-sentry-project` | 2034-2037 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2037-no-window-error-project` | 2037-2038 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2038-no-project-cts` | 2038 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2039-no-project-fleet-ops` | 2039 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2041-android-sdk` | 2041-2043 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2043-no-android-without-track` | 2043-2044 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2046-seer-quota` | 2046-2047 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2046-seer-autofix-botfleet` | 2047-2048 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2048-seer-slack-notify` | 2048-2049 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2049-no-seer-bot-seats` | 2049-2050 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2051-seer-findings` | 2051 | Observability (Sentry + Datadog, all agents) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2055-datadog-vs-sentry-doc` | 2053-2055 | Datadog vs Sentry (do not double-pay) | [Observability: Sentry and Datadog](../../AGENT-SYNC.md#observability-sentry-and-datadog) |
| `L2055-datadog-doc-moved` | 2055 | Datadog vs Sentry (do not double-pay) | retired:  Doc-housekeeping history (the text moved out of the always-loaded doc on 2026-09-01).  The binding pointer survives in L2055-datadog-vs-sentry-doc. |
| `L2059-onboarding-procedure` | 2057-2060 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2061-onboard-app-local` | 2061 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2062-onboard-app-github` | 2062 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2063-onboard-seat-local` | 2063 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2064-onboard-seat-github` | 2064 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2065-fleet-apps-registry` | 2065 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2066-template-agents` | 2066 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2068-stanza-add-verbatim` | 2068-2070 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2071-stanza-slack-channel` | 2070-2071 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2072-stanza-protocol-pointer` | 2072-2073 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2073-stanza-reserve-effort-board` | 2073-2074 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2074-stanza-peer-messages` | 2074 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2076-global-configs-point-here` | 2076-2078 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2078-first-commit-stanza` | 2078-2079 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2079-effort-log-protocol` | 2079-2081 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2083-codex-audit-helper` | 2083-2085 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2085-codex-audit-apply` | 2084-2085 | Onboarding a new app/repo (self-propagation rule) | [Onboarding New Apps and Seats](../../AGENT-SYNC.md#onboarding-new-apps-and-seats) |
| `L2091-questions-pointer` | 2089-2092 | Questions? | [How to Use This Document](../../AGENT-SYNC.md#how-to-use-this-document) |
| `L2094-watcher-heading-rulings` | 2094 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2096-must-receive-channel` | 2096-2098 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2100-skim-match` | 2100-2102 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2101-skim-fleet` | 2101 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2103-untrusted-wrapper` | 2103 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2105-prefer-live-watcher` | 2105 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2106-grep-minimum` | 2105-2108 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2107-grep-fleet` | 2108 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2108-irrelevant-wake-one-line` | 2108-2109 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2108-update-filter-terms` | 2108 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2111-fleet-wake-meaning` | 2111-2113 | Watcher noise discipline | [Posting](../../AGENT-SYNC.md#posting) |
| `L2113-fleet-sparingly` | 2113-2115 | Watcher noise discipline | [Posting](../../AGENT-SYNC.md#posting) |
| `L2114-fleet-full-read` | 2114-2115 | Watcher noise discipline | [Reading and Listening](../../AGENT-SYNC.md#reading-and-listening) |
| `L2115-fleet-old-reading-retired` | 2115-2116 | Watcher noise discipline | retired:  History of a reading the owner already retired on 2026-09-13.  The live ruling stays in L1384-fleet-use-only, and the Zulip fleet user group replaces it. |
| `L2117-afc-signoff` | 2117 | Watcher noise discipline | [Posting](../../AGENT-SYNC.md#posting) |
| `L2117-routine-claims-tag-repo` | 2117-2118 | Watcher noise discipline | [Posting](../../AGENT-SYNC.md#posting) |
| `L2120-gates-ruling` | 2120 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2122-gates-why` | 2122-2125 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2124-gates-observed-load` | 2123-2124 | Serialize local gates | [Appendix D: History and Incidents](../../AGENT-SYNC.md#appendix-d-history-and-incidents) |
| `L2125-gates-scope` | 2125 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2127-gating-now` | 2127 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2128-wait-for-gate-clear` | 2128-2129 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2129-gate-stale-30` | 2129-2130 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2131-gate-clear` | 2131 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2132-gate-exempt` | 2132 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2133-gate-flake-load` | 2133-2134 | Serialize local gates | [Builds on the Mac: iOS Loop, Mac Apps, Gate Serialization](../../AGENT-SYNC.md#builds-on-the-mac-ios-loop-mac-apps-gate-serialization) |
| `L2138-mcp-scope` | 2136-2138 | MCP Server Configuration (Fleetwide) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2140-mcp-desktop-seats` | 2140-2142 | MCP Server Configuration (Fleetwide) | retired:  The Parall per-seat desktop config rule names only MONET and RENOIR, both retired on 2026-10-07.  Memory still lists ~/Library/Application Support/Parall/Monet/vm_bundles/claudevm.bundle as protected live infra, so the owner should confirm the Parall desktop install is truly out of service before this rule is dropped.  (needs owner) |
| `L2143-mcp-claude-code` | 2143-2144 | MCP Server Configuration (Fleetwide) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2145-mcp-per-repo` | 2145-2146 | MCP Server Configuration (Fleetwide) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2148-mcp-dead-paths` | 2148 | MCP Server Configuration (Fleetwide) | retired:  Warns against ~/.monet/mcp.json and ~/.renoir/mcp.json.  Both seats are retired, so the warning is moot. |
| `L2150-openrouter-no-mcp` | 2150-2155 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2155-openrouter-why` | 2155-2157 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2157-openrouter-no-mcp-needed` | 2157-2158 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2160-openrouter-no-minting` | 2160-2161 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2161-openrouter-key-name` | 2161-2162 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2163-openrouter-workspaces` | 2163-2164 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2166-openrouter-admin-key` | 2166-2168 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2168-openrouter-app-keys` | 2168-2169 | OpenRouter: no MCP, no minting keys, admin is analytics-only | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2173-strict-ci-problem` | 2173-2174 | PR Queue Saturation Mitigation (Strict CI) | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2175-auto-update-prs` | 2175-2176 | PR Queue Saturation Mitigation (Strict CI) | [Infrastructure and CI](../../AGENT-SYNC.md#infrastructure-and-ci) |
| `L2178-auto-update-trigger` | 2178-2189 | PR Queue Saturation Mitigation (Strict CI) | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L2194-auto-update-params` | 2194-2200 | PR Queue Saturation Mitigation (Strict CI) | [Appendix C: Examples](../../AGENT-SYNC.md#appendix-c-examples) |
| `L2204-workflow-push-token-rejected` | 2203-2204 | Pushing GitHub Actions Workflow Files | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L2205-workflow-push-pat` | 2205-2206 | Pushing GitHub Actions Workflow Files | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L2207-no-ci-pending` | 2207 | Pushing GitHub Actions Workflow Files | [Landing Work: Commit, PR, Review, Merge, Deploy](../../AGENT-SYNC.md#landing-work-commit-pr-review-merge-deploy) |
| `L2209-downloads-binding` | 2209-2212 | ~/Downloads Folder Symlink Quirks (Binding) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2212-downloads-symlink-fact` | 2212-2213 | ~/Downloads Folder Symlink Quirks (Binding) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2214-downloads-absolute-path` | 2214-2215 | ~/Downloads Folder Symlink Quirks (Binding) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2216-downloads-dont-fix` | 2216 | ~/Downloads Folder Symlink Quirks (Binding) | [Mac Machine Rules](../../AGENT-SYNC.md#mac-machine-rules) |
| `L2222-fleetwide-scope` | 2220-2222 | Fleet-wide operating rules | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L2226-no-external-contact` | 2224-2226 | No external contact without owner approval | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L2226-external-contact-exceptions` | 2226 | No external contact without owner approval | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L2230-no-forks` | 2228-2230 | No forks of other repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
| `L2230-clutch-dsh-example` | 2230 | No forks of other repositories | [Repositories, Forks, and External Contact](../../AGENT-SYNC.md#repositories-forks-and-external-contact) |
