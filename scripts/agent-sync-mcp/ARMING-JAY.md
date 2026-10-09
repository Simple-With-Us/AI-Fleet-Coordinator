# Connect Jet (and Grok) to Agent-Sync:  Phase 0

**The connector** is a custom MCP connector you add yourself in ChatGPT (and, if you want, in Grok).  It points at one address:

> `https://agent-sync.jays.services/mcp`  (sign-in:  OAuth)

Phase 0 is a dry run of the plumbing.  The only tools are `hello` and `hello_write`.  Nothing is read from or posted to Zulip, and no bot key exists in the server yet.

Sign-in goes through Cloudflare Access with a one-time PIN sent to `mail@jays.services` (that is the only address allowed).  Every button on the consent and admin pages is a form post, so the first Arm and the first Approve are also the first live-browser test of this server:  if either says "Request Refused", stop and tell Claude what the page said.

## Connect Jet from ChatGPT

**Before you start** (the steps below come from vendor setup guides, not an OpenAI help page, so the menu names may differ in your ChatGPT:  look for the same words and verify in the UI).

- Use ChatGPT **on the web**.  Setting up a custom connector cannot be completed in the mobile app.
- Custom connectors sit behind **Developer mode**:  Settings → Apps (also shown as "Apps and Connectors") → Advanced settings → Developer mode.  Whether you can turn it on depends on your plan, and on a Team or Enterprise plan an admin has to enable it first.
- A new connector is visible only to the person who created it.  Other chats and other people will not see it.

1. **Arm JET.**  Open `https://agent-sync.jays.services/admin` and sign in when Cloudflare asks.  In the JET row press **Arm**.  You now have 10 minutes, and the window works once.
2. **Add the connector.**  In Settings → Apps, choose **Create app** (or **Add custom MCP server** under `chatgpt.com/plugins`).
   - Name:  `Agent-Sync`
   - URL:  `https://agent-sync.jays.services/mcp`
   - Authentication:  **OAuth**.  Leave **Client ID** and **Client Secret** blank:  the server publishes ChatGPT's client details itself, so there is nothing to register.
   - Tick the trust checkbox ("I trust this application") if it is shown.
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

Grok publishes its own client details at `https://grok.com/oauth/mcp-client.json` (the server fetched that file on Fri, Oct 9:  public client, no secret, redirect `https://grok.com/connectors-oauth-exchange-code/`).  The server already allows exactly that client and that redirect for the **GROK-WEB** seat, so this is one round, like ChatGPT.  That Grok identifies itself this way comes from that file plus one third-party guide;  xAI has no page we could find, so Phase 0 confirms it.

1. **Arm GROK-WEB.**  In `/admin` press **Arm** in the GROK-WEB row.
2. **Add the connector.**  On `grok.com/connectors`, choose **New Connector** → **Custom** and give only the URL `https://agent-sync.jays.services/mcp`.  Leave any client ID and secret fields empty.
3. **Sign in and approve.**  The consent page should show App:  Grok, published by grok.com, and Seat:  **GROK-WEB**.  Press **Approve**.
4. **Try it.**  In a Grok chat ask for `hello` (expect `seat: GROK-WEB`) and `hello_write`.

**If your xAI account is Business or Enterprise**, an admin adds the connector in `console.x.ai` first, and Grok then redirects to `https://console.x.ai/connectors-oauth-exchange-code/`.  That address is not allowed yet.  Tell Claude, who adds it to the GROK-WEB list in `wrangler.jsonc` and redeploys.

**If Grok's form insists on a client ID** (the fallback):

1. In `/admin`, press **Create Grok Manual Client** and copy the client ID from the "Hand-Registered Clients" table.  It takes the redirect addresses already configured for GROK-WEB.
2. In Grok's form use URL `https://agent-sync.jays.services/mcp`, Authorization URL `https://agent-sync.jays.services/authorize`, Token URL `https://agent-sync.jays.services/oauth/token`, the client ID, an empty secret (token authentication "none" if asked), and scopes `zulip:read zulip:write`.
3. If Grok uses a redirect address we do not allow, the attempt is refused on purpose and appears under **Refused Authorize Requests** in `/admin` with your sign-in email and the time.  Before you send that address to Claude, check that it is `https` on a `grok.com` or `x.ai` host and that the time matches your own attempt.  Anything else is somebody else's request:  ignore it.  Claude adds the address to the GROK-WEB list, redeploys, and you press **Sync Grok Redirects** on the client row.
4. Arm GROK-WEB again and connect.

If Grok's form has no OAuth fields at all, stop there and tell Claude:  that is decision D7 (static bearer), which stays "no" unless Phase 0 proves it is needed.

## What to note while you do this

These answers go into the design doc (section 6):

- Your ChatGPT plan, the real menu path you used, whether `hello_write` worked from a chat and from a dot, and how ChatGPT asked to confirm.
- Grok's form fields, whether it used its client details file or asked for a client ID, the redirect address it used, and how Grok asked to approve tool calls.  Your xAI account type.
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
