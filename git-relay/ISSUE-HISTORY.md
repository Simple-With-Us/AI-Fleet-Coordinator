# Bounded issue history metadata

Two default-off own-App issues:read commands list issue events and timeline events.
Fixed issue number in authorized repo;1-50 results,1-1000 pages. Compact event
kind, optional positive ID, optional time, optional40hex commit only. No bodies,
actors, labels, arbitrary URLs or nested source objects returned. Missing fields
are null, not invented empty-history evidence. has_more is conservative based on
original page length. This is not complete historical activity, a full timeline,
or an independent verification of any actor or status. PRs through issue endpoints
may return PR event metadata; these are reads, not mutation destinations.
No configured grants, connections, writes or deployment. Existing broker receipt
secret scan remains. Raw detailed comment/review/label-history content still absent.

Source: https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
