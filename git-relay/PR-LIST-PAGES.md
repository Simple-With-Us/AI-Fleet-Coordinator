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
