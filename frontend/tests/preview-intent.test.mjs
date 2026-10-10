import test,{beforeEach} from 'node:test'
import assert from 'node:assert/strict'
import {preparePreviewIntent,recordPreviewProposal,terminalIntentHashes,sha256} from '../src/lib/preview-intent.ts'
let values
beforeEach(()=>{values=new Map();globalThis.sessionStorage={getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)}})
test('unresolved identity survives caller recreation and stays scoped to account, plan, endpoint and body',async()=>{
 const first=await preparePreviewIntent('actor-a','undo','plan-a',{targetRevision:3});assert.deepEqual(await preparePreviewIntent('actor-a','undo','plan-a',{targetRevision:3}),first)
 for(const [actor,kind,plan,body] of [['actor-b','undo','plan-a',{targetRevision:3}],['actor-a','undo','plan-b',{targetRevision:3}],['actor-a','rebuild','plan-a',{targetRevision:3}],['actor-a','undo','plan-a',{targetRevision:2}]])assert.notEqual((await preparePreviewIntent(actor,kind,plan,body)).key,first.key)
 assert.ok([...values.values()].every(value=>!value.includes('targetRevision')))
})
test('terminal binding requires exact owner, endpoint kind, known plan and nonnull request hash',async()=>{
 const first=await preparePreviewIntent('actor-a','undo','plan-a',{targetRevision:3}),hash=await sha256(first.key),row={ownerId:'actor-a',requestType:'future_undo',requestPlanId:'plan-a',clientRequestKeyHash:hash,status:'applied',expires_at:'2030-01-01T00:00:00Z'}
 for(const changed of [{ownerId:'actor-b'},{requestType:'program_rebuild'},{requestPlanId:'plan-b'},{clientRequestKeyHash:null},{clientRequestKeyHash:await sha256('different-request')},{status:'unknown'}]){const terminal=terminalIntentHashes([{...row,...changed}],'actor-a','future_undo','plan-a');assert.equal((await preparePreviewIntent('actor-a','undo','plan-a',{targetRevision:3},terminal)).key,first.key)}
 const terminal=terminalIntentHashes([row],'actor-a','future_undo','plan-a');assert.notEqual((await preparePreviewIntent('actor-a','undo','plan-a',{targetRevision:3},terminal)).key,first.key)
})
test('applied dismissed superseded and expired bound proposals end their own exact intent',async()=>{
 for(const status of ['applied','dismissed','superseded','expired','awaiting_approval']){const first=await preparePreviewIntent('actor-a','initial','new',{status}),row={ownerId:'actor-a',requestType:'initial_program',requestPlanId:'created-plan',clientRequestKeyHash:await sha256(first.key),status,expires_at:status==='awaiting_approval'?'2000-01-01T00:00:00Z':'2030-01-01T00:00:00Z'};assert.notEqual((await preparePreviewIntent('actor-a','initial','new',{status},terminalIntentHashes([row],'actor-a','initial_program'))).key,first.key)}
})
test('received awaiting proposal retains request and exact proposal identity',async()=>{const first=await preparePreviewIntent('actor-a','rebuild','plan-a',{coverage:'unknown'});recordPreviewProposal(first,'draft-a');assert.equal((await preparePreviewIntent('actor-a','rebuild','plan-a',{coverage:'unknown'})).key,first.key);assert.equal(JSON.parse(values.get(first.slot)).proposalId,'draft-a')})
test('unavailable persistent session identity fails before a preview can be sent',async()=>{globalThis.sessionStorage.setItem=()=>{throw Error('Unavailable')};await assert.rejects(preparePreviewIntent('actor-a','initial','new',{}),/preserve a safe preview retry identity/)})
