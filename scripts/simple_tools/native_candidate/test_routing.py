import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "protocol_routing", Path(__file__).with_name("native_routing.py")
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / ".zcode/v2/tasks-index.sqlite"
        self.path.parent.mkdir(parents=True)
        self.c = sqlite3.connect(self.path)
        self.addCleanup(self.c.close)
        self.c.executescript("""
CREATE TABLE tasks (task_id,workspace_key,workspace_path,workspace_identity,host_id,root_path,project_id,execution_directories,worktree_root);
CREATE TABLE projects (project_id,host_id,deleted);
CREATE TABLE project_roots (project_id,root_path);
INSERT INTO tasks VALUES ('session','remote:fixture','/fixture','remote:fixture','server-fixture','/fixture','project',NULL,NULL);
INSERT INTO projects VALUES ('project','server-fixture',0);
INSERT INTO project_roots VALUES ('project','/fixture');
""")
        self.c.commit()
        self.path.chmod(0o600)
        self.binding = {"session_id": "session", "workspace": "/fixture"}

    def change(self, sql):
        self.c.execute(sql)
        self.c.commit()

    def test_exact_namespace_and_project_change_invalidates_stamp(self):
        routing, before = m.route(self.home, self.binding)
        self.assertEqual(routing, {"workspace_identity": "remote:fixture"})
        self.change("UPDATE projects SET deleted=1")
        with self.assertRaises(ValueError):
            m.route(self.home, self.binding)

    def test_identity_rotation_changes_route_and_stamp(self):
        old = m.route(self.home, self.binding)
        self.change(
            "UPDATE tasks SET workspace_key='remote:second',workspace_identity='remote:second'"
        )
        self.assertNotEqual(m.route(self.home, self.binding), old)

    def test_local_absence_is_explicit(self):
        self.change(
            "UPDATE tasks SET host_id='local',workspace_key='/fixture',workspace_identity=NULL"
        )
        self.assertEqual(m.route(self.home, self.binding)[0], {"workspace_identity": None})

    def test_missing_duplicate_and_mismatched_routes_refuse(self):
        mutations = [
            "DELETE FROM tasks",
            "INSERT INTO tasks SELECT * FROM tasks",
            "UPDATE tasks SET workspace_path='/other'",
            "UPDATE tasks SET root_path='/other'",
            "UPDATE tasks SET workspace_key='/fixture'",
            "UPDATE tasks SET workspace_identity=NULL",
            "UPDATE tasks SET workspace_key='',workspace_identity=''",
            "UPDATE tasks SET execution_directories='[1]'",
            "UPDATE tasks SET worktree_root='/fixture'",
            "UPDATE projects SET host_id='other'",
            "DELETE FROM projects",
            "UPDATE project_roots SET root_path='/other'",
            "INSERT INTO project_roots VALUES ('project','/other')",
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.c.execute("SAVEPOINT fixture")
                self.c.execute(mutation)
                self.c.commit()
                with self.assertRaises(ValueError):
                    m.route(self.home, self.binding)
                # Restore the whole fixture after committed mutations.
                self.c.executescript(
                    "DELETE FROM tasks;DELETE FROM projects;DELETE FROM project_roots;INSERT INTO tasks VALUES ('session','remote:fixture','/fixture','remote:fixture','server-fixture','/fixture','project',NULL,NULL);INSERT INTO projects VALUES ('project','server-fixture',0);INSERT INTO project_roots VALUES ('project','/fixture');"
                )
                self.c.commit()

    def test_writable_or_symlink_database_refuses(self):
        self.path.chmod(0o666)
        with self.assertRaises(ValueError):
            m.route(self.home, self.binding)
        self.path.chmod(0o600)
        target = self.path.with_suffix(".real")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            m.route(self.home, self.binding)


if __name__ == "__main__":
    unittest.main()
