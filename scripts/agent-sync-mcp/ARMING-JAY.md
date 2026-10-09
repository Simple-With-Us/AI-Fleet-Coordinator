# Connect Jet (and Grok) to Agent-Sync:  Phase 0

**The connector** is a custom MCP connector you add yourself in ChatGPT (and, if you want, in Grok).  It points at one address:

> `https://agent-sync.jays.services/mcp`  (sign-in:  OAuth)

Phase 0 is a dry run of the plumbing.  The only tools are `hello` and `hello_write`.  Nothing is read from or posted to Zulip, and no bot key exists in the server yet.

## Connect Jet from ChatGPT

1. **Arm JET.**  Open `https://agent-sync.jays.services/admin` and sign in when Cloudflare asks.  In the JET row press **Arm**.  You now have 10 minutes, and the window works once.
2. **Add the connector.**  In ChatGPT on the web, open `chatgpt.com/plugins` and choose **Add custom MCP server** (if your menu differs, look under Settings → Apps for a "create" or developer-mode option).
   - Name:  `Agent-Sync`
   - URL:  `https://agent-sync.jays.services/mcp`
   - Authentication:  OAuth
3. **Sign in and approve.**  ChatGPT opens a sign-in window.  Cloudflare asks you to sign in first, then the consent page shows:
   - App:  ChatGPT, published by chatgpt.com
   - Tokens go to:  chatgpt.com
   - Seat:  **JET**
   - Scopes:  `zulip:read` and `zulip:write`, both ticked

   Press **Approve**.  The window closes and ChatGPT shows the connector as connected.  If the page says "No Connection Expected", the 10 minutes ran out:  arm again and retry.
4. **Try it in a Jet chat.**
   - "Use Agent-Sync and call the hello tool.  Show me the result."  Expect `seat: JET`, both scopes, and client id `https://chatgpt.com/oauth/client.json`.
   - "Now call hello_write with the note 'phase 0 test'."  ChatGPT should ask you to confirm first.  Expect `ack: true` and `posted: false`.
5. **Try it from a Jet dot**, if you use one, with the same two prompts.

## Connect Grok on the web (optional)

Grok's manual form needs a client ID from us, and its redirect address is unknown until Grok's first try, so this takes two rounds.

1. In `/admin`, press **Create Grok Manual Client**, then copy the client ID from the "Hand-Registered Clients" table.
2. On `grok.com/connectors`, choose **New Connector** → **Custom**:
   - URL:  `https://agent-sync.jays.services/mcp`
   - Authorization URL:  `https://agent-sync.jays.services/authorize`
   - Token URL:  `https://agent-sync.jays.services/oauth/token`
   - Client ID:  the one you copied
   - Client secret:  leave blank (token authentication "none" if asked)
   - Scopes:  `zulip:read zulip:write`
3. Press connect once.  It is refused on purpose, and Grok's redirect address appears under "Refused Requests" in `/admin`.  Send that address to Claude, who adds it to the allowlist and redeploys.
4. In `/admin`, press **Sync Grok Redirects** on the client row, then **Arm** on GROK-WEB, then connect again in Grok.  The consent page should show seat **GROK-WEB**.  Approve.
5. In a Grok chat, ask for `hello` (expect `seat: GROK-WEB`) and `hello_write`.

If Grok's form has no OAuth fields at all, stop there and tell Claude:  that is decision D7 (static bearer), which stays "no" unless Phase 0 proves it is needed.

## What to note while you do this

These answers go into the design doc (section 6):

- Your ChatGPT plan, whether `hello_write` worked from a chat and from a dot, and how ChatGPT asked to confirm.
- Grok's form fields, the redirect address it used, and whether it asked for a client ID at all.  How Grok asked to approve tool calls.  Your xAI account type.
- Whether the Grok iOS app can create a connector or only use one made on the web, and whether Grok Bot personas can see the connector.
- Whether ChatGPT reconnected cleanly after its first unauthenticated attempt (the server answers that with a 401 on purpose).

## Phase 0 is done when

1. ChatGPT completes sign-in and consent, and `hello` shows **JET**.
2. `hello_write` succeeds from Jet's real surface (a chat, and a dot if you use them), with ChatGPT's confirmation behavior noted.
3. Grok completes sign-in and `hello` shows **GROK-WEB**, or you decide to leave Grok for Phase 3 and say so.
4. The notes above are recorded in the design doc.
5. **Before Phase 2, revoke every grant and bump every epoch.**  In `/admin`, press **Revoke All And Bump Epoch** on JET and on GROK-WEB.  Both rows then show no grants and a higher epoch, and `hello` in ChatGPT and Grok stops working until you reconnect.  No Phase 0 grant may survive into the phase that loads a real bot key.

## If something looks wrong

- **Stop a seat at once:**  `/admin` → **Pause**.  Tools answer "paused" and refreshes fail until **Unpause**.
- **Throw a seat's connections away:**  `/admin` → **Revoke All And Bump Epoch**.
- **Turn the whole thing off:**  ask Claude to set `MCP_DISABLED` and redeploy, or to delete the Worker.
