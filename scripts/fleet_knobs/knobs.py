"""``knobs.get(name, default, kind)``:  the one way a fleet job reads a mode, a cap or a flag.

    from fleet_knobs import knobs
    mode = knobs.get("CLAIM_EXPIRY_MODE", "dry-run", "enum", local=config_mode, stop=stop_file_exists)
    report["knobs"] = knobs.report()          # names, sources and the stale flag, never values

What a job can rely on:

  * One Infisical fetch per folder per process, on the first read of a knob in that folder
    (``Spec.folder``, "/" unless the registry says otherwise), and no fetch of a folder the
    process never reads.  So a job reading only root knobs fetches "/" exactly as before, and
    the wake router fetches only /wake-router, which its narrow token allows.  A failed fetch
    is not retried in the same process (``refresh()`` forces it for a long-running one).  An
    export that holds none of a folder's registered names counts as a failed fetch (reason
    ``empty``) while a usable copy of that folder still has knobs, so a blank answer cannot
    erase a pause.
  * A failed fetch leaves a 60 second marker beside the copy (lkg.py), and other processes
    skip their own fetch of that folder inside that window, so a broken auth path is paid for
    once.  Each folder has its own copy and its own marker.
  * Non-secret knobs fall back, in order, to the on-disk last-known-good copy (marked
    stale) and then to the registry default.  A read never raises for a non-secret knob.
  * A secret has no default and no disk copy:  if it cannot be fetched, ``get()`` raises
    SecretUnavailable and the job must stop.  "Never cached" means never written anywhere;
    the value stays in this process's memory for the one fetch.
  * ``local=`` (a job's own config file) and ``stop=`` (a STOP file) can only narrow the
    answer.  A local value that would widen it is ignored and the report says so.
  * Only names in the registry can be read.  Logs carry key names and short reasons only.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import lkg, source, values
from .registry import BY_NAME, REGISTRY, ROOT_FOLDER, Spec, names_in

log = logging.getLogger("fleet_knobs")

_UNSET = object()

# Failures that belong to this one process, not to Infisical or the network:  a process with
# no reader token of its own (Muster's Controls tab runs the engine without one) or no reader
# helper.  They cost no wait, and recording them would make every sibling that does hold a
# token skip its own fetch for the marker's 60 seconds, so they leave no marker.
PROCESS_LOCAL_REASONS = frozenset({"service-token-required", "scoped-reader-unavailable"})


class KnobError(Exception):
    """A programming or configuration error:  unknown knob, wrong kind, refused environment."""


class SecretUnavailable(KnobError):
    """A secret knob could not be fetched (or is unset or malformed).  Fail closed."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"secret {name} is unavailable ({reason});  failing closed")
        self.name = name
        self.reason = reason


class Knob:
    """The answer for one knob.  ``repr()`` never shows the value."""

    __slots__ = ("local_ignored", "name", "narrowed", "reason", "source", "stale", "value")

    def __init__(
        self,
        name: str,
        value: Any,
        source_: str,
        stale: bool = False,
        reason: str | None = None,
        narrowed: bool = False,
        local_ignored: bool = False,
    ) -> None:
        self.name = name
        self.value = value
        self.source = source_  # infisical, lkg or default
        self.stale = stale
        self.reason = reason  # unset, invalid, unreachable:<token>, lkg-<note>
        self.narrowed = narrowed
        self.local_ignored = local_ignored

    def __repr__(self) -> str:
        return f"Knob({self.name}, source={self.source}, stale={self.stale})"

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "stale": self.stale}
        if self.reason:
            out["reason"] = self.reason
        if self.narrowed:
            out["narrowed"] = True
        if self.local_ignored:
            out["local_ignored"] = True
        return out


class _Folder:
    """What this process learned about one Infisical folder."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.loaded = False
        self.fetch = "not-attempted"  # ok, failed or not-attempted
        self.reason: str | None = None
        self.live: dict[str, str] = {}
        self.lkg: dict | None = None
        self.lkg_note = "not-read"


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.folders: dict[str, _Folder] = {}
        self.answers: dict[str, Knob] = {}
        self.force = False  # refresh():  fetch even inside the negative cache

    def folder(self, path: str) -> _Folder:
        found = self.folders.get(path)
        if found is None:
            found = self.folders[path] = _Folder(path)
        return found


_state = _State()


def reset_for_tests() -> None:
    global _state
    _state = _State()


def refresh() -> None:
    """Forget the fetch so the next read fetches again (for a long-running process).

    A deliberate retry, so it also ignores a sibling's recent-failure marker."""
    reset_for_tests()
    _state.force = True


def _spec(name: str, kind: str | None) -> Spec:
    spec = BY_NAME.get(name)
    if spec is None:
        raise KnobError(
            f"unknown knob {name!r}:  add it to fleet_knobs/registry.py and INFISICAL.md first"
        )
    if kind is not None and kind != spec.kind:
        raise KnobError(f"knob {name} is kind {spec.kind}, not {kind}")
    return spec


def _load(path: str = ROOT_FOLDER) -> _Folder:
    """Fetch one folder, once per process, and answer what was learned about it."""
    st = _state
    with st.lock:
        fo = st.folder(path)
        if fo.loaded:
            return fo
        fo.loaded = True
        refusal = source.environment_refusal()
        if refusal:
            fo.loaded = False
            raise KnobError(refusal)
        previous, previous_note = lkg.read(folder=path)
        recent = None if st.force else _recent_failure(path)
        if recent is not None:
            # A sibling process failed moments ago:  skip the fetch (fail fast) and use the copy.
            fo.fetch = "failed"
            fo.reason = "recent-" + recent
            fo.lkg, fo.lkg_note = previous, previous_note
            log.info(
                "fleet_knobs: skipping the Infisical fetch of %s, which failed %s seconds ago "
                "or less (%s)",
                path,
                int(lkg.FAILURE_TTL_SECONDS),
                recent,
            )
            return fo
        try:
            live = source.fetch_named(names_in(path), project_id=source.PROJECT_ID, path=path)
            _refuse_empty_export(live, previous)
            fo.live = live
            fo.fetch = "ok"
            fo.lkg, fo.lkg_note = previous, previous_note
        except Exception as exc:  # noqa: BLE001 - a read must not crash a job
            fo.fetch = "failed"
            fo.reason = (
                exc.reason if isinstance(exc, source.FetchFailed) else "error-" + type(exc).__name__
            )
            log.warning(
                "fleet_knobs: Infisical fetch of %s failed (%s);  using the last-known-good "
                "copy where there is one",
                path,
                fo.reason,
            )
            fo.lkg, fo.lkg_note = previous, previous_note
            if fo.reason not in PROCESS_LOCAL_REASONS:
                try:
                    lkg.record_failure(fo.reason, folder=path)
                except Exception:  # noqa: BLE001 - the marker is a convenience
                    pass
            return fo
        try:
            lkg.clear_failure(folder=path)
        except Exception:  # noqa: BLE001 - the marker is a convenience
            pass
        try:
            _save_lkg(fo)
        except Exception as exc:  # noqa: BLE001 - the copy is a convenience
            log.warning(
                "fleet_knobs: could not update the last-known-good copy of %s (%s)",
                path,
                type(exc).__name__,
            )
        return fo


def _recent_failure(path: str) -> str | None:
    try:
        return lkg.recent_failure(folder=path)
    except Exception:  # noqa: BLE001 - an unreadable marker is ignored
        return None


def _refuse_empty_export(live: dict[str, str], previous: dict | None) -> None:
    """An export with none of our names in it is not proof that every knob was deleted.

    An empty body with exit 0 is also what a wrong path, a project with nothing in it or a
    half-broken login can produce, and believing it would rewrite the last-known-good copy
    with no knobs and lift every pause the next time Infisical is unreachable.  So when a
    usable copy still holds knobs, treat the empty answer as a failed fetch (reason
    ``empty``):  the copy is kept and every answer is marked stale.  Deleting every knob on
    purpose therefore reads as an outage until the copy ages out;  a project that has never
    had a knob has no such copy, so it is unaffected.
    """
    if live:
        return
    if previous and previous["knobs"]:
        raise source.FetchFailed("empty")


def _save_lkg(fo: _Folder) -> None:
    """After a good fetch of one folder, refresh that folder's copy with its non-secret knobs."""
    previous, _note = lkg.read(folder=fo.path)
    specs = [s for s in REGISTRY if s.folder == fo.path]
    fresh: dict[str, str] = {}
    for spec in specs:
        if spec.secret:
            continue
        raw = fo.live.get(spec.name)
        if raw is None or not raw.strip():
            continue  # unset in Infisical:  drop it
        ok, _typed = values.lenient(spec, raw)
        if ok:
            fresh[spec.name] = raw
        # else: a live value that fails validation is dropped, like an unset one.  A live
        # read answers the default for it, so the copy must too, or an outage right after a
        # typo would bring back an older and possibly wider value (an old "act").
    unchanged = previous is not None and previous["knobs"] == fresh
    if unchanged and lkg.age_seconds(previous) < lkg.REFRESH_SECONDS:
        return
    # Every secret this process holds, from any folder, so none can land in this copy.
    secrets = [
        f.live[s.name]
        for f in _state.folders.values()
        for s in REGISTRY
        if s.secret and s.name in f.live
    ]
    try:
        lkg.write(fresh, secrets, folder=fo.path)
    except (OSError, ValueError) as exc:
        offender = next(
            (
                k
                for k, v in fresh.items()
                for s in secrets
                if s and len(s) >= 8 and s.encode("utf-8") in json.dumps({k: v}).encode("utf-8")
            ),
            None,
        )
        log.warning(
            "fleet_knobs: could not write the last-known-good copy (%s)%s",
            type(exc).__name__,
            f"; a secret value is a substring of {offender}" if offender else "",
        )


def _coerce_default(spec: Spec, default: Any) -> Any:
    """The caller's default as a typed value, narrowed against the registry default."""
    base = values.default_value(spec)
    if default is _UNSET or default is None:
        return base
    try:
        typed = (
            default
            if spec.kind == "str" and isinstance(default, str)
            else values.parse(spec, default)
        )
    except ValueError:
        raise KnobError(f"the default given for {spec.name} is not a valid {spec.kind}") from None
    narrowed, _n, _i = values.narrow(spec, base, typed)
    return narrowed


def resolve(
    name: str,
    default: Any = _UNSET,
    kind: str | None = None,
    *,
    local: Any = None,
    stop: bool = False,
) -> Knob:
    """The full answer for `name`.  `get()` returns just ``.value``."""
    spec = _spec(name, kind)
    if spec.secret:
        if default is not _UNSET and default is not None:
            raise KnobError(f"{name} is a secret and takes no default")
        if local is not None or stop:
            raise KnobError(f"{name} is a secret and takes no local override")
        return _resolve_secret(spec)
    base_default = _coerce_default(spec, default)
    st = _state
    fo = _load(spec.folder)
    with st.lock:
        knob = _from_sources(spec, fo, base_default)
        if local is not None or stop:
            knob = _apply_local(spec, knob, local, stop)
        st.answers[name] = knob
        return knob


def _from_sources(spec: Spec, fo: _Folder, base_default: Any) -> Knob:
    if fo.fetch == "ok":
        raw = fo.live.get(spec.name)
        if raw is None or not raw.strip():
            return Knob(spec.name, base_default, "default", reason="unset")
        ok, typed = values.lenient(spec, raw)
        if ok:
            return Knob(spec.name, typed, "infisical")
        return Knob(spec.name, base_default, "default", reason="invalid")
    # Infisical could not be read, so no answer below is confirmed live:  all are stale.
    reason = f"unreachable:{fo.reason}"
    if fo.lkg is not None:
        ok, typed = values.lenient(spec, fo.lkg["knobs"].get(spec.name))
        if ok:
            return Knob(spec.name, typed, "lkg", stale=True, reason=reason)
        return Knob(spec.name, base_default, "default", stale=True, reason=reason)
    return Knob(
        spec.name, base_default, "default", stale=True, reason=f"{reason},lkg-{fo.lkg_note}"
    )


def _apply_local(spec: Spec, knob: Knob, local: Any, stop: bool) -> Knob:
    value = knob.value
    narrowed = ignored = False
    if local is not None:
        ok, typed = values.lenient(spec, local)
        if ok:
            value, narrowed, ignored = values.narrow(spec, value, typed)
        else:
            ignored = True
            log.warning("fleet_knobs: local value for %s is invalid and was ignored", spec.name)
    if stop:
        forced = values.most_conservative(spec, value)
        narrowed = narrowed or forced != value
        value = forced
    if ignored:
        log.warning("fleet_knobs: local value for %s would widen it and was ignored", spec.name)
    knob.value, knob.narrowed, knob.local_ignored = value, narrowed, ignored
    return knob


def _resolve_secret(spec: Spec) -> Knob:
    st = _state
    fo = _load(spec.folder)
    with st.lock:
        if fo.fetch != "ok":
            raise SecretUnavailable(spec.name, f"unreachable:{fo.reason}")
        raw = fo.live.get(spec.name)
        if raw is None or not raw.strip():
            raise SecretUnavailable(spec.name, "unset")
        ok, typed = values.lenient(spec, raw)
        if not ok:
            raise SecretUnavailable(spec.name, "malformed")
        knob = Knob(spec.name, typed, "infisical")
        st.answers[spec.name] = knob
        return knob


def get(
    name: str,
    default: Any = _UNSET,
    kind: str | None = None,
    *,
    local: Any = None,
    stop: bool = False,
) -> Any:
    """The value of knob `name`.

    `default` may only narrow the registry default, `kind` is checked against the registry,
    `local` is a job-local value (config file) and `stop` is true when a STOP file exists;
    both can only narrow.  Raises KnobError for an unknown knob and SecretUnavailable for a
    secret that cannot be had."""
    return resolve(name, default, kind, local=local, stop=stop).value


def _describe_folder(fo: _Folder) -> dict[str, Any]:
    out: dict[str, Any] = {"fetch": fo.fetch}
    if fo.reason:
        out["fetch_reason"] = fo.reason
    if fo.lkg is not None:
        out["lkg_age_seconds"] = lkg.age_seconds(fo.lkg)
    elif fo.fetch == "failed":
        out["lkg"] = fo.lkg_note
    return out


def folder_status(path: str) -> dict[str, Any]:
    """What this process learned about one folder:  ``fetch`` (ok, failed or not-attempted),
    with ``fetch_reason`` when there is one.  It reads memory only and never fetches, so a
    reader whose knobs share one folder can ask whether that folder's fetch worked."""
    st = _state
    with st.lock:
        found = st.folders.get(path)
        return _describe_folder(found) if found is not None else {"fetch": "not-attempted"}


def report() -> dict[str, Any]:
    """What this process learned, for a job's report:  names, sources and the stale flag.

    ``stale`` is true when any answer came from the last-known-good copy.  It carries no
    values, so it is safe to write into a report, a log or a digest.

    ``fetch`` is ``ok`` only when every folder this process fetched was read, ``failed`` when
    any was not, and ``fetch_reason`` and the copy's age are those of the first folder that
    failed (the root first).  A process that read only root knobs gets the same report as
    before folders existed;  one that read another folder also gets ``folders``, each folder's
    own fetch, reason and copy."""
    st = _state
    with st.lock:
        tried = [st.folders[p] for p in sorted(st.folders, key=lambda p: (p != ROOT_FOLDER, p))]
        tried = [f for f in tried if f.fetch != "not-attempted"]
        failed = [f for f in tried if f.fetch == "failed"]
        lead = failed[0] if failed else (tried[0] if tried else None)
        out: dict[str, Any] = {
            "schema": 1,
            "fetch": "failed" if failed else ("ok" if tried else "not-attempted"),
            "stale": any(k.stale for k in st.answers.values()),
            "knobs": {n: k.describe() for n, k in sorted(st.answers.items())},
        }
        if lead is not None:
            lead_view = _describe_folder(lead)
            for key in ("fetch_reason", "lkg_age_seconds", "lkg"):
                if key in lead_view:
                    out[key] = lead_view[key]
        if any(f.path != ROOT_FOLDER for f in tried):
            out["folders"] = {f.path: _describe_folder(f) for f in tried}
        return out
