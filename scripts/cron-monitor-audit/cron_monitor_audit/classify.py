"""Sort each monitor against the senders that were found."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cron_monitor_audit.errors import AuditError
from cron_monitor_audit.parse import literal_slugs, operator_senders, parse_reporter, slugify
from cron_monitor_audit.sentry import Monitor, is_disabled
from cron_monitor_audit.sources import CodeHit, CodeSearch, RepoSource

Category = Literal["matched_healthy", "matched_failing", "orphan", "uncertain"]

# Sentry stores Monitor.slug at 50 characters. The reporter sends the full
# ci-<identity>-<workflow> slug, and a longer one is stored as that prefix.
_SLUG_MAX = 50

_DOC_SUFFIXES = (".md", ".rst", ".txt")
_SIGNALS = (
    "monitor_slug",
    "monitorSlug",
    "capture_checkin",
    "captureCheckIn",
    "check_in",
    "checkins",
)


@dataclass(frozen=True)
class Finding:
    category: Category
    reason: str
    sender: str


@dataclass(frozen=True)
class Catalog:
    produced: dict[str, str]
    literals: dict[str, str]
    operator_pinned: dict[str, str]
    operator_prefix: str
    identities: dict[str, set[str]]
    schedules_known: set[str]
    readable: set[str]
    unreadable: tuple[tuple[str, str], ...]
    code_search: CodeSearch


def build_catalog(
    repos: list[RepoSource],
    identities: dict[str, set[str]],
    code_search: CodeSearch | None = None,
) -> Catalog:
    produced: dict[str, str] = {}
    literals: dict[str, str] = {}
    pinned: dict[str, str] = {}
    prefix = ""
    known: dict[str, set[str]] = {name: set(owners) for name, owners in identities.items()}
    schedules_known: set[str] = set()
    determined: set[str] = set()
    readable: set[str] = set()
    unreadable: list[tuple[str, str]] = []
    for repo in repos:
        if not repo.readable:
            unreadable.append((repo.full_name, repo.reason))
            continue
        readable.add(repo.full_name)
        reporter_identity, schedules = _repo_schedules(repo)
        if reporter_identity:
            known.setdefault(reporter_identity, set()).add(repo.full_name)
        if schedules is not None:
            determined.add(repo.full_name)
            if reporter_identity:
                schedules_known.add(reporter_identity)
                folded = {name.casefold(): name for name in repo.workflow_names}
                for workflow_name in schedules:
                    if repo.names_known and workflow_name.casefold() not in folded:
                        continue
                    slug = f"ci-{reporter_identity}-{slugify(workflow_name)}"
                    produced.setdefault(slug, repo.full_name)
        for path, text in repo.files:
            where = f"{repo.full_name}:{path}"
            for slug in literal_slugs(text):
                literals.setdefault(slug, where)
            extra, dynamic = operator_senders(text)
            if dynamic:
                prefix = dynamic
            for slug in extra:
                pinned.setdefault(slug, where)
    for identity, owners in known.items():
        if identity in schedules_known or not owners:
            continue
        if owners <= determined:
            schedules_known.add(identity)
    return Catalog(
        produced=produced,
        literals=literals,
        operator_pinned=pinned,
        operator_prefix=prefix,
        identities=known,
        schedules_known=schedules_known,
        readable=readable,
        unreadable=tuple(unreadable),
        code_search=code_search or CodeSearch("unavailable", "not run", {}),
    )


def explainable(slug: str, catalog: Catalog) -> bool:
    if _exact_sender(slug, catalog) or _prefix_hits(slug, catalog):
        return True
    return bool(catalog.operator_prefix) and slug.startswith(catalog.operator_prefix)


def classify_monitor(monitor: Monitor, catalog: Catalog) -> Finding:
    sender, kind = _sender(monitor.slug, catalog)
    if kind == "sender":
        return _matched(monitor, sender)
    if kind == "mention":
        return Finding(
            "uncertain",
            f"mentioned in {sender} and no check-in send was found",
            sender,
        )
    if kind == "dynamic":
        return Finding(
            "uncertain",
            f"a dynamic {catalog.operator_prefix}<instance> sender exists and this instance is not pinned",
            "",
        )
    if kind == "ambiguous":
        return Finding(
            "uncertain",
            "the slug is 50 characters and is a prefix of more than one longer sender slug",
            "",
        )
    if _confident_orphan(monitor.slug, catalog):
        return Finding("orphan", _orphan_reason(monitor), "")
    detail = catalog.code_search.detail or catalog.code_search.state
    return Finding(
        "uncertain",
        "no check-in send was found in the scanned workflows and scripts; "
        f"code search is {catalog.code_search.state} ({detail})",
        "",
    )


def _matched(monitor: Monitor, sender: str) -> Finding:
    if is_disabled(monitor):
        return Finding(
            "uncertain",
            f"sender {sender} exists and the monitor status is {monitor.status or 'disabled'}",
            sender,
        )
    if monitor.health == "failing":
        return Finding(
            "matched_failing",
            f"sender {sender} and the latest check-in status is {monitor.check_status}",
            sender,
        )
    if monitor.health == "healthy":
        return Finding("matched_healthy", f"sender {sender}", sender)
    return Finding(
        "uncertain",
        f"sender {sender} exists and Sentry reported no check-in status",
        sender,
    )


def _sender(slug: str, catalog: Catalog) -> tuple[str, str]:
    exact = _exact_sender(slug, catalog)
    if exact:
        return exact, "sender"
    hits = _prefix_hits(slug, catalog)
    fulls = {full for full, _where in hits}
    if len(fulls) == 1:
        return hits[0][1], "sender"
    if len(fulls) > 1:
        return "", "ambiguous"
    hit = _code_hit(slug, catalog)
    if hit == "sender":
        return _hit_where(slug, catalog, sender=True), "sender"
    if hit == "mention":
        return _hit_where(slug, catalog, sender=False), "mention"
    if catalog.operator_prefix and slug.startswith(catalog.operator_prefix):
        return "", "dynamic"
    return "", ""


def _exact_sender(slug: str, catalog: Catalog) -> str:
    for mapping in (catalog.produced, catalog.literals, catalog.operator_pinned):
        if slug in mapping:
            return mapping[slug]
    return ""


def _prefix_hits(slug: str, catalog: Catalog) -> list[tuple[str, str]]:
    if len(slug) != _SLUG_MAX:
        return []
    hits: list[tuple[str, str]] = []
    for mapping in (catalog.produced, catalog.literals, catalog.operator_pinned):
        for full, where in mapping.items():
            if len(full) > _SLUG_MAX and full.startswith(slug):
                hits.append((full, where))
    return hits


def _code_hit(slug: str, catalog: Catalog) -> str:
    hits = catalog.code_search.hits.get(slug) or ()
    saw_mention = False
    for hit in hits:
        if _hit_is_sender(slug, hit):
            return "sender"
        if hit.path.endswith(_DOC_SUFFIXES):
            saw_mention = True
    if saw_mention:
        return "mention"
    return ""


def _hit_is_sender(slug: str, hit: CodeHit) -> bool:
    path = hit.path.replace("\\", "/")
    lowered = path.lower()
    if lowered.endswith(_DOC_SUFFIXES):
        return False
    parts = lowered.split("/")
    if "tests" in parts or parts[-1].startswith("test_"):
        return False
    if slug in literal_slugs(hit.fragment):
        return True
    return any(signal in hit.fragment for signal in _SIGNALS) and slug in hit.fragment


def _hit_where(slug: str, catalog: Catalog, sender: bool) -> str:
    for hit in catalog.code_search.hits.get(slug) or ():
        if sender and _hit_is_sender(slug, hit):
            return hit.path
        if not sender and hit.path.endswith(_DOC_SUFFIXES):
            return hit.path
    return ""


def _confident_orphan(slug: str, catalog: Catalog) -> bool:
    identity = match_identity(slug, set(catalog.identities))
    if identity is not None:
        return identity in catalog.schedules_known
    if not slug.startswith("ci-"):
        return False
    if catalog.unreadable:
        return False
    return catalog.code_search.state == "ok" or bool(catalog.readable)


def _orphan_reason(monitor: Monitor) -> str:
    if is_disabled(monitor):
        return f"no check-in send was found and the monitor is already {monitor.status}"
    return "no check-in send was found; proposed disable"


def match_identity(slug: str, identities: set[str]) -> str | None:
    if not slug.startswith("ci-"):
        return None
    rest = slug[3:]
    matches = [item for item in identities if rest == item or rest.startswith(item + "-")]
    if not matches:
        return None
    return max(matches, key=len)


def _repo_schedules(repo: RepoSource) -> tuple[str, dict[str, str] | None]:
    """Identity and the schedule map that sends check-ins.

    A reusable workflow cron_schedules input wins over a vendored
    CRON_SCHEDULES constant. An empty map means the repo was read and
    does not send ci check-ins.
    """
    workflow: tuple[str, dict[str, str] | None] | None = None
    script: tuple[str, dict[str, str] | None] | None = None
    saw_workflow_file = False
    saw_script = False
    for path, text in repo.files:
        reporter = parse_reporter(path, text)
        if reporter is None:
            continue
        if path.endswith(".py"):
            saw_script = True
            script = (reporter.identity, reporter.schedules)
        else:
            saw_workflow_file = True
            if reporter.schedules is not None or reporter.identity:
                workflow = (reporter.identity, reporter.schedules)
    if workflow and workflow[1] is not None:
        identity = workflow[0] or (script[0] if script else "")
        return identity, workflow[1]
    if script and script[1] is not None:
        identity = (workflow[0] if workflow and workflow[0] else "") or script[0]
        return identity, script[1]
    if repo.names_known and not saw_script and not saw_workflow_file:
        return "", {}
    if workflow and workflow[0]:
        return workflow[0], workflow[1]
    if script and script[0]:
        return script[0], script[1]
    return "", None


def category_heading(category: Category) -> str:
    match category:
        case "matched_healthy":
            return "Matched And Healthy"
        case "matched_failing":
            return "Matched But Failing"
        case "orphan":
            return "Orphans"
        case "uncertain":
            return "Uncertain"
        case _ as other:
            unexpected: never = other
            raise AuditError(f"unknown category {unexpected}")
