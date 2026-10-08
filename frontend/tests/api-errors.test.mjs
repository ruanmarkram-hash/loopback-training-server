import test from 'node:test'
import assert from 'node:assert/strict'
import {describeApiDetail} from '../src/lib/api-errors.ts'
test('field validation shows explanation but excludes input and exception context',()=>{
 const rendered=describeApiDetail([{loc:['body','benchmark','confidence'],msg:'Input should be less than or equal to 1',input:'synthetic-private-input',ctx:{error:'synthetic-private-context'}}],'Fallback')
 assert.equal(rendered,'benchmark · confidence: Input should be less than or equal to 1');assert.ok(!rendered.includes('private'))
})
test('onboarding and plan guardrail responses remain actionable',()=>{
 assert.equal(describeApiDetail({status:'onboarding_required',required_fields:['weekly_distance_meters','source']},'Fallback'),'Current ability facts are required before a program can be prepared. Required: weekly distance meters, source.')
 assert.equal(describeApiDetail({reason:'Plan guardrail breach',validation:[{severity:'critical',message:'Weekly distance exceeds the agreed limit.'}]},'Fallback'),'Plan guardrail breach Weekly distance exceeds the agreed limit.')
})
test('unknown objects never leak arbitrary payloads',()=>{assert.equal(describeApiDetail({input:'secret',ctx:'secret'},'Fallback'),'Fallback')})
