# Repository webhooks GitHub commands

Default-off own-App repository webhook reads and writes on the existing
workflow-verb custody path.  Source contract:
https://docs.github.com/en/rest/repos/webhooks

Reads.  `wf_webhooks` lists a bounded page and `wf_webhook_get` reads one
hook.  Both project id, name, active flag, the sorted event list and the
non-secret config (url, content_type, insecure_ssl).  `config.secret` is
dropped unread wherever upstream returns it:  never projected, compared,
logged or persisted.

Secret boundary (the hard rule).  Raw shared secrets must not travel the
generic request channel - args, canonical forms and custody keys see
every field even when outputs and audits omit them - so `config.secret`
is NOT accepted anywhere.  Hook secrets wait for an approved private
secret-reference channel;  until then, creating a hook that needs HMAC
signing and rotating or setting a secret are honest gaps, not hidden
capabilities.  Reads and the config-field update below remain safe for
secret-bearing hooks.

Update routes (why there is no events/active update).  The general
update route PATCH .../hooks/{id} is documented to REMOVE a previously
set secret unless the same or a new secret is provided with the call.
Secret presence is never exposed by the API, so no request on that route
can be proven safe here:  events and active changes (which only that
route accepts) are withheld.  Config-field changes use the documented
dedicated route PATCH .../hooks/{id}/config, whose contract preserves
the existing secret by omission;  `wf_webhook_update` sends only url,
content_type and/or insecure_ssl there (at least one required) and
verifies exactly the sent fields against a fresh config read.  Any
mismatch is UNKNOWN (502), never a local refusal that would release the
outcome key.

URLs are parsed, not regex-trusted:  https only, a real host, no
userinfo, no query and no fragment (query strings and userinfo commonly
smuggle delivery credentials), plus the known-pattern credential scan on
the whole value.  The same rules apply to URLs upstream returns.
insecure_ssl accepts the documented "0"/"1"/0/1 forms and normalizes to
the string form;  JSON booleans are rejected (Python's True == 1 would
otherwise smuggle an invalid value upstream).

Writes.  `wf_webhook_create` requires `config.url`;  `name` only accepts
the documented `web`.  Omitted fields take the documented defaults
(content_type form, insecure_ssl "0", events ["push"], active true) and
the fresh-read verification expects exactly those defaults, so a server
that applied something else is UNKNOWN.  `wf_webhook_delete` is verified
by a fresh 404 read;  a still-present hook or a failed re-read is
UNKNOWN.  `wf_webhook_ping` confirms upstream acceptance (204) only -
delivery is asynchronous and honestly not verified by the verb.

Custody.  Any validation fault while projecting a response AFTER a write
is UNKNOWN (502) - never a local refusal that would release the outcome
key and allow a repeated mutation.  All verbs need the administration
permission in the seat ceiling (read or write per verb) and stay under
the existing own-App, per-seat, default-off gates;  declaring capability
grants no invocation authority.
