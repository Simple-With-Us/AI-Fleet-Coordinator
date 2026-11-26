"""The owner's Infisical ruling, enforced here so CI fails before review does.

The ruling (Jay, Nov 22 2026):  every secret and every variable resolves from
Infisical,  the environment is never a source,  and a consumer's own store is the
authority for its own narrow grant.  This module pins the three things a reader
of the code cannot check by eye:

  1.  every consumer names the account and the folder it reads,  and the document
      says exactly what the code says;
  2.  no consumer reads a secret value out of the environment;
  3.  no consumer source carries a token value.

It lives in the suite the "simple-chat CLI tests" job already runs,  so the next
environment read fails CI instead of review,  and nothing under .github/workflows/
has to change for that to be true (the relay refuses workflow writes: OPS-1108).
"""

import importlib
import importlib.util
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
REPO = SCRIPTS.parent
DOC = REPO / "INFISICAL.md"

FLEET_OPS = "Fleet-OPS"
COORDINATOR = "AI Fleet Coordinator"

# The consumer sites this guard covers.  Each one resolves its own grant.
CONSUMER_SITES = (
    "simple_tools/zulip.py",
    "simple_tools/notes_client.py",
    "simple_tools/daemon.py",
    "simple_tools/mcp/fleetlink_tools.py",
    "fleet_rag/core.py",
    "fleet_rag/public_fallback.py",
    "zulip_linkifiers.py",
    "zulip_provision_bots.py",
    "github-outbox-bridge.py",
    "simple-tools-mcp/install_seat_key.py",
    "fleet-work-backup/fleet_work_backup.py",
)

# A named environment read of one of these shapes is a secret read,  and the
# ruling allows none of them in a consumer site.  A name that is a knob,  a
# project selector or a path is not one of these.
SECRET_NAME = re.compile(
    r"(?:^|_)(?:TOKEN|TOKENS|SECRET|SECRETS|PASSWORD|PASSWORDS|CREDENTIAL|CREDENTIALS"
    r"|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|SIGNING_KEY|ENCRYPTION_KEY)(?:_|$)"
)
ENV_READ = re.compile(
    r"os\.environ\.get\(\s*[\"']([A-Z0-9_]+)[\"']"
    r"|os\.environ\[\s*[\"']([A-Z0-9_]+)[\"']"
    r"|getenv\(\s*[\"']([A-Z0-9_]+)[\"']"
)

# The document's own paragraph names every account this repository's consumers
# read,  and the folder each one is scoped to.
DOC_ACCOUNT = re.compile(r"`([a-z0-9][a-z0-9-]*)`\s*\(([^)]*)\)")
DOC_FOLDER = re.compile(r"`([^`]+)`")


def _load(name: str, relative: str):
    """Load one consumer module by path,  the way the site loads its own code."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ConformanceTests(unittest.TestCase):
    """The ruling, checked against the code,  the document and the provider."""

    def _document_accounts(self) -> dict:
        """The account table the document states,  parsed from its own paragraph."""
        text = DOC.read_text(encoding="utf-8")
        start = text.index("The accounts this repository's consumers name.")
        end = text.index("Setup for a new consumer", start)
        paragraph = text[start:end]
        accounts = {}
        for match in DOC_ACCOUNT.finditer(paragraph):
            account, inside = match.group(1), match.group(2)
            folders = DOC_FOLDER.findall(inside)
            if not folders:
                continue
            project = COORDINATOR if COORDINATOR in inside else FLEET_OPS
            accounts[account] = (project, folders[-1])
        return accounts

    def _code_accounts(self) -> dict:
        """The account table the code states,  read from the consumers themselves."""
        accounts = {}

        from simple_tools import notes_client as NC
        from simple_tools import zulip as Z

        for folder, account in Z.INFISICAL_TOKEN_ACCOUNTS.items():
            accounts[account] = (FLEET_OPS, folder)
        for (_project, folder), account in NC.TOKEN_ACCOUNTS.items():
            accounts[account] = (COORDINATOR, folder)

        import fleet_rag.core as C
        import zulip_linkifiers as L
        import zulip_provision_bots as P

        accounts[C.FLEET_OPS_TOKEN_ACCOUNT] = (FLEET_OPS, C.FLEET_OPS_FOLDER)
        for folder, account in L.TOKEN_ACCOUNTS.items():
            accounts[account] = (FLEET_OPS, folder)
        for folder, account in P.TOKEN_ACCOUNTS.items():
            accounts[account] = (FLEET_OPS, folder)

        seat_key = _load("conformance_install_seat_key", "simple-tools-mcp/install_seat_key.py")
        for folder, account in seat_key.TOKEN_ACCOUNTS.items():
            accounts[account] = (FLEET_OPS, folder)

        backup = _load("conformance_fleet_work_backup", "fleet-work-backup/fleet_work_backup.py")
        accounts[backup.CREDENTIAL_TOKEN_ACCOUNT] = (FLEET_OPS, backup.CREDENTIAL_FOLDER)

        from simple_tools.mcp import fleetlink_tools as FT

        accounts[FT.READER_ACCOUNT] = (COORDINATOR, FT.INFISICAL_PATH)

        bridge = _load("conformance_outbox_bridge", "github-outbox-bridge.py")
        # The bridge reads one folder per call,  and the document names the one
        # it reads:  Fleet-OPS prod /zulip.
        accounts[bridge.TOKEN_ACCOUNT] = (FLEET_OPS, "/zulip")

        # The alert-ingress self test holds its own grant for the Coordinator
        # project,  and the sheet reader holds its own for the Fleet-OPS one.
        # The document names both,  so the code side reads both.
        alert = _load("conformance_alert_selftest", "alert-ingress/selftest.py")
        for (project, folder), account in alert.TOKEN_ACCOUNTS.items():
            accounts[account] = (COORDINATOR if project == "ai-fleet-coordinator" else FLEET_OPS, folder)

        # The sheet reader is JavaScript,  so its two constants are read from the
        # source.  A constant that is renamed stops the guard,  which is the point:
        # the document and the code have to keep naming the same grant.
        sheet = (SCRIPTS / "sheet-read.mjs").read_text(encoding="utf-8")
        sheet_account = re.search(r'const TOKEN_ACCOUNT = "([^"]+)"', sheet)
        sheet_folder = re.search(r'const SECRET_FOLDER = "([^"]+)"', sheet)
        self.assertIsNotNone(sheet_account, "the sheet reader names no token account")
        self.assertIsNotNone(sheet_folder, "the sheet reader names no folder")
        accounts[sheet_account.group(1)] = (FLEET_OPS, sheet_folder.group(1))
        return accounts

    def test_the_code_and_the_document_name_the_same_account_and_folder(self):
        documented = self._document_accounts()
        coded = self._code_accounts()
        self.assertTrue(documented, "the document names no account")
        self.assertEqual(
            documented,
            coded,
            "the document and the code disagree about an account's project or folder",
        )

    def test_the_backup_job_reads_the_folder_where_its_own_names_live(self):
        # Every one of the job's names is a literal in the Fleet-OPS prod
        # /global-api-keys folder,  and a grant on the project root would be a
        # wider grant than this job needs.
        backup = _load("conformance_fleet_work_backup_folder", "fleet-work-backup/fleet_work_backup.py")
        self.assertEqual(backup.CREDENTIAL_FOLDER, "/global-api-keys")
        self.assertEqual(backup.CREDENTIAL_TOKEN_ACCOUNT, "fleet-work-backup-ro")
        self.assertEqual(
            self._document_accounts()["fleet-work-backup-ro"],
            (FLEET_OPS, "/global-api-keys"),
        )

    def test_no_consumer_reads_a_secret_value_from_the_environment(self):
        offences = []
        for relative in CONSUMER_SITES:
            path = SCRIPTS / relative
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            for match in ENV_READ.finditer(text):
                name = next(group for group in match.groups() if group)
                if SECRET_NAME.search(name):
                    offences.append("%s: %s" % (relative, name))
        self.assertEqual(
            offences,
            [],
            "a consumer reads a secret value out of the environment:  %s" % offences,
        )

    def test_no_consumer_source_carries_a_token_value(self):
        sys.path.insert(0, str(SCRIPTS / "simple_tools"))
        import secretscan

        offences = []
        for relative in CONSUMER_SITES:
            path = SCRIPTS / relative
            if not path.exists():
                continue
            finding = secretscan.scan_file("AI-Fleet-Coordinator", relative, path.read_text(encoding="utf-8"))
            if finding:
                offences.append(relative)
        self.assertEqual(offences, [], "a consumer source carries a secret:  %s" % offences)


if __name__ == "__main__":
    unittest.main()
