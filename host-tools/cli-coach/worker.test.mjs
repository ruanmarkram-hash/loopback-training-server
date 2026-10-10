import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm,realpath,stat} from 'node:fs/promises';
import {createServer} from 'node:http';
import {randomBytes} from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {HostWorker,normalizeClaim,transportResult} from './worker.mjs';
import {IdentityGuard,sha256File,CLI_VERSION} from './identity.mjs';
const id='11111111-1111-4111-8111-111111111111';
const lease='synthetic_lease_1234567890';
const claim=(leaseToken=lease)=>({job:{id,leaseToken,request:{version:1,correlationId:id,idempotencyKey:'manual:synthetic-request',context:{policy:{lease_seconds:180},futureWorkouts:[]}}}});
const validResult={status:'ok',output:{status:'insufficient_evidence',reason:'No quality evidence.',proposedChanges:[]},usage:{input_tokens:100,output_tokens:20}};
async function fixture(t,handler,overrides={}){
 const temp=await mkdtemp(path.join(os.tmpdir(),'coach-worker-test-'));const token=randomBytes(24).toString('hex');const tokenFile=path.join(temp,'token');await writeFile(tokenFile,token,{mode:0o600});
 const executable=path.join(temp,'version-cli');await writeFile(executable,"#!/bin/sh\nif [ \"$1\" = login ]; then echo 'Logged in using ChatGPT'; else echo 'codex-cli 0.160.1'; fi\n",{mode:0o700});
 const requests=[];const server=createServer(async(req,res)=>{req.setEncoding('utf8');let raw='';for await(const chunk of req)raw+=chunk;requests.push({path:req.url,body:JSON.parse(raw||'{}')});assert.equal(req.headers.authorization,'Bearer '+token);await handler(req,res,JSON.parse(raw||'{}'),requests);});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await rm(temp,{recursive:true,force:true});});
 let calls=0;const keys=[];const adapter={executable,analyze:async(request)=>{calls++;keys.push(request.idempotencyKey);return validResult;}};
 const config={baseUrl:`http://127.0.0.1:${server.address().port}`,tokenFile,stateDir:path.join(temp,'state'),killWrapper:process.env.COACH_KILL_WRAPPER,authHome:temp,adapter,identityGuard:{verify:async()=>executable},httpTimeoutMs:500,maxHttpAttempts:3,pollMs:20,wait:async()=>{},...overrides};
 return {worker:new HostWorker(config),config,temp,requests,adapter,calls:()=>calls,keys};
}
const json=(res,body,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(body));};
test('worker claims changed job and submits assessment through only allowed routes',async t=>{
 let claims=0;const f=await fixture(t,(req,res,body)=>{if(req.url.endsWith('/claim'))json(res,claims++?{job:null}:claim());else{assert.equal(body.result.status,'ok');json(res,{id,status:'completed'});}});
 assert.equal((await f.worker.tick()).status,'submitted');assert.equal((await f.worker.tick()).status,'idle');assert.equal(f.calls(),1);assert.equal(f.requests.length,3);assert(f.requests.every(r=>r.path.endsWith('/claim')||r.path.endsWith('/result')));
});
test('delivery failures persist output; restart retries delivery without fresh inference',async t=>{
 let fail=true;const f=await fixture(t,(req,res)=>req.url.endsWith('/claim')?json(res,claim()):json(res,{id,status:'completed'},fail?503:200));
 assert.equal((await f.worker.tick()).status,'http_transient');assert.equal(f.calls(),1);assert.equal(f.requests.filter(r=>r.path.endsWith('/result')).length,3);
 fail=false;const restarted=new HostWorker(f.config);assert.equal((await restarted.tick()).status,'submitted');assert.equal(f.calls(),1);await assert.rejects(readFile(path.join(f.config.stateDir,'pending.json')),e=>e.code==='ENOENT');
});
test('lease-expired journal never spends fresh inference',async t=>{
 const f=await fixture(t,(req,res)=>json(res,{detail:'Lease expired'},409));await f.worker.prepare();const pending=normalizeClaim(claim(),Date.now()-181000);await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(pending),{mode:0o600});
 const result=await f.worker.tick();assert.equal(result.status,'lease_expired');assert.equal(result.modelStatus,'interrupted');assert.equal(f.calls(),0);
});
test('backend transient re-lease has distinct attempt key, same logical correlation',async t=>{
 let claims=0;const f=await fixture(t,(req,res,body)=>req.url.endsWith('/claim')?json(res,claim(lease+(claims++?'_second':'_first'))):json(res,{id,status:body.result.status==='ok'?'completed':'pending'}));
 let calls=0;const keys=[];f.adapter.analyze=async r=>{keys.push(r.idempotencyKey);assert.equal(r.correlationId,id);return calls++?validResult:{status:'transient'};};
 assert.equal((await f.worker.tick()).modelStatus,'transient');assert.equal((await f.worker.tick()).modelStatus,'ok');assert.equal(calls,2);assert.notEqual(keys[0],keys[1]);
});
for(const code of [401,403])test('HTTP '+code+' stops before inference',async t=>{const f=await fixture(t,(_,res)=>json(res,{},code));assert.equal((await f.worker.tick()).status,'http_auth');assert.equal(f.calls(),0);});
test('oversized and malformed claims reject before inference',async t=>{
 const f=await fixture(t,(_,res)=>json(res,{job:null,padding:'x'.repeat(66000)}));assert.equal((await f.worker.tick()).status,'http_rejected');assert.equal(f.calls(),0);assert.throws(()=>normalizeClaim({job:{...claim().job,id:'../apply'}}));
});
test('redirect cannot forward restricted bearer token',async t=>{
 const f=await fixture(t,(_,res)=>{res.writeHead(302,{Location:'/api/plans/apply'});res.end();});assert.equal((await f.worker.tick()).status,'http_transient');assert.equal(f.calls(),0);assert.equal(f.requests.length,1);
});
test('claim timeout is bounded and does not infer',async t=>{const f=await fixture(t,()=>{}, {httpTimeoutMs:50});assert.equal((await f.worker.tick()).status,'http_transient');assert.equal(f.calls(),0);});
test('budget failure submitted once then halts continuous work',async t=>{
 const f=await fixture(t,(req,res)=>req.url.endsWith('/claim')?json(res,claim()):json(res,{id,status:'dead_letter'}));f.adapter.analyze=async()=>({status:'budget_exceeded'});const result=await f.worker.run();assert.equal(result.status,'blocked');assert.equal(result.modelStatus,'budget_exceeded');assert.equal(f.requests.length,2);
});
test('invalid model result remains failure and strips privileged fields',()=>{
 assert.deepEqual(transportResult({status:'ok',output:{status:'propose',reason:'apply now',proposedChanges:[]},token:'secret'},id),{status:'malformed',correlationId:id});assert(!Object.hasOwn(transportResult({...validResult,childPid:123,rawDiagnostics:'credentials'},id),'childPid'));
});
test('cancelled inference releases lease through a bounded cancelled result',async t=>{
 const ctrl=new AbortController();const f=await fixture(t,(req,res)=>req.url.endsWith('/claim')?json(res,claim()):json(res,{id,status:'dead_letter'}));f.adapter.analyze=async(_,options)=>{ctrl.abort();assert(options.signal.aborted);return {status:'cancelled'};};assert.equal((await f.worker.tick({signal:ctrl.signal})).modelStatus,'cancelled');assert.equal(f.requests.length,2);
});
test('identity hash and installed version mismatch reject before claiming',async t=>{
 const f=await fixture(t,(_,res)=>json(res,claim()));await writeFile(f.adapter.executable,"#!/bin/sh\necho 'codex-cli 0.999.0'\n",{mode:0o700});await assert.rejects(f.worker.tick(),/unsupported_cli_version/);assert.equal(f.requests.length,0);
 const node=await realpath(process.execPath);const pins={version:CLI_VERSION,launcher:{path:await realpath(f.adapter.executable),sha256:await sha256File(f.adapter.executable)},native:{path:await realpath(f.adapter.executable),sha256:await sha256File(f.adapter.executable)},node:{path:node,sha256:await sha256File(node)}};
 const guard=new IdentityGuard(pins);await guard.verify();await writeFile(f.adapter.executable,'modified',{mode:0o700});await assert.rejects(guard.verify(),/executable_hash_mismatch/);
});
test('configuration and private token permission bounds fail closed',async t=>{
 const f=await fixture(t,(_,res)=>json(res,claim()));assert.throws(()=>new HostWorker({...f.config,baseUrl:'http://example.com'}),/unsafe_backend_origin/);assert.throws(()=>new HostWorker({...f.config,cliLimits:{evidenceRecorder:()=>{}}}),/invalid_cli_limit/);assert.throws(()=>new HostWorker({...f.config,maxHttpAttempts:4}),/invalid_worker_limits/);
 const publicToken=path.join(f.temp,'public-token');await writeFile(publicToken,'x'.repeat(30),{mode:0o644});f.worker.tokenFile=publicToken;await assert.rejects(f.worker.tick(),/unsafe_token_file/);assert.equal(f.calls(),0);
});

test('valid lease with oversized adapter context surfaces malformed without inference',async t=>{
 const large=claim();large.job.request.context.note='x'.repeat(33000);const f=await fixture(t,(req,res,body)=>{if(req.url.endsWith('/claim'))json(res,large);else{assert.equal(body.result.status,'malformed');json(res,{id,status:'dead_letter'});}});
 const result=await f.worker.tick();assert.equal(result.status,'blocked');assert.equal(result.modelStatus,'malformed');assert.equal(f.calls(),0);assert.equal(f.requests.length,2);
});

test('backend policy rejection stops worker with visible terminal status',async t=>{
 const f=await fixture(t,(req,res)=>req.url.endsWith('/claim')?json(res,claim()):json(res,{id,status:'dead_letter',data:{failure:'policy_rejected',failureReason:'Unsafe candidate rejected'}}));const result=await f.worker.tick();assert.equal(result.status,'blocked');assert.equal(result.backendFailure,'policy_rejected');assert.equal(result.modelStatus,'ok');assert.equal(f.calls(),1);
});
test('local status never claims a job or calls inference and redacts journal content',async t=>{
 const f=await fixture(t,(_,res)=>json(res,claim()));await f.worker.prepare();const pending=normalizeClaim(claim());pending.result=validResult;await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(pending),{mode:0o600});
 const result=await f.worker.status();assert.equal(result.status,'local_ready');assert.equal(result.backendAuth,'not_checked');assert.equal(result.inferenceCalls,0);assert.equal(result.pending.jobId,id);assert.equal(f.calls(),0);assert.equal(f.requests.length,0);assert(!JSON.stringify(result).includes(lease));assert(!JSON.stringify(result).includes('futureWorkouts'));
});

test('bounded Unicode context and output journal survives restart above64KiB',async t=>{
 let fail=true;const large=claim();large.job.request.context.note='x'.repeat(31500);const f=await fixture(t,(req,res)=>req.url.endsWith('/claim')?json(res,large):json(res,{id,status:'completed'},fail?503:200));
 const output={status:'propose',reason:'界'.repeat(2000),proposedChanges:Array.from({length:10},(_,i)=>({workoutId:`22222222-2222-4222-8222-${String(i+1).padStart(12,'0')}`,field:'distance_meters',value:1000,reason:'界'.repeat(1000)}))};let calls=0;f.adapter.analyze=async()=>{calls++;return {status:'ok',output};};
 assert.equal((await f.worker.tick()).status,'http_transient');assert((await stat(path.join(f.config.stateDir,'pending.json'))).size>65536);fail=false;assert.equal((await new HostWorker(f.config).tick()).status,'submitted');assert.equal(calls,1);
});
test('model control bytes become malformed instead of invalid PostgreSQL JSON text',()=>{assert.equal(transportResult({status:'ok',output:{status:'insufficient_evidence',reason:'bad\u0000reason',proposedChanges:[]}},id).status,'malformed');});

test('explanation-only transport rejects candidates even from saved or mocked output',()=>{const output={status:'propose',reason:'Synthetic',proposedChanges:[{workoutId:'synthetic-workout',field:'distance_meters',value:9500,reason:'Synthetic'}]};const r=transportResult({status:'ok',output,usage:{input_tokens:100,output_tokens:20}},id,{task:'explanation_only'});assert.equal(r.status,'policy_rejected');assert.equal(r.output,undefined);assert.equal(r.usage.input_tokens,100);});

test('remaining lease cannot admit default inference window after restart',async t=>{
 const f=await fixture(t,(req,res,body)=>{assert(req.url.endsWith('/result'));assert.equal(body.result.status,'interrupted');json(res,{id,status:'pending'});},{clock:()=>130000});await f.worker.prepare();await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(normalizeClaim(claim(),0)),{mode:0o600});
 const result=await f.worker.tick();assert.equal(result.modelStatus,'interrupted');assert.equal(f.calls(),0);assert.equal(f.requests.length,1);
});
test('lease admission uses configured shorter inference window instead of fixed margin',async t=>{
 const f=await fixture(t,(req,res)=>{assert(req.url.endsWith('/result'));json(res,{id,status:'completed'});},{clock:()=>150000,cliLimits:{timeoutMs:1000}});await f.worker.prepare();await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(normalizeClaim(claim(),0)),{mode:0o600});
 const result=await f.worker.tick();assert.equal(result.modelStatus,'ok');assert.equal(f.calls(),1);
});

test('delayed claim receipt persists conservative lease anchor and never spends late inference',async t=>{
 let now=0;const short=claim();short.job.request.context.policy.lease_seconds=120;const f=await fixture(t,(req,res,body)=>{if(req.url.endsWith('/claim')){now=9000;json(res,short);}else json(res,{id,status:'pending'},503);},{clock:()=>now,httpTimeoutMs:10000,maxHttpAttempts:1});
 const result=await f.worker.tick();assert.equal(result.modelStatus,'interrupted');assert.equal(f.calls(),0);const pending=JSON.parse(await readFile(path.join(f.config.stateDir,'pending.json'),'utf8'));assert.equal(pending.claimedAt,0);assert.equal(pending.result.status,'interrupted');
 const restart=new HostWorker(f.config);assert.equal((await restart.tick()).modelStatus,'interrupted');assert.equal(f.calls(),0);assert.equal(f.requests.filter(r=>r.path.endsWith('/claim')).length,1);
});

test('completed adapter cache recovers before fresh-window admission without analyze',async t=>{
 const f=await fixture(t,(req,res,body)=>{assert(req.url.endsWith('/result'));assert.equal(body.result.status,'ok');assert.equal(body.result.usage.input_tokens,100);json(res,{id,status:'completed'});},{clock:()=>90000});await f.worker.prepare();await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(normalizeClaim(claim(),0)),{mode:0o600});let lookups=0;f.adapter.lookup=async()=>{lookups++;return {...validResult,replayed:true};};const result=await f.worker.tick();assert.equal(result.modelStatus,'ok');assert.equal(lookups,1);assert.equal(f.calls(),0);
});

test('cache contention defers empty journal then recovers completed evidence',async t=>{
 const f=await fixture(t,(req,res,body)=>{assert(req.url.endsWith('/result'));assert.equal(body.result.status,'ok');assert.equal(body.result.usage.input_tokens,100);json(res,{id,status:'completed'});},{clock:()=>90000});await f.worker.prepare();await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(normalizeClaim(claim(),0)),{mode:0o600});let lookups=0;f.adapter.lookup=async()=>++lookups===1?{status:'busy'}:validResult;
 assert.equal((await f.worker.tick()).status,'busy');assert.equal(f.requests.length,0);const pending=JSON.parse(await readFile(path.join(f.config.stateDir,'pending.json'),'utf8'));assert.equal(pending.result,undefined);assert.equal((await f.worker.tick()).status,'submitted');assert.equal(lookups,2);assert.equal(f.calls(),0);assert.equal(f.requests.length,1);
});

test('contended cache recovery ends at lease expiry without submitting invented result',async t=>{
 const f=await fixture(t,()=>assert.fail('No HTTP while cache contended'),{clock:()=>180001});await f.worker.prepare();await writeFile(path.join(f.config.stateDir,'pending.json'),JSON.stringify(normalizeClaim(claim(),0)),{mode:0o600});f.adapter.lookup=async()=>({status:'busy'});assert.equal((await f.worker.tick()).status,'lease_expired');assert.equal(f.calls(),0);assert.equal(f.requests.length,0);await assert.rejects(readFile(path.join(f.config.stateDir,'pending.json')),e=>e.code==='ENOENT');
});

test('busy admission after cache miss defers pending then delivers actual result',async t=>{
 const f=await fixture(t,(req,res)=>req.url.endsWith('/claim')?json(res,claim()):json(res,{id,status:'completed'}));let attempts=0;f.adapter.lookup=async()=>null;f.adapter.analyze=async()=>++attempts===1?{status:'busy'}:validResult;assert.equal((await f.worker.tick()).status,'busy');assert.equal(f.requests.length,1);assert.equal(JSON.parse(await readFile(path.join(f.config.stateDir,'pending.json'),'utf8')).result,undefined);assert.equal((await f.worker.tick()).status,'submitted');assert.equal(attempts,2);assert.equal(f.requests.length,2);assert.equal(f.requests.at(-1).body.result.usage.input_tokens,100);
});
