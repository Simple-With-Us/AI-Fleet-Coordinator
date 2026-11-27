"""Read-only protocol routing, never session or delivery authority.

The owning session's CLI row and registry checks remain separate and required.
A tasks-index identity selects the host's existing project/event namespace only.
"""

import json
import os
import sqlite3
import stat
from pathlib import Path


def route(home, binding):
    path = Path(home) / ".zcode/v2/tasks-index.sqlite"
    if path.resolve() != path.absolute():
        raise ValueError("routing-custody")
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise ValueError("routing-custody")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT workspace_key, workspace_path, workspace_identity, host_id, "
            "root_path, project_id, execution_directories, worktree_root FROM tasks "
            "WHERE task_id=?",
            (binding["session_id"],),
        ).fetchmany(2)
        if len(rows) != 1:
            raise ValueError("routing-ambiguous-or-missing")
        row = dict(rows[0])
        workspace = binding["workspace"]
        if (
            row["workspace_path"] != workspace
            or row["root_path"] != workspace
            or row["worktree_root"] is not None
            or row["execution_directories"] not in (None, "[]")
        ):
            raise ValueError("routing-workspace")
        identity = row["workspace_identity"]
        if identity is not None and (
            not isinstance(identity, str)
            or not identity
            or len(identity) > 4096
            or identity != identity.strip()
            or any(ord(c) < 32 for c in identity)
        ):
            raise ValueError("routing-identity")
        if row["workspace_key"] != (identity or workspace):
            raise ValueError("routing-identity")
        if identity:
            project = connection.execute(
                "SELECT host_id, deleted FROM projects WHERE project_id=?",
                (row["project_id"],),
            ).fetchone()
            roots = connection.execute(
                "SELECT root_path FROM project_roots WHERE project_id=?",
                (row["project_id"],),
            ).fetchmany(2)
            if (
                not project
                or project["host_id"] != row["host_id"]
                or project["deleted"] != 0
                or len(roots) != 1
                or roots[0]["root_path"] != workspace
            ):
                raise ValueError("routing-project")
        elif row["host_id"] not in (None, "local"):
            raise ValueError("routing-identity-required")
        return {"workspace_identity": identity}, json.dumps(
            {
                "task": row,
                "project": dict(project) if identity else None,
                "roots": [dict(r) for r in roots] if identity else [],
            },
            sort_keys=True,
        )
    finally:
        connection.close()
