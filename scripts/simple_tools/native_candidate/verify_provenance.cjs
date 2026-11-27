'use strict';
const fs=require('fs'),path=require('path'),os=require('os'),crypto=require('crypto'),cp=require('child_process');
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
for(const [manifest,file]of [['schema-provenance.json','native_source_schemas.cjs']]){const m=require('./'+manifest);if(hash(fs.readFileSync(path.join(__dirname,file)))!==m.generated_sha256)throw Error('generated-hash:'+file)}
const scope=require('./scope-provenance.json');for(const [file,m]of Object.entries(scope.outputs))if(hash(fs.readFileSync(path.join(__dirname,file)))!==m.generated_sha256)throw Error('generated-hash:'+file);
if(process.argv[2]){
 const source=fs.readFileSync(process.argv[2],'utf8'),m=require('./schema-provenance.json');if(hash(source)!==m.source_sha256)throw Error('source-hash');
 for(const d of [...m.declarations,...Object.values(scope.outputs).flatMap(x=>x.declarations)])if(hash(source.slice(d.start,d.end))!==d.sha256)throw Error('source-slice:'+d.name);
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'native-provenance-'));
 try{for(const script of ['extract_source_schemas.cjs','extract_scope.cjs'])cp.execFileSync(process.execPath,[path.join(__dirname,script),path.resolve(process.argv[2]),dir]);for(const file of ['native_source_schemas.cjs',...Object.keys(scope.outputs)])if(!fs.readFileSync(path.join(dir,file)).equals(fs.readFileSync(path.join(__dirname,file))))throw Error('regeneration:'+file)}finally{fs.rmSync(dir,{recursive:true,force:true})}
 console.log('PASS source hash, every declaration slice and portable byte-identical regeneration');
}else console.log('PASS generated-file hashes only. Supply pinned source path for independent slice/regeneration verification.');
