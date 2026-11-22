import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from simple_tools import operator_registry as OR

class ModuleCLI(unittest.TestCase):
 def test_history_resolve_and_old_schema_readers_in_real_module_process(self):
  with tempfile.TemporaryDirectory()as d:
   db=str(Path(d)/'registry.db');OR.SessionRegistry(db).close_db()
   row=OR.register_session(db,platform='testbed',verified_host='mac',profile_scope='original',native_session_id='native-cli-session',seat='CODEX',source='test',issuer='OPERATOR')
   scripts=Path(__file__).resolve().parents[2]
   env={'PATH':os.environ['PATH'],'HOME':d,'PYTHONPATH':str(scripts)}
   for old in (False,True):
    if old:
     import sqlite3
     with sqlite3.connect(db)as c:c.execute('DROP TABLE session_pr_bindings')
    for command,extra in [('history',[]),('resolve',['--letter',row['label']])]:
     with self.subTest(old_schema=old,command=command):
      p=subprocess.run([sys.executable,'-m','simple_tools.operator_registry',command,'--db',db,'--platform','testbed',*extra],cwd=d,env=env,capture_output=True,text=True,timeout=15)
      self.assertEqual(p.returncode,0,p.stdout+p.stderr)
      data=json.loads(p.stdout);actual=data[0]if isinstance(data,list)else data
      self.assertEqual(actual['native_session_id'],'native-cli-session');self.assertEqual(actual['bound_prs'],[])
