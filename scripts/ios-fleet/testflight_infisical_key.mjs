// Dedicated service custody adapter. Never reads a PEM file or inherited token.
import {execFile} from 'node:child_process';
import {createPrivateKey,createHash} from 'node:crypto';
// Fleet-OPS project inventory is private. Pin its reviewed digest, as the repository
// project-ID checker does; read the identifier only from scoped token metadata.
export const PROJECT_SHA256='98523bf1fe1594f019e654a36f5fec6331bc9863270199b39c4c0977198e83c2';
export const SECRET_PATH='/testflight-github';
export const SECRET_NAME='GH_APP_SWU_TESTFLIGHT_PRIVATE_KEY';
export const ACCOUNT='ios-testflight-github-ro';
const BASE='https://app.infisical.com/api';
const projectDigest=(value)=>createHash('sha256').update(value).digest('hex');
const refused=()=>new Error('dedicated-testflight-isolated-binding-unavailable');
function keychain(execImpl) {
  return new Promise((resolve,reject)=>{
    execImpl('/usr/bin/security',['find-generic-password','-s','infisical-service-token','-a',ACCOUNT,'-w'],{encoding:'utf8',timeout:15000,maxBuffer:16384},(err,out)=>{
      if(err)return reject(refused());
      const token=String(out||'').trim();
      if(!/^st\.[^\s\0]{18,8190}$/.test(token))return reject(refused());
      resolve(token);
    });
  });
}
export async function resolveIsolatedTestflightKey({execImpl=execFile,fetchImpl=fetch,now=Date.now}={}) {
  try {
    const deadline=now()+60000;
    const token=await keychain(execImpl);
    async function get(path,max) {
      const remaining=Math.min(20000,deadline-now());if(remaining<=0)throw refused();
      const r=await fetchImpl(BASE+path,{method:'GET',redirect:'error',signal:AbortSignal.timeout(Math.floor(remaining)),headers:{Authorization:`Bearer ${token}`,Accept:'application/json'}});
      if(!r.ok)throw refused();
      const length=Number(r.headers?.get?.('content-length'));if(length>max)throw refused();
      // Bound streaming response before JSON parse, not only after allocation.
      const chunks=[];let size=0;
      if(!r.body?.getReader)throw refused();
      const reader=r.body.getReader();
      try { while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>max)throw refused();chunks.push(value);} }
      finally {await reader.cancel().catch(()=>{});}
      return JSON.parse(Buffer.concat(chunks).toString('utf8'));
    }
    const m=await get('/v2/service-token',131072);
    const project=m.workspace;
    if(typeof project!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(project)||projectDigest(project)!==PROJECT_SHA256)throw refused();
    if(m.workspace!==project || ('projectId'in m&&m.projectId!==project) || !Array.isArray(m.permissions)||m.permissions.length!==1||m.permissions[0]!=='read'||!Array.isArray(m.scopes)||m.scopes.length!==1)throw refused();
    const scope=m.scopes[0];if(!scope||Object.keys(scope).sort().join(',')!=='environment,secretPath'||scope.environment!=='prod'||scope.secretPath!==SECRET_PATH)throw refused();
    // Initial issuance is finite. Registered non-expiring support is not inferred
    // from older reader approval, and requires a future explicit binding record.
    if(typeof m.expiresAt!=='string'||!/(?:Z|[+-]\d{2}:\d{2})$/.test(m.expiresAt)||!Number.isFinite(Date.parse(m.expiresAt))||Date.parse(m.expiresAt)<=now())throw refused();
    const q=new URLSearchParams({workspaceId:project,environment:'prod',secretPath:SECRET_PATH,viewSecretValue:'true',expandSecretReferences:'false',recursive:'false',include_imports:'false'});
    const payload=await get(`/v3/secrets/raw/${SECRET_NAME}?${q}`,65536);
    const s=payload.secret;
    if(!s||s.secretKey!==SECRET_NAME||s.secretPath!==SECRET_PATH||s.workspace!==project||s.environment!=='prod'||s.type!=='shared'||s.secretValueHidden!==false||typeof s.secretValue!=='string'||!s.secretValue||/\$\{[^}]+\}/.test(s.secretValue))throw refused();
    const k=createPrivateKey(s.secretValue);if(k.asymmetricKeyType!=='rsa')throw refused();
    return k;
  } catch {throw refused();}
}
