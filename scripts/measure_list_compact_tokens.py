#!/usr/bin/env python3
"""Synthetic token savings for AFC-756 compact list/search presentation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from simple_tools.mcp import list_presentation as LP

BODY = " ".join(["paragraph"] * 120)


def zulip_messages(n: int) -> list[dict]:
    return [
        {
            "id": i,
            "sender": "Jay",
            "body": BODY,
            "channel": "work-in-progress",
            "topic": "fleet",
        }
        for i in range(1, n + 1)
    ]


def linear_issues(n: int) -> dict:
    return {
        "issues": {
            "nodes": [
                {
                    "identifier": f"AFC-{i}",
                    "title": f"Issue {i}",
                    "description": BODY,
                }
                for i in range(1, n + 1)
            ]
        }
    }


def notes_list(n: int) -> dict:
    return {
        "notes": [
            {"title": f"[AFC, Agent] Note {i}", "id": f"x-coredata://{i}"} for i in range(1, n + 1)
        ],
        "total": n,
    }


SAMPLES = [
    (
        "zulip_read_topic (25 msgs)",
        zulip_messages(25),
        lambda d: [
            {"body": m["body"][: LP.COMPACT_BODY_LIMIT] + "..."}
            for m in d[: LP.COMPACT_DEFAULT_LIMIT]
        ],
    ),
    (
        "linear_list_issues (30)",
        linear_issues(30),
        lambda d: LP.compact_linear_graphql(d, top_n=LP.COMPACT_DEFAULT_LIMIT)[0],
    ),
    ("notes_list (100)", notes_list(100), lambda d: LP.compact_notes_list_result(d)),
]


def main() -> int:
    rows = []
    total_full = 0
    total_compact = 0
    for label, full_payload, compact_fn in SAMPLES:
        compact_payload = compact_fn(full_payload)
        full_t = LP.approximate_tokens(full_payload)
        compact_t = LP.approximate_tokens(compact_payload)
        saved = 100.0 * (1.0 - compact_t / full_t) if full_t else 0.0
        rows.append((label, full_t, compact_t, saved))
        total_full += full_t
        total_compact += compact_t
    print(
        json.dumps(
            {
                "samples": [
                    {
                        "label": r[0],
                        "full_tokens": r[1],
                        "compact_tokens": r[2],
                        "saved_pct": round(r[3], 1),
                    }
                    for r in rows
                ],
                "total_full_tokens": total_full,
                "total_compact_tokens": total_compact,
                "total_saved_pct": round(100.0 * (1.0 - total_compact / total_full), 1)
                if total_full
                else 0.0,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
