# Bounded binary Git blob read

One default-off own-App contents:read command reads an exact blob SHA, maximum32000
raw bytes. Git object SHA1 and raw byte count are verified; output canonical base64.
Latin1-decoded bytes are explicitly scanned for repository known ASCII credential
patterns BEFORE opaque base64 leaves relay, and again at MCP receipt boundary.
This is best-effort known-pattern scanning, not a secret-free assertion. No arbitrary
URL, range, larger blob, write or branch operation. Source only, no grants/connections.
Existing UTF8blobread remains unchanged. General large binary support still absent.

Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
