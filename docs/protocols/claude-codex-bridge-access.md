# Claude and Codex bridge access (AFC-296)

Prepared Mon, Oct 12, 2026.  The hosted Agent-Sync endpoint is
`https://agent-sync.jays.services/mcp`; the server binds each grant to a seat and
uses that seat's bot credential.  Identity is never a caller-supplied argument.

| Surface | Supported route | Status in this change |
| --- | --- | --- |
| Claude web, mobile, Desktop, Cowork | Hosted remote custom connector; independent public OAuth client; exact `https://claude.ai/api/mcp/auth_callback` | CLAUDE configuration prepared; provisioning, deploy, registration, consent and real hosted acceptance pending |
| Claude Code cloud | Cloud host delivers claude.ai connectors, subject to organization and session policy | Can use the hosted CLAUDE connector after connection and surface acceptance |
| Claude Code native | Direct HTTP OAuth with loopback, or local stdio `simple-chat mcp` | Separate route; no native callback added |
| Codex local desktop, CLI, IDE | Native Streamable HTTP OAuth or local stdio `simple-chat mcp` | Client supports independent configured OAuth ID; no hosted CODEX binding added |
| Codex Cloud / hosted Work | Hosted plugin MCP connection | Actual independent callback/client identity required before enabling CODEX |

## Hosted identity requirements

Claude supports **Use your own OAuth client**.  Its blank secret is a public
client, with S256 PKCE; DCR can stay disabled.  The CLAUDE manual client carries
its own seat tag, and its callback belongs only to CLAUDE.  Worker bindings
`ZULIP_CLAUDE_EMAIL` and `ZULIP_CLAUDE_API_KEY` use the existing canonical
AFC `prod` `/zulip` entry names; Worker provisioning remains unconfirmed.  The existing JET
client and grant cannot be reused.  A real `whoami` must confirm CLAUDE,
`claude-bot@simplewithus.zulipchat.com`, role 400 and hosted transport.  Follow
[owner connection steps](../../scripts/agent-sync-mcp/ARMING-JAY.md#connect-hosted-claude-claude-afc-296).

Codex Cloud needs a separate hosted MCP connection's actual OAuth identity from
its management page or a matching owner-initiated refusal.  OpenAI's hosted
connection can use the stable ChatGPT callback/CIMD pair or a callback-specific
pair; the stable pair already belongs to JET.  The bridge rejects sharing an
HTTPS callback or CIMD identity between seats.  Do not guess a CODEX callback,
move JET's pair, broaden its allowlist or enable DCR to bypass that boundary.
An independent binding must be demonstrated before CODEX is enabled.

Hosted CODEX Linear access is a separate track from Zulip OAuth.  Currently
`src/linear.js` enables only GROK-WEB, and `LINEAR_TOOLS_WRITES=0` holds writes
off globally.  Future CODEX access needs its own Linear app credential pair,
verified app identity, a scoped policy change and real `linear_whoami` acceptance.
Do not enable global writes or change GROK-WEB as a shortcut.  The server-local
`simple-board` route is independent of hosted Linear permissions.

Native Codex uses different client metadata documents:
`https://chatgpt.com/oauth/codex/client.json` with issuer-bound responses, or
`https://chatgpt.com/oauth/codex/<callback_id>/client.json` otherwise.  Its default
loopback has an ephemeral port; configured clients can use an exact callback.
These native identities do not establish Codex Cloud compatibility.  Native
Claude's localhost/variable-port callback also differs from this bridge's exact
IPv4 loopback policy; configuring it requires a separate explicit decision.

Sources, verified Oct 12: [OpenAI MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[OpenAI hosted OAuth](https://developers.openai.com/plugins/build/auth),
[Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp),
[Claude authentication](https://claude.com/docs/connectors/building/authentication),
and [Claude Code connector delivery](https://code.claude.com/docs/en/mcp#use-mcp-servers-from-claudeai).

## CODEX preparation tracks

Jay selected **prepare both routes** for CODEX: the server-local CLI/MCP track
with Zulip and its own Linear app, and the hosted OAuth track.  Both remain
in scope; the hosted track needs actual independent connection identity capture
through supported management UI, while persistent server credentials need
separate approval and secure provisioning.

Preparation does not authorize a persistent credential handoff or starting
unattended sessions.  Verify the seat identity after separately authorized
provisioning; never substitute another seat's credentials.  No listener or wake
configuration is changed by this hosted preparation.
