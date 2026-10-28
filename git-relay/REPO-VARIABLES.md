# Repository Actions variables

Five default-off commands list/get/create/update/delete non-secret repository
Actions variables.  Explicit own-App variables:read/write permission is required;
not configured or added to a seat ceiling here.  Names uppercase1-100, noGITHUB_
prefix; values at most8192 UTF8 bytes and secret-scanned.  List pages1-1000,
size1-50; values returned only through existing response secret scanning.
Create requires actual empty201, update/delete empty204, else UNKNOWN/no replay.
Mutation receipts omit values.  No compare-and-swap or execution consent is implied;
these commands can alter future workflows and need separate owner authority.
No renames, org/environment variables, secrets API, or grants are implemented.
Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json

Credential-pattern scanning is best effort, not proof an arbitrary string is non-secret.  Never use these commands to store passwords or API keys.

Known-success update/delete setters reapply after fresh authorization, including A-B-A.  Create remains append-only cached success.  Pending/UNKNOWN never re-arm.
