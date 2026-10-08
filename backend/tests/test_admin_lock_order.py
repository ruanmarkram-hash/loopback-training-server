"""Independent concurrent HTTP transactions must not acquire crossed admin locks."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import OperationalError

from app.models.api_token import ApiToken
from app.models.user import User
from app.security import verify_password
from tests.conftest import _client, _make_user


def test_concurrent_admin_cross_password_resets(client_admin, admin_user, session_factory, monkeypatch):
    import app.routes.admin as routes

    second = _make_user(session_factory, "second-admin", role="admin")
    client_second = _client(second[1])
    original_hash = routes.hash_password
    overlap = threading.Barrier(2)

    def synchronized_hash(value):
        # Force overlap after authentication acquired its locks. A correct
        # serialization may admit only one endpoint, so the barrier is bounded.
        try:
            overlap.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return original_hash(value)

    monkeypatch.setattr(routes, "hash_password", synchronized_hash)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(
                client_admin.post,
                "/api/admin/users/" + str(second[0]) + "/password",
                json={"password": "synthetic-second-password"},
            ),
            pool.submit(
                client_second.post,
                "/api/admin/users/" + str(admin_user[0]) + "/password",
                json={"password": "synthetic-first-password"},
            ),
        ]
        results = []
        for call in calls:
            try:
                results.append(call.result(timeout=8))
            except OperationalError as error:
                results.append(error)
    outcomes = [
        getattr(
            result, "status_code", (type(result).__name__, getattr(getattr(result, "orig", None), "sqlstate", None))
        )
        for result in results
    ]
    assert outcomes == [204, 204]
    with session_factory() as db:
        assert verify_password(db.get(User, admin_user[0]).password_hash, "synthetic-first-password")
        assert verify_password(db.get(User, second[0]).password_hash, "synthetic-second-password")


@pytest.mark.parametrize("change,expected", [("deactivate", 401), ("revoke", 401), ("role", 403)])
def test_waiting_admin_mutation_revalidates_authentication(
    client_admin, admin_user, session_factory, monkeypatch, change, expected
):
    from app import auth

    target = _make_user(session_factory, "target")
    waiting = threading.Event()
    with session_factory() as holder:
        holder.execute(select(func.pg_advisory_xact_lock(auth._ADMIN_WRITE_LOCK_KEY)))
        engine = holder.get_bind()

        def observe(connection, cursor, statement, parameters, context, executemany):
            if "pg_advisory_xact_lock" in statement:
                waiting.set()

        event.listen(engine, "before_cursor_execute", observe)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    client_admin.post,
                    "/api/admin/users/" + str(target[0]) + "/password",
                    json={"password": "must-not-be-applied"},
                )
                if not waiting.wait(timeout=3):
                    holder.rollback()
                    raise AssertionError("Admin request did not reach the advisory lock")
                actor = holder.get(User, admin_user[0])
                if change == "deactivate":
                    actor.is_active = False
                elif change == "role":
                    actor.role = "user"
                else:
                    holder.execute(delete(ApiToken).where(ApiToken.user_id == actor.id))
                holder.commit()
                assert pending.result(timeout=5).status_code == expected
        finally:
            holder.rollback()
            event.remove(engine, "before_cursor_execute", observe)
    with session_factory() as db:
        assert verify_password(db.get(User, target[0]).password_hash, "pw")
