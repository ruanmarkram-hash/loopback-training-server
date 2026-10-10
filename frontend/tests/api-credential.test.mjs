import test from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import ts from 'typescript'
const compile=source=>ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
const dataUrl=source=>'data:text/javascript;base64,'+Buffer.from(source).toString('base64')
async function loadApi(){const values=new Map([['loopback.token','synthetic-credential-a'],['loopback.tokenId','synthetic-id-a'],['loopback.user',JSON.stringify({id:'synthetic-actor-a'})]]);globalThis.localStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};const detail=dataUrl(compile(readFileSync('src/lib/api-errors.ts','utf8'))),source=compile(readFileSync('src/lib/api.ts','utf8')).replace("'./api-errors'",JSON.stringify(detail));return {api:await import(dataUrl(source)+'#'+crypto.randomUUID()),values}}
for(const adopted of [false,true])test(`body parsing cannot deliver old-principal data after credential change; adopted ${adopted}`,async()=>{
 const {api,values}=await loadApi();let changed=0,unauthorized=0;api.setSessionChangedHandler(()=>changed++);api.setUnauthorizedHandler(()=>unauthorized++);let release,entered;const pending=new Promise(resolve=>release=resolve),parsing=new Promise(resolve=>entered=resolve);globalThis.fetch=async()=>({status:200,ok:true,json:async()=>{entered();await pending;return {owner:'synthetic-actor-a'}}});const result=api.api.get('/synthetic');await parsing;values.set('loopback.token','synthetic-credential-b');values.set('loopback.tokenId','synthetic-id-b');values.set('loopback.user',JSON.stringify({id:'synthetic-actor-b'}));if(adopted)api.bindAuthCredential();release();await assert.rejects(result,error=>error instanceof api.ApiError&&error.status===(adopted?409:401));assert.equal(unauthorized,0);assert.equal(changed,adopted?0:1)
})
for(const auth of [true,false])test(`initiating command cannot dispatch under a later adopted principal; authenticated ${auth}`,async()=>{
 const {api,values}=await loadApi();const command=api.captureAuthCommand();let requests=0;globalThis.fetch=async()=>{requests++;return {status:200,ok:true,json:async()=>({})}};values.set('loopback.token','synthetic-credential-b');api.bindAuthCredential();await assert.rejects(api.api.post('/synthetic',{fact:'actor-a'},{auth,command}),error=>error.status===409);assert.equal(requests,0);assert.equal(command.isCurrent(),false)
})
test('pending public login response cannot replace newly adopted session',async()=>{
 const {api,values}=await loadApi();const command=api.captureAuthCommand();let release,entered;const pending=new Promise(resolve=>release=resolve),fetching=new Promise(resolve=>entered=resolve);globalThis.fetch=async()=>{entered();await pending;return {status:200,ok:true,json:async()=>({owner:'synthetic-actor-a'})}};const result=api.api.post('/synthetic-login',{}, {auth:false,command});await fetching;values.set('loopback.token','synthetic-credential-b');api.bindAuthCredential();release();await assert.rejects(result,error=>error.status===409);assert.equal(values.get('loopback.token'),'synthetic-credential-b')
})
