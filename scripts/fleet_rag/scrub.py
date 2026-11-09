"""Secret scrubbing for corpus ingest and agent contributions.

Two layers:
  1. `scrub(text)` — regex redaction of well-known credential shapes.  Always applied.
  2. `gitleaks_flagged(path)` — gate: run gitleaks over a staged JSONL file and return the
     1-based line numbers it flags, so the caller can drop those rows.  Used when gitleaks is
     on PATH (it is on the fleet Mac).

The gate fails CLOSED.  When gitleaks is installed but cannot complete the scan (non-zero exit,
a fatal/error line on its output, a missing or unparsable report, a timeout) `GitleaksError` is
raised and the caller must not write the rows it was asked to gate.  Only a machine with no
gitleaks at all skips the gate, and that is reported by returning an empty set.

The JSON report gitleaks writes contains the raw matched secrets, so it lives in a private
0700 temp directory for the duration of one call and is removed before the function returns.

The fleet review of 2026-08-27 required a scrub because the corpus sources (board rows, Slack
transcripts, effort logs) contain transcripts of a fleet that has leaked tokens into logs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Iterable

# (kind, compiled pattern).  Order matters: specific shapes before the generic assignment rule.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack-token", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("tailscale-key", re.compile(r"\btskey-[A-Za-z0-9\-]{10,}\b")),
    ("infisical-token", re.compile(r"\b(?:st|mi)\.[A-Za-z0-9\-]{8,}\.[A-Za-z0-9\-]{8,}\.[A-Za-z0-9]{8,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("sentry-dsn", re.compile(r"https://[0-9a-f]{16,}@[a-z0-9.\-]+/[0-9]+")),
    ("url-basic-auth", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):([^/\s@]+)@")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-./+=]{16,}")),
    ("api-key-header", re.compile(r"(?i)\b(api[_\-]?key|x-api-key|authorization)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{16,}")),
    ("assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:secret|token|password|passwd|pwd|api[_\-]?key|client[_\-]?secret|private[_\-]?key|access[_\-]?key|dsn)[A-Z0-9_]*)"
        r"(\s*[:=]\s*)['\"]?(?!<redacted|\[REDACTED|\$\{|\$[A-Z_]|<[A-Z_]+>|\*{3})([A-Za-z0-9_\-./+=]{12,})['\"]?")),
    ("hex-64", re.compile(r"\b[0-9a-f]{64}\b")),
]

# gitleaks log lines look like "7:15PM FTL stat /x: no such file" / "7:15PM ERR ..." (colour
# codes may wrap the level).  INF and WRN ("leaks found: 2") are normal.
_GITLEAKS_FAILURE = re.compile(r"(?:^|[\s\x1b\[0-9;m])(?:FTL|ERR)(?:[\s\x1b]|$)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_GITLEAKS_DIR_MIN = (8, 19)     # `gitleaks dir` replaced `detect --no-git` in 8.19.0
_mode_cache: dict[str, str] = {}


class GitleaksError(RuntimeError):
    """gitleaks is installed but the scan could not be completed; the caller must fail closed."""


def _redact_assignment(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}[REDACTED:secret]"


def scrub(text: str) -> tuple[str, list[str]]:
    """Return (scrubbed_text, kinds_found).  Deterministic; safe to call repeatedly."""
    kinds: list[str] = []
    out = text
    for kind, pat in _PATTERNS:
        if kind == "assignment":
            new, n = pat.subn(_redact_assignment, out)
        elif kind == "url-basic-auth":
            new, n = pat.subn(lambda m: f"{m.group(1)}{m.group(2)}:[REDACTED:password]@", out)
        elif kind == "hex-64":
            # 64-hex is a sha256 in most of our docs; only redact when it sits next to a key word.
            new, n = re.subn(r"(?i)(secret|token|key|password)(\W{1,3})[0-9a-f]{64}\b",
                             lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:hex64]", out)
        else:
            new, n = pat.subn(f"[REDACTED:{kind}]", out)
        if n:
            kinds.append(kind)
            out = new
    return out, kinds


# --------------------------------------------------------------------------- gitleaks gate

def _gitleaks_version(exe: str, timeout: int = 30) -> tuple[int, ...] | None:
    try:
        p = subprocess.run([exe, "version"], capture_output=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", _ANSI.sub("", p.stdout.decode("utf-8", errors="replace")))
    if p.returncode != 0 or not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


def _gitleaks_mode(exe: str) -> str:
    """'dir' (8.19+) or 'detect' (older builds without the dir subcommand).  Cached per binary."""
    mode = _mode_cache.get(exe)
    if mode:
        return mode
    ver = _gitleaks_version(exe)
    if ver is not None:
        mode = "dir" if ver[:2] >= _GITLEAKS_DIR_MIN else "detect"
    else:
        # Version output unreadable: probe the subcommand itself.
        try:
            p = subprocess.run([exe, "dir", "--help"], capture_output=True, timeout=30, check=False)
            mode = "dir" if p.returncode == 0 else "detect"
        except (subprocess.TimeoutExpired, OSError):
            mode = "detect"
    _mode_cache[exe] = mode
    return mode


def _gitleaks_cmd(exe: str, mode: str, target: str, report: str) -> list[str]:
    common = ["--report-format", "json", "--report-path", report, "--no-banner", "--exit-code", "0"]
    if mode == "dir":
        return [exe, "dir", target, *common]
    return [exe, "detect", "--no-git", "--source", target, *common]


def gitleaks_flagged(jsonl_path: str, timeout: int = 300) -> set[int]:
    """Line numbers (1-based) in a JSONL file that gitleaks flags.

    Returns an empty set only when gitleaks is not installed.  Raises GitleaksError whenever
    gitleaks is present but the scan did not verifiably complete.
    """
    exe = shutil.which("gitleaks")
    if not exe:
        return set()
    if not os.path.isfile(jsonl_path):
        raise GitleaksError(f"staged file missing: {jsonl_path}")
    mode = _gitleaks_mode(exe)
    tmpdir = tempfile.mkdtemp(prefix="fleet-rag-gitleaks-")    # mkdtemp is 0700
    report = os.path.join(tmpdir, "report.json")
    try:
        try:
            proc = subprocess.run(_gitleaks_cmd(exe, mode, jsonl_path, report), capture_output=True,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise GitleaksError(f"gitleaks timed out after {timeout}s") from None
        except OSError as e:
            raise GitleaksError(f"gitleaks could not be run: {type(e).__name__}") from None
        out = _ANSI.sub("", proc.stdout.decode("utf-8", errors="replace"))
        err = _ANSI.sub("", proc.stderr.decode("utf-8", errors="replace"))
        if proc.returncode != 0:
            raise GitleaksError(f"gitleaks {mode} exited {proc.returncode}: {_last_line(err or out)}")
        if _GITLEAKS_FAILURE.search(out) or _GITLEAKS_FAILURE.search(err):
            raise GitleaksError(f"gitleaks {mode} reported a failure: {_last_line(err or out)}")
        if not os.path.isfile(report):
            raise GitleaksError(f"gitleaks {mode} wrote no report")
        try:
            with open(report, encoding="utf-8") as fh:
                raw = fh.read().strip()
            findings = json.loads(raw) if raw else []
        except (OSError, json.JSONDecodeError) as e:
            raise GitleaksError(f"gitleaks report unreadable: {type(e).__name__}") from None
        if not isinstance(findings, list):
            raise GitleaksError("gitleaks report is not a JSON list")
        return {int(f.get("StartLine", 0)) for f in findings if isinstance(f, dict) and f.get("StartLine")}
    finally:
        try:
            os.unlink(report)
        except OSError:
            pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "no output")[:200]


def scrub_rows(rows: Iterable[dict]) -> tuple[list[dict], int]:
    """Scrub the `text` of each row in place; returns (rows, number_of_rows_touched)."""
    touched = 0
    out = []
    for r in rows:
        new, kinds = scrub(r.get("text", ""))
        if kinds:
            touched += 1
            r = {**r, "text": new, "scrubbed": kinds}
        out.append(r)
    return out, touched
