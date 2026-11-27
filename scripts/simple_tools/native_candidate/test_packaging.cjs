'use strict';
const fs=require('fs'),path=require('path'),os=require('os'),cp=require('child_process'),assert=require('assert');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'native-package-'));
try{
 fs.copyFileSync(path.join(__dirname,'native_flow.cjs'),path.join(dir,'native_flow.cjs'));
 let r=cp.spawnSync(process.execPath,['-e',`require(${JSON.stringify(path.join(dir,'native_flow.cjs'))})`],{encoding:'utf8'});assert.notEqual(r.status,0);assert(r.stderr.includes('schema-provenance.json'));
 fs.writeFileSync(path.join(dir,'schema-provenance.json'),JSON.stringify({source_sha256:'wrong',generated_sha256:'wrong'}));fs.writeFileSync(path.join(dir,'native_source_schemas.cjs'),'module.exports={}');
 r=cp.spawnSync(process.execPath,['-e',`require(${JSON.stringify(path.join(dir,'native_flow.cjs'))})`],{encoding:'utf8'});assert.notEqual(r.status,0);assert(r.stderr.includes('candidate-schema-provenance'));
 console.log('PASS missing/mismatched local generated schema fails before dependencies, transport or effects');
}finally{fs.rmSync(dir,{recursive:true,force:true})}
