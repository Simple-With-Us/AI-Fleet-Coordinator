"""A setting the Operator or the wake router reads must be a registered knob, or be listed here with a reason.

Muster's Controls tab lists the knob registry (`fleet_knobs/registry.json`), so a setting that lands outside
the registry is a setting the owner cannot see or change from Muster.  This test reads the Operator and
wake-router scripts and fails for any setting they read that the registry does not hold.

What counts as "reads a setting" is deliberately narrow, so the test does not flake:

  * `<env>.get("NAME")`, `<env>.pop(...)`, `<env>.setdefault(...)`, `<env>["NAME"]` and `os.getenv("NAME")`,
    where `<env>` is `os.environ` or a name or attribute with "env" in it (`env`, `environment`, `self._env`);
  * any string literal that is an ALL_CAPS name starting `WAKE_ROUTER_`, `OPERATOR_` or `SIMPLE_TOOLS_OPERATOR_`
    (the constants the scripts define their settings under).

Only the literal name is looked at, so a name built at run time is not seen.  The files are parsed, never run.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
REGISTRY_JSON = SCRIPTS / "fleet_knobs" / "registry.json"

NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
OPERATOR_PREFIX = re.compile(r"^(WAKE_ROUTER|OPERATOR|SIMPLE_TOOLS_OPERATOR)_")

# The Operator and wake-router scripts, relative to scripts/.
SCANNED = sorted(
    {
        p.relative_to(SCRIPTS).as_posix()
        for pattern in ("simple_tools/wake*.py", "simple_tools/operator_*.py")
        for p in SCRIPTS.glob(pattern)
    }
    | {"simple_tools/router.py", "simple_tools/claude_wake.py"}
)

# Settings read outside the registry today, each with the reason it is not a knob.  A new Operator or
# wake-router setting must be registered in fleet_knobs/registry.py (and INFISICAL.md), not added here.
# The first group are settings read from the environment that the registry does not hold: they are listed so
# the gap is visible and nothing else can join them silently.  Remove a row when its knob is registered.
# AFC-773 reviewed each row: only the Linear dry-run switch is a tunable the owner would change, and it waits
# for the Eavesdropper to read knobs from Infisical and for a folder to be chosen.  The wake alerter's
# settings are per host, and the alerter has no Infisical access.  The DM mode awaits an owner decision.
ALLOWLIST: dict[str, str] = {
    # Settings read from the environment that are not registered knobs, each with its reason.
    "SIMPLE_TOOLS_OPERATOR_DM_MODE": "staged Operator DM policy (legacy or clarify); whether to register or remove it is an owner decision (AFC-773).",
    "OPERATOR_ZULIP_USER_ID": "the Operator's Zulip identity pin, an identity and not an owner-tunable: the environment wins, else `[daemon] operator_user_id` in the host's listener.toml, and on the server the Eavesdropper's loader fills it from Infisical /operator; so not a knob.",
    "OPERATOR_ALERTER_HOOK": "a command line the per-host wake alerter runs; the alerter has no Infisical access.",
    "OPERATOR_ALERTER_HOOK_TIMEOUT": "the per-host wake alerter's hook time limit, read at import; the alerter has no Infisical access.",
    "OPERATOR_LISTENER_URL": "the address one host's wake alerter polls, a per-host URL.",
    "OPERATOR_WAKE_CHANNEL": "written into the hook child's environment for each wake, never read as a setting.",
    "OPERATOR_WAKE_TOPIC": "written into the hook child's environment for each wake, never read as a setting.",
    "OPERATOR_SEAT": "the seat one host's wake alerter speaks as, a per-host identity.",
    "OPERATOR_LINEAR_DRY_RUN": "the one tunable here: a switch that makes the Operator's Linear mirror write nothing, read once at engine build as the exact text 1; register it as a knob when the Eavesdropper reads knobs from Infisical and its folder is chosen (AFC-773).",
    # Where a secret or a credential is found:  names of locations and identities, never values.
    "OPERATOR_ROUTINE_KEY_NAME": "name of the secret that holds the routine key, not the secret.",
    "OPERATOR_SECRET_PATH": "Infisical folder of a secret, not a value.",
    "OPERATOR_SECRET_PROJECT": "Infisical project of a secret, not a value.",
    "OPERATOR_LINEAR_INFISICAL_TOKEN": "bootstrap credential for reading Infisical itself;  cannot live in Infisical.",
    "OPERATOR_ZULIP_EMAIL": "the Operator bot's identity, resolved through the credential store.",
    "FLEET_SECRETS_BIN": "path to the fleet-secrets binary on this host.",
    "GH_TOKEN": "set for a child process by the GitHub client, not read as a setting.",
    "ZULIP_SITE": "the Zulip site address, a host fact shared by every seat.",
    # Per-host paths and test seams, not owner-tunable behaviour.
    "OPERATOR_STATE": "local state directory.",
    "OPERATOR_WAKE_STATE": "the wake's state word, written into the hook child's environment, never read as a setting.",
    "OPERATOR_WAKE_ID": "per-run wake id passed to a child process.",
    "OPERATOR_LINEAR_STATE": "local state directory.",
    "SIMPLE_TOOLS_OPERATOR_REGISTRY": "path of the local registry file, a per-host seam.",
    "SIMPLE_TOOLS_STATE_DIR": "local state directory.",
    "CLAUDE_CONFIG_DIR": "Claude Code's own variable.",
    "CLAUDE_WAKE_STATE": "local state directory.",
}


def _env_like(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" or "env" in node.attr.lower()
    if isinstance(node, ast.Name):
        return "env" in node.id.lower()
    return False


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def settings_read(source: str) -> set[str]:
    """The setting names the source reads, by the narrow rules in the module docstring."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        name = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            attr = node.func.attr
            if (
                attr in ("get", "pop", "setdefault") and _env_like(node.func.value)
            ) or attr == "getenv":
                name = _literal(node.args[0])
        elif isinstance(node, ast.Subscript) and _env_like(node.value):
            name = _literal(node.slice)
        elif isinstance(node, ast.Constant):
            value = _literal(node)
            if value and OPERATOR_PREFIX.match(value):
                name = value
        if name and NAME.match(name):
            found.add(name)
    return found


def read_all() -> dict[str, set[str]]:
    """Setting name -> the scanned files that read it."""
    where: dict[str, set[str]] = {}
    for relative in SCANNED:
        path = SCRIPTS / relative
        if not path.exists():
            continue
        for name in settings_read(path.read_text(encoding="utf-8")):
            where.setdefault(name, set()).add(relative)
    return where


def registered() -> set[str]:
    data = json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))
    return {knob["name"] for knob in data["knobs"]}


class OperatorKnobCoverage(unittest.TestCase):
    def test_every_setting_the_operator_reads_is_registered_or_allowlisted(self) -> None:
        known = registered()
        outside = {n: f for n, f in read_all().items() if n not in known and n not in ALLOWLIST}
        self.assertEqual(
            outside,
            {},
            "These settings are read by an Operator or wake-router script but are not in the knob registry, "
            "so Muster cannot show or change them.  Register each in scripts/fleet_knobs/registry.py "
            "(then regenerate registry.json and INFISICAL.md), or allowlist it here with a reason.",
        )

    def test_the_allowlist_has_no_stale_or_registered_rows(self) -> None:
        known = registered()
        seen = read_all()
        for name, reason in ALLOWLIST.items():
            self.assertTrue(reason.strip(), f"{name} needs a reason")
            self.assertNotIn(
                name, known, f"{name} is registered now;  remove it from the allowlist"
            )
            self.assertIn(
                name,
                seen,
                f"{name} is no longer read by a scanned script;  remove it from the allowlist",
            )

    def test_the_scan_finds_the_known_wake_router_knobs(self) -> None:
        seen = read_all()
        known = registered()
        for name in (
            "WAKE_ROUTER_DELIVERY_ENABLED",
            "WAKE_ROUTER_KEY",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL",
        ):
            self.assertIn(name, seen, f"the scan no longer sees {name}")
            self.assertIn(name, known)

    def test_the_detector_is_narrow(self) -> None:
        source = (
            "import os\n"
            'a = os.environ.get("OPERATOR_X_ONE")\n'
            'b = env["SOME_OTHER_NAME"]\n'
            'c = {"ZZ_TOP_NAME": 1}\n'  # a dict key is not a read
            'd = "NOT_PREFIXED_CONSTANT"\n'  # an unprefixed literal is not a read
            'e = "WAKE_ROUTER_THING"\n'
            'f = other.get("MIXED_CASE_name")\n'
            'g = mapping.get("PLAIN_MAP_KEY")\n'  # not an environment-like mapping
        )
        self.assertEqual(
            settings_read(source), {"OPERATOR_X_ONE", "SOME_OTHER_NAME", "WAKE_ROUTER_THING"}
        )


if __name__ == "__main__":
    unittest.main()
