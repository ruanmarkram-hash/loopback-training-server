import test from 'node:test'
import assert from 'node:assert/strict'
import {parseGoalTime,parsePace,decimalSeconds,formatElapsedSeconds} from '../src/lib/coach-format.ts'
test('elapsed inputs preserve fractional seconds and durations longer than a day',()=>{
 assert.equal(parseGoalTime('25:01:02.375'),90062.375)
 assert.equal(parseGoalTime('50:00.125'),3000.125)
 assert.equal(parsePace('6:00.375'),360.375)
})
test('unknown and invalid components never become valid supplied facts',()=>{
 for(const value of ['', '0:00','6:60','-1:30','1:02:60','1.5:02'])assert.equal(parseGoalTime(value),null)
 for(const value of ['', '0:00','6:60','1:02:03','-1:30','1.5:02'])assert.equal(parsePace(value),null)
})

test('tiny positive seconds retain shortest round-trip precision through displayed parts',()=>{
 for(const value of [1e-7,1.2345678901234568e-7,Number.MIN_VALUE,360.375]){
  const decimal=decimalSeconds(value);assert.ok(!decimal.includes('e'));assert.equal(Number(decimal),value)
  assert.equal(parsePace(formatElapsedSeconds(value)),value)
  assert.equal(parseGoalTime(formatElapsedSeconds(value,true)),value)
 }
 assert.equal(formatElapsedSeconds(1e-7),'0:00.0000001')
 assert.equal(formatElapsedSeconds(1e-7,true),'0:00:00.0000001')
})
