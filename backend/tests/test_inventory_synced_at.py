"""synced_at must mean "last time the device reported this item", not "row
first inserted" — the upsert path used to update `complete` in place while
leaving synced_at frozen, so a fresh snapshot looked days old."""

import uuid
from datetime import datetime


def _item(iid=None, day=10, complete=False):
    return {
        "id": iid or str(uuid.uuid4()),
        "displayName": "Run",
        "date": {"year": 2026, "month": 7, "day": day, "hour": 8, "minute": 0},
        "complete": complete,
    }


def _synced_at(client, iid):
    rows = {i["id"]: i for i in client.get("/api/workouts/inventory").json()}
    return datetime.fromisoformat(rows[iid]["synced_at"])


def test_resync_bumps_synced_at_on_updated_rows(client_a):
    item = _item()
    assert client_a.put("/api/workouts/inventory", json=[item]).status_code == 200
    first = _synced_at(client_a, item["id"])

    # Same id, flag flipped in place — the pre-fix behavior kept synced_at frozen.
    item["complete"] = True
    assert client_a.put("/api/workouts/inventory", json=[item]).status_code == 200
    second = _synced_at(client_a, item["id"])

    assert second > first
    assert client_a.get("/api/workouts/inventory").json()[0]["complete"] is True


def test_snapshot_shares_one_timestamp_across_rows(client_a):
    kept = _item(day=1)
    assert client_a.put("/api/workouts/inventory", json=[kept]).status_code == 200

    added = _item(day=2)
    assert client_a.put("/api/workouts/inventory", json=[kept, added]).status_code == 200

    # Updated row and inserted row carry the same snapshot timestamp.
    assert _synced_at(client_a, kept["id"]) == _synced_at(client_a, added["id"])


def test_inventory_preserves_multiple_actual_devices_without_latest_claim(client_a):
    from datetime import datetime, timezone
    q = client_a.post('/api/queue',json={'activityType':'running','title':'Versions','workoutData':{'activityType':'running','singleGoal':{'type':'distance','value':5000,'unit':'meters'}}}).json()
    first = client_a.get('/api/workouts/queue').json()[0]
    client_a.patch('/api/queue/'+q['id'],json={'workoutData':{'activityType':'running','singleGoal':{'type':'distance','value':5100,'unit':'meters'}}})
    second = client_a.get('/api/workouts/queue').json()[0]
    devices = [{'devicePlanId':x['prescriptionRevision'],'prescriptionRevision':x['prescriptionRevision'],'contentHash':x['contentHash'],'observedAt':datetime.now(timezone.utc).isoformat(),'date':{'year':None,'month':None,'day':None,'hour':None,'minute':None},'complete':False} for x in [first,second]]
    response = client_a.put('/api/workouts/inventory',json=[{'id':q['id'],'logicalWorkoutId':q['id'],'displayName':'Multiple scheduled versions','date':{},'complete':False,'observedDevices':devices}])
    assert response.status_code == 200
    row = next(x for x in client_a.get('/api/workouts/inventory').json() if x['id']==q['id'])
    assert row['ambiguous'] is True
    assert len(row['observed_devices']) == 2
    assert row['prescription_revision'] is None
    assert row['device_plan_id'] is None
    assert row['content_hash'] is None
    assert row['observed_at'] is None
    assert all(row[x] is None for x in ['year','month','day','hour','minute'])


def test_inventory_unknown_date_legacy_payload_is_preserved_unknown(client_a):
    import uuid
    item = {'id':str(uuid.uuid4()),'displayName':'Unknown date','date':{},'complete':False}
    assert client_a.put('/api/workouts/inventory',json=[item]).status_code == 200
    row = client_a.get('/api/workouts/inventory').json()[0]
    assert row['ambiguous'] is False
    assert row['observed_devices'] == []
    assert all(row[key] is None for key in ['year','month','day','hour','minute'])


def test_inventory_observation_bounds_and_duplicate_ids_fail_atomically(client_a):
    import uuid
    logical = str(uuid.uuid4())
    base = {'id':logical,'displayName':'Observed','date':{},'complete':False}
    device = {'devicePlanId':logical,'date':{},'complete':False}
    assert client_a.put('/api/workouts/inventory',json=[{**base,'observedDevices':[device,device]}]).status_code == 422
    assert client_a.get('/api/workouts/inventory').json() == []
    devices = [{**device,'devicePlanId':str(uuid.uuid4())} for _ in range(11)]
    assert client_a.put('/api/workouts/inventory',json=[{**base,'observedDevices':devices}]).status_code == 422
    assert client_a.get('/api/workouts/inventory').json() == []
    assert client_a.put('/api/workouts/inventory',json=[{**base,'date':{'year':2026,'month':2,'day':30}}]).status_code == 422


def test_inventory_cannot_use_foreign_device_revision(client_a, client_b):
    from datetime import datetime, timezone
    q = client_b.post('/api/queue',json={'activityType':'running','title':'Other athlete','workoutData':{'activityType':'running','singleGoal':{'type':'distance','value':5000,'unit':'meters'}}}).json()
    other = client_b.get('/api/workouts/queue').json()[0]
    import uuid
    own_logical = str(uuid.uuid4())
    payload = {'id':own_logical,'displayName':'Must reject','date':{},'observedDevices':[{'devicePlanId':other['prescriptionRevision'],'prescriptionRevision':other['prescriptionRevision'],'contentHash':other['contentHash'],'observedAt':datetime.now(timezone.utc).isoformat(),'date':{},'complete':False}]}
    assert client_a.put('/api/workouts/inventory',json=[payload]).status_code == 422
    assert client_a.get('/api/workouts/inventory').json() == []
    assert client_a.put('/api/workouts/inventory',json=[{'id':q['id'],'displayName':'Foreign logical','date':{}}]).status_code == 422


def test_ambiguous_devices_cannot_claim_one_latest_revision(client_a):
    import uuid
    logical, first, second = [str(uuid.uuid4()) for _ in range(3)]
    payload = {'id':logical,'displayName':'Ambiguous','date':{},'devicePlanId':first,'observedDevices':[{'devicePlanId':x,'date':{},'complete':False} for x in [first,second]]}
    assert client_a.put('/api/workouts/inventory',json=[payload]).status_code == 422
    assert client_a.get('/api/workouts/inventory').json() == []
