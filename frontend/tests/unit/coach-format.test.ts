import test from 'node:test'
import assert from 'node:assert/strict'
import { parsePace, parseGoalTime, formatPrescriptionValue, formatGoal } from '../../src/lib/coach-format.ts'

test('pace input uses minutes per kilometre with strict seconds and positive values',()=>{
 assert.equal(parsePace('5:15'),315)
 assert.equal(parsePace(' 6:00 '),360)
 for(const invalid of ['', '0:00','-1:00','5:99','5.5','NaN'])assert.equal(parsePace(invalid),null)
})
test('goal time accepts minutes or hours without treating aspiration as pace',()=>{
 assert.equal(parseGoalTime('45:00'),2700)
 assert.equal(parseGoalTime('1:30:00'),5400)
 for(const invalid of ['', '0:00','1:60:00','1:20:90','-1:00'])assert.equal(parseGoalTime(invalid),null)
})
test('proposal values state canonical running units',()=>{
 assert.equal(formatPrescriptionValue('pace_seconds_per_km',315),'5:15 /km')
 assert.equal(formatPrescriptionValue('distance_meters',5200),'5.2 km')
 assert.equal(formatPrescriptionValue('duration_seconds',1800),'30 min')
})

test('event goal renders calendar date and finish time once without raw storage keys',()=>{
 const text=formatGoal({type:'10k',race_date:'2027-01-02',target_seconds:2940})
 assert.equal(text,'10k: Event 2 Jan 2027 · Target 49:00 finish')
 assert.ok(!text.includes('2027-01-02'))
 assert.ok(!text.includes('target_seconds'))
 assert.equal(formatGoal({type:'weekly_volume',target:20,unit:'km',by_week:4,detail:'Keep recovery easy'}),'Weekly volume: 20 km by week 4 · Keep recovery easy')
})
