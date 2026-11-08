# Bounded PR list pages

Own-App pull_requests:read, default-off `wf_pr_list_page` adds page1-1000,
1-50records and fixed created-ascending order to PR listing. Existing wf_pr_list
already supports same-owner head branch and base filters; these are not new
capabilities. New receipt qualification keeps PRnumber/state/draft and head/base
refs/SHAs only. No title/body/author/URL/unbounded provider fields. State and supplied
head/base filter matches are validated; head provider label must identify the
requested repository owner, not a same-named fork branch. No arbitrary sort/raw
query/cross-owner syntax. Duplicate IDs and malformed records are refused.

`has_more` is conservatively true for full pages. `snapshot:false` is always
returned: PR creation, closing and updates can change membership/metadata between
pages. Do not claim completeness or infer absence, readiness or permission from a
page. Head/base metadata can go stale immediately; writes need their own fences.
No implicit merge/review decision. PR visibility and permissions remain own-App.

Source relevance: iOS publish uses same-owner head filtering before PR creation.
This supplies a qualified page, not proof that no PR exists. No live inventory
read, PR opened, review triggered, deployment or credential change performed.
Verified endpoint/filter semantics:
https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json

Observed branch metadata accepts printable Git-ref characters including @,+,#
and Unicode. It is never reused as a write ref. Caller head/base filter grammar
and all existing write-ref validation stay unchanged. Malformed rows still refuse
as a whole rather than silently omit PRs.

The relay reads GitHub's raw page, which is far larger than the rows it returns: 50 pull requests are 1.0 to 1.3 MB raw and about 20 KB as rows.  A paged GET (`per_page` in its query) is therefore bounded at 8 MiB (`MAX_PAGE_BODY` in `github_client.py`) while every other read keeps the 1 MiB bound.  Before this, BotFleet page 11 at per_page 50 (1,257,416 bytes raw) hit the 1 MiB bound and reached callers as an unreadable answer, though the same pull requests read fine at smaller page sizes.
