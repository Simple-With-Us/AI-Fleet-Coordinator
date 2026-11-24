"""Read-only group/ACL custody for candidate runtime paths. Never reads file contents."""
import grp,json,os,pwd,stat,subprocess,sys

def status_holds_foreign_group(data,gid,uid):
    fields={}
    for line in data.splitlines():
        if ':'in line:
            key,value=line.split(':',1)
            if key in ('Uid','Gid','Groups'):
                if key in fields:raise ValueError('custody-status-duplicate')
                values=value.split()
                if any(not v.isdecimal()for v in values):raise ValueError('custody-status-invalid')
                fields[key]=[int(v)for v in values]
    if set(fields)!={'Uid','Gid','Groups'}or len(fields['Uid'])!=4 or len(fields['Gid'])!=4:raise ValueError('custody-status-incomplete')
    return gid in fields['Gid']+fields['Groups'] and any(v not in (0,uid)for v in fields['Uid'])

def local_nss_complete(text):
    found={}
    for line in text.splitlines():
        line=line.split('#',1)[0]
        if ':'not in line:continue
        key,value=line.split(':',1)
        if key in ('passwd','group'):
            if key in found:raise ValueError('custody-nss-duplicate')
            found[key]=value.split()
    if set(found)!={'passwd','group'}or any(v not in (['files','systemd'],['files'])for v in found.values()):raise ValueError('custody-nss-unqualified')
    # Qualified host uses local passwd/group plus systemd dynamic identities.
    # systemd dynamic UID/GIDs are allocated separately, but any retained writable
    # GID is still independently checked in all observed process credential fields.
    return True

def validate_paths(paths,uid,*,lstat=os.lstat,accounts=pwd.getpwall,group=grp.getgrgid,groups=os.getgrouplist,acl=None,holders=None,nss=None):
    if type(uid)is not int or uid<=0 or not paths or len(paths)>64:raise ValueError('custody-input')
    if acl is None:
        def acl(path):
            r=subprocess.run(['/usr/bin/getfacl','-cp','--absolute-names','--',path],capture_output=True,text=True,timeout=3,check=True)
            if r.stderr.strip():raise ValueError('custody-acl-warning')
            return r.stdout
    if holders is None:
        def holders(gid):
            values=[]
            for name in os.listdir('/proc'):
                if not name.isdecimal():continue
                try:
                    data=open('/proc/'+name+'/status').read()
                    if status_holds_foreign_group(data,gid,uid):values.append(name)
                except FileNotFoundError:continue
                except ProcessLookupError:continue
                # A process that cannot be inspected is not proof of absence.
                except (PermissionError,IndexError,ValueError):raise ValueError('custody-process-unverifiable')
            return values
    if nss is None:nss=open('/etc/nsswitch.conf').read()
    local_nss_complete(nss)
    records=[];verified=set()
    for path in paths:
        st=lstat(path)
        if stat.S_ISLNK(st.st_mode)or st.st_uid not in (0,uid)or st.st_mode&0o002:raise ValueError('custody-owner-or-other-write')
        # Reject all extended/default ACL entries, including read-only additions.
        text=acl(path)
        entries=[line.strip()for line in text.splitlines()if line.strip()and not line.startswith('#')]
        if len(entries)!=3 or sorted(line.split('::',1)[0] for line in entries)!=['group','other','user'] or any('::'not in line or len(line.split('::',1)[1])!=3 or any(c not in 'rwx-'for c in line.split('::',1)[1])for line in entries):raise ValueError('custody-extended-acl')
        if st.st_mode&0o020:
            if st.st_uid!=uid:raise ValueError('custody-root-group-write')
            if st.st_gid not in verified:
                users=accounts();byname={u.pw_name:u for u in users};g=group(st.st_gid)
                if any(name not in byname or byname[name].pw_uid!=uid for name in g.gr_mem):raise ValueError('custody-group-supplementary')
                for user in users:
                    if st.st_gid in groups(user.pw_name,user.pw_gid)and user.pw_uid!=uid:raise ValueError('custody-group-member')
                if holders(st.st_gid):raise ValueError('custody-group-process')
                verified.add(st.st_gid)
            records.append({'path':path,'private_owner_group':st.st_gid})
    return {'ok':True,'private_owner_group_paths':records}

if __name__=='__main__':
    try:
        values=json.load(sys.stdin)
        if set(values)!={'paths','uid'}:raise ValueError('custody-input')
        print(json.dumps(validate_paths(values['paths'],values['uid'])))
    except Exception:
        print(json.dumps({'ok':False,'reason':'path-custody-unverifiable'}));sys.exit(1)
