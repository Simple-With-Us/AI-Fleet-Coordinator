#!/usr/bin/env python3
"""Compare official REST operations to relay route evidence, without guessing parity.

Usage: audit-github-capabilities.py OPENAPI_JSON [--output FILE]
This does not fetch data, call GitHub, mutate account state, or prove live access.
"""

import argparse
import ast
import collections
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json"


def route_shape(path):
    path = path.split("?", 1)[0]
    path = re.sub(r"\{[^{}]*\}", "{}", path)
    return path.rstrip("/")


def string_template(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value if isinstance(value, ast.Constant) else "{}" for value in node.values
        )
    return None


def route_candidates(path):
    # The repo variable is owner/name, two REST path segments.
    path = path.replace("/repos/{}", "/repos/{owner}/{repo}", 1)
    return route_shape(path)


def relay_evidence():
    tree = ast.parse((ROOT / "git-relay/git_relay/workflow_verbs.py").read_text())
    evidence = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("wf_"):
            continue
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr == "_get" and call.args:
                method, path = "GET", string_template(call.args[0])
            elif call.func.attr == "_call" and len(call.args) >= 2:
                method, path = string_template(call.args[0]), string_template(call.args[1])
            else:
                continue
            if method and path and path.startswith("/"):
                evidence.append(
                    {
                        "handler": fn.name,
                        "method": method,
                        "route_template": path,
                        "shape": route_candidates(path),
                    }
                )
    return evidence


def inventory(spec, exposed):
    evidence = relay_evidence()
    operations = []
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI paths must be an object; previous inventory remains intact")
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if (
                method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
                or not isinstance(operation, dict)
                or "operationId" not in operation
            ):
                continue
            candidates = sorted(
                {
                    item["handler"]
                    for item in evidence
                    if item["method"] == method.upper() and item["shape"] == route_shape(path)
                }
            )
            operations.append(
                {
                    "operation_id": operation["operationId"],
                    "method": method.upper(),
                    "path": path,
                    "category": (
                        operation["tags"][0]
                        if isinstance(operation.get("tags"), list)
                        and operation["tags"]
                        and isinstance(operation["tags"][0], str)
                        else "other"
                    ),
                    "candidate_handlers": candidates,
                    "status": "route-evidence-needs-semantic-audit"
                    if candidates
                    else "no-direct-route-evidence",
                }
            )
    return {
        "source": SOURCE,
        "classification_note": "Route evidence is a candidate match, not supported/full parity or live permission proof. Shared/dynamic helpers may not be recognized. GraphQL requires separate semantic inventory.",
        "operations": operations,
        "relay_route_evidence": evidence,
        "exposed_tools": sorted(exposed),
    }


def previous_exposed(path):
    try:
        previous = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    exposed = previous.get("exposed_tools", []) if isinstance(previous, dict) else []
    return (
        [item for item in exposed if isinstance(item, str) and item.startswith("gr_")]
        if isinstance(exposed, list)
        else []
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=pathlib.Path)
    parser.add_argument(
        "--output", type=pathlib.Path, default=ROOT / "docs/github-capabilities/inventory.json"
    )
    args = parser.parse_args()
    result = inventory(json.loads(args.spec.read_text()), previous_exposed(args.output))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "operations": len(result["operations"]),
                "statuses": dict(collections.Counter(o["status"] for o in result["operations"])),
            }
        )
    )


if __name__ == "__main__":
    main()
