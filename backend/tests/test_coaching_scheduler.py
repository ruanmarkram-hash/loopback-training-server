"""Scheduler liveness and publication serialization against real PostgreSQL."""
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from app.coaching_service import lock_athlete
from app.coaching_scheduler import run_review_scheduler, scan_reviews
from app.models.plan import Plan
from app.models.queue import WorkoutQueue
from app.models.user import User


def test_scheduler_lock_wait_does_not_block_event_loop(client_a, session_factory):
    plan = client_a.post('/api/plans', json={'name':'Liveness','activityType':'running','startDate':'2026-10-01'}).json()
    locked = threading.Event()
    release = threading.Event()
    def hold():
        with session_factory() as db:
            row = db.get(Plan, uuid.UUID(plan['id']))
            lock_athlete(db, row.user_id)
            locked.set()
            release.wait(timeout=3)
            db.rollback()
    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(timeout=2)
    watchdog = threading.Timer(0.7, release.set)
    watchdog.start()
    async def probe():
        task = asyncio.create_task(run_review_scheduler(session_factory))
        begin = time.monotonic()
        try:
            await asyncio.sleep(0.05)
            elapsed = time.monotonic()-begin
            release.set()
            await asyncio.sleep(0.1)
            return elapsed
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    try:
        assert asyncio.run(probe()) < 0.3
    finally:
        release.set()
        holder.join(timeout=3)
        watchdog.cancel()


def test_publication_rechecks_retired_plan_after_stale_read(client_a, session_factory):
    from app.coaching_scheduler import publish_forecast
    from sqlalchemy import func
    p = client_a.post('/api/plans',json={'name':'Forecast','activityType':'running','startDate':'2026-10-01'}).json()
    forecast_id = uuid.uuid4()
    with session_factory() as setup:
        plan = setup.get(Plan,uuid.UUID(p['id']))
        plan.metadata_ = {'programFacts':{'available_days':['mon','tue','wed','thu','fri','sat','sun']},'forecastApproved':True,'forecast':[{'id':str(forecast_id),'scheduledDate':(datetime.now(timezone.utc)+timedelta(days=3)).isoformat(),'title':'Future','composition':{'activityType':'running','singleGoal':{'type':'distance','value':5000,'unit':'meters'}}}]}
        setup.commit()
    with session_factory() as stale, session_factory() as retire:
        loaded = stale.get(Plan,uuid.UUID(p['id']))
        retired = retire.get(Plan,loaded.id)
        retired.status = 'completed'
        retire.commit()
        publish_forecast(stale,loaded.user_id,loaded)
        stale.commit()
    with session_factory() as observed:
        assert observed.get(WorkoutQueue,forecast_id) is None


def test_two_publication_sessions_do_not_duplicate_forecast(client_a, session_factory):
    from concurrent.futures import ThreadPoolExecutor
    from app.coaching_scheduler import publish_forecast
    p = client_a.post('/api/plans',json={'name':'Concurrent forecast','activityType':'running','startDate':'2026-10-01'}).json()
    forecast_id = uuid.uuid4()
    with session_factory() as setup:
        plan = setup.get(Plan,uuid.UUID(p['id']))
        plan.metadata_ = {'programFacts':{'available_days':['mon','tue','wed','thu','fri','sat','sun']},'forecastApproved':True,'forecast':[{'id':str(forecast_id),'scheduledDate':(datetime.now(timezone.utc)+timedelta(days=3)).isoformat(),'title':'Future','composition':{'activityType':'running','singleGoal':{'type':'distance','value':5000,'unit':'meters'}}}]}
        setup.commit()
    barrier = threading.Barrier(2)
    def publish():
        with session_factory() as db:
            loaded = db.get(Plan,uuid.UUID(p['id']))
            barrier.wait(timeout=2)
            publish_forecast(db,loaded.user_id,loaded)
            time.sleep(0.1)
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish) for _ in range(2)]
        for result in futures:
            result.result(timeout=5)
    with session_factory() as observed:
        assert observed.get(WorkoutQueue,forecast_id) is not None
