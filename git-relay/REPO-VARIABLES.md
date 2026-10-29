# Repository Actions variables

Five default-off commands list/get/create/update/delete non-secret repository
Actions variables.  Explicit own-App variables:read/write permission is required;
not configured or added to a seat ceiling here.  Names uppercase1-100, noGITHUB_
prefix; values at most8192 UTF8 bytes and secret-scanned.  List pages1-1000,
size1-50; values returned only through existing response secret scanning.
Create requires actual201 with empty JSON object (or empty body), update/delete empty204, else UNKNOWN/no replay.
Mutation receipts omit values.  No compare-and-swap or execution consent is implied;
these commands can alter future workflows and need separate owner authority.
No renames, org/environment variables, secrets API, or grants are implemented.
Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json

Credential-pattern scanning is best effort, not proof an arbitrary string is non-secret.  Never use these commands to store passwords or API keys.

Known-success update/delete setters reapply after fresh authorization, including A-B-A.  Create remains append-only cached success.  An already-absent delete (upstream 404) is a completed desired state only after verifying the exact repository still exists; other failures remain errors.  Pending/UNKNOWN never re-arm.

Variables is now a supported minter permission key in source, not a configured grant.  Existing seat permissions remain unchanged and deny by default.
