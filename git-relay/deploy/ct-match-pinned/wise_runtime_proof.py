"""Fixed own-WISE proof over the already working server-to-Mac SSH route.

Source preparation only. No credential copy or other-seat binding. Each proof is
one finite subprocess, not a daemon or persistent tunnel. The host supervisor
separately binds actual service identity/config/code digests before this proof.
"""

import json
import subprocess
import tempfile
from pathlib import Path

MAC_HOST = "100.113.106.39"
MAC_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH7VILvQ5Z8jfqRY8QYhbNwlHDxgyEURmOuwYdu609ff"
BEFORE = "8762d7c50c0c09b92e34f640643f77b71f78524e412233ecb266f9f9a4b84802"
AFTER = "8bd0ea0b3a9fe2782d490c97065e4aff2ec7f38a2630abb04e98eca8e491e235"
REMOTE_SOURCE = r"""
import hashlib,json,os,stat,subprocess
from pathlib import Path
ROOT=Path('/Users/jay/.local/share/simple-git/releases/0fac2e8417218ff8791da9d699b66de0770bddca')
PINS={
 'scripts/simple-git':('7c375cbe1b13c836b382871e05f63a4ceb5590a3d967912ddc000ec42a873a92',0o755),
 'scripts/git-tools/git':('8b8cc9d0b1e0b1f58d82d024deaa96edb5fd470f990b321ed3e2051a0c06ff2e',0o755),
 'scripts/git-tools/export_catalog.py':('727f0e66aee78660c01cb9d36e5861a547fd2d962664e54f1397e9d4d57ceed9',0o644),
 'scripts/git-tools/catalog.json':('a92812f6893791957552246167fe6f46baaf198f73f0d5be427f668ce05971ea',0o644),
 'scripts/simple_tools/secretscan.py':('f29d222d0d6af9726b93c268134cf4fe4c84af10a60da9460250aef9892c253c',0o644),
}
def read(path,mode):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 with os.fdopen(fd,'rb') as h:
  info=os.fstat(h.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_uid!=501 or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=mode:raise ValueError()
  return h.read(500000)
def main():
 for n,(digest,mode) in PINS.items():
  if hashlib.sha256(read(ROOT/n,mode)).hexdigest()!=digest:raise ValueError()
 secret=read(Path('/Users/jay/.config/wise/simple-tools-bearer'),0o600).decode().strip()
 env={'HOME':'/Users/jay','PATH':'/usr/bin:/bin:/opt/homebrew/bin','LANG':'C.UTF-8','AGENT_SEAT':'WISE','SIMPLE_GIT_REMOTE':'1','SIMPLE_TOOLS_BEARER':secret,'PYTHONDONTWRITEBYTECODE':'1'}
 def call(argv):
  p=subprocess.run([str(ROOT/'scripts/simple-git'),*argv],env=env,capture_output=True,timeout=20)
  if max(len(p.stdout),len(p.stderr))>65536 or secret.encode() in p.stdout or secret.encode() in p.stderr:raise ValueError()
  return p
 p=call(['whoami','--json'])
 if p.returncode:raise ValueError()
 who=json.loads(p.stdout)['structured']
 if who.get('authenticated_seat')!='WISE' or who.get('default_identity')!='WISE' or who.get('transport')!='hosted':raise ValueError()
 repo='Simple-With-Us/AI-Fleet-Coordinator'
 p=call(['call','gr_wf_repo_get','--args-json',json.dumps({'repo':repo,'identity':'WISE'})])
 if p.returncode:raise ValueError()
 info=json.loads(p.stdout).get('repository',{})
 if info.get('full_name')!=repo or info.get('default_branch')!='main':raise ValueError()
 # Null expected-head AND base-object IDs are unsatisfiable provider write
 # preconditions independently of the expected early default-branch refusal.
 args={'repo':repo,'branch':'main','path':'README.md','message':'Bounded own-WISE runtime guard proof','expected_head_sha':'0'*40,'base_blob_sha':'0'*40,'edits':[{'start':0,'end':0,'text':''}],'identity':'WISE'}
 p=call(['call','gr_wf_file_patch','--args-json',json.dumps(args)])
 expected=b'simple-git: gr_wf_file_patch failed on the hosted server: relay_refused (reason=bad_request_default_branch_protected)\n'
 if p.returncode!=1 or p.stdout or p.stderr!=expected:raise ValueError()
 return {'ok':True,'seat':'WISE','transport':'hosted','repo_read':True,'null_guard':'default_branch_protected'}
try:result=main()
except BaseException:result={'ok':False}
print(json.dumps(result,sort_keys=True))
"""

EXPECTED = {
    "ok": True,
    "seat": "WISE",
    "transport": "hosted",
    "repo_read": True,
    "null_guard": "default_branch_protected",
}


def prove(hashes, run=subprocess.run):
    """No dynamic command, seat, URL, path, credential or packet argument."""
    if hashes.get("code/patch_secretscan.py") not in {BEFORE, AFTER}:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="wise-proof-hostkey-") as directory:
            Path(directory).chmod(0o755)  # Public host key must be readable by runuser jay.
            path = Path(directory) / "known_hosts"
            path.write_text(MAC_HOST + " " + MAC_HOST_KEY + "\n")
            path.chmod(0o644)
            result = run(
                [
                    "/usr/sbin/runuser",
                    "-u",
                    "jay",
                    "--",
                    "/usr/bin/ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "UserKnownHostsFile=" + str(path),
                    "-o",
                    "ConnectTimeout=5",
                    "jay@" + MAC_HOST,
                    "python3 -",
                ],
                input=REMOTE_SOURCE.encode(),
                capture_output=True,
                timeout=35,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        return (
            result.returncode == 0
            and not result.stderr
            and len(result.stdout) < 512
            and json.loads(result.stdout) == EXPECTED
        )
    except Exception:
        return False


def make_proof():
    return lambda identity, hashes: prove(hashes)
