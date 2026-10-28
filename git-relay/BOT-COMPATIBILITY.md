# Persistent bot runtime configuration contract

Git Relay does not select an engine, platform or interactive login. Each configured
principal has its own `identity`, `app_slug`, action allowlist, repository allowlist
and `mint_permissions`. A bearer must verify to that exact configured identity.
`app_slug` must equal the policy principal, so one bot cannot borrow another App.
The server never synthesizes a policy entry merely because a bearer exists.

The existing `GIT_RELAY_SEATS_JSON` accepts any persistent identity under this
contract, including bearer-only bots, BotFleet roles and Grok Bot personas. It is
not limited to the six currently deployed principals. Missing configuration or
credentials refuses that bot only; there is no shared identity fallback. A new
principal's explicit App slug must come from its configuration, not a guessed
platform name. `JET-CHAT` is an existing login context, not a new persistent bot.

`test_bot_compatibility.py` covers the 38 named persistent identities with synthetic
policy entries, own-App resolution, allowed and denied repositories, existing bearer
shapes and the JET-CHAT login context. These are code compatibility tests, not proof
that any bot has an App, policy entry, credential or working connection. No account
setup, minting, policy widening or live verification is performed by these tests.
