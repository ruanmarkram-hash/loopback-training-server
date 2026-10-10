"""Server-managed backups (app/backup.py) + POST /api/admin/backup.

run_backup shells out to a real pg_dump against the test database, so these
also prove the runtime image / CI runner carries a pg_dump that can talk to
the Postgres version in use.
"""

import gzip

import pytest

import app.backup as backup_module
from app.backup import BackupError, _pg_uri, run_backup
from app.config import get_settings

TEST_DB_NAME = "training_api_test"


def _swap_db(uri: str, name: str) -> str:
    return uri.rsplit("/", 1)[0] + "/" + name


@pytest.fixture()
def backup_settings(engine, tmp_path, monkeypatch):
    """Settings pointing run_backup at the test DB and a tmp backup dir."""

    def make(**over):
        s = get_settings().model_copy(
            update={
                "database_url": _swap_db(get_settings().db_uri, TEST_DB_NAME),
                "db_host": None,
                "backup_dir": str(tmp_path),
                **over,
            }
        )
        monkeypatch.setattr(backup_module, "get_settings", lambda: s)
        return s

    return make


def test_pg_uri_strips_driver():
    assert _pg_uri("postgresql+psycopg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert _pg_uri("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


def test_run_backup_writes_valid_dump(backup_settings, tmp_path):
    backup_settings()
    path = run_backup("test")
    assert path.parent == tmp_path
    assert path.name.startswith("training-api-") and path.name.endswith(".sql.gz")
    with gzip.open(path, "rb") as f:
        content = f.read()
    assert b"PostgreSQL database dump" in content
    assert not list(tmp_path.glob("*.partial"))


def test_run_backup_prunes_to_keep(backup_settings, tmp_path):
    backup_settings(backup_keep=2)
    fakes = [tmp_path / f"training-api-2020010{i}-000000.sql.gz" for i in (1, 2, 3)]
    for f in fakes:
        f.write_bytes(b"old dump")
    (tmp_path / "unrelated.txt").write_text("never pruned")

    path = run_backup("test")
    remaining = sorted(tmp_path.glob("training-api-*.sql.gz"))
    assert remaining == [fakes[2], path]
    assert (tmp_path / "unrelated.txt").exists()


def test_run_backup_missing_dir_raises(backup_settings, tmp_path):
    backup_settings(backup_dir=str(tmp_path / "nope"))
    with pytest.raises(BackupError, match="does not exist"):
        run_backup("test")


def test_backup_endpoint(backup_settings, client_admin):
    backup_settings()
    resp = client_admin.post("/api/admin/backup")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"file", "sizeBytes", "completedAt"}
    assert body["file"].startswith("training-api-")
    assert body["sizeBytes"] > 0


def test_backup_endpoint_unavailable_dir_is_503(backup_settings, tmp_path, client_admin):
    backup_settings(backup_dir=str(tmp_path / "nope"))
    resp = client_admin.post("/api/admin/backup")
    assert resp.status_code == 503
    assert "does not exist" in resp.json()["detail"]


def test_backup_endpoint_rejects_non_admin(client_a):
    assert client_a.post("/api/admin/backup").status_code == 403


def test_pre_migrate_fails_closed_on_existing_unmanaged_schema(backup_settings,client_a):
    backup_settings()
    # The API TestClient uses a real create_all schema without Alembic ownership.
    client_a.get('/api/auth/me')
    with pytest.raises(BackupError, match='unmanaged schema'):
        backup_module.pre_migrate()


def test_stalled_dump_deadline_cleans_partial_and_releases_lock(backup_settings,tmp_path,monkeypatch):
    import subprocess,sys,time
    backup_settings()
    real_popen=subprocess.Popen
    started=time.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(backup_module,'_PG_DUMP_TIMEOUT_S',0.2)
        patch.setattr(backup_module.subprocess,'Popen',lambda *a,**kw:real_popen([sys.executable,'-c','import time;time.sleep(60)'],**kw))
        with pytest.raises(BackupError,match='timed out'):
            run_backup('stalled child fixture')
    assert time.monotonic()-started<3
    assert not list(tmp_path.glob('*.sql.gz*'))
    assert run_backup('after timeout').exists()


def test_dump_connection_preserves_libpq_options_without_password_arguments(backup_settings, monkeypatch):
    real_popen = backup_module.subprocess.Popen
    observed = {}
    def record(command, **kwargs):
        observed.update(command=command, env=kwargs['env'])
        return real_popen(command, **kwargs)
    settings = backup_settings()
    settings = backup_settings(database_url=settings.db_uri + '?sslmode=prefer&application_name=backup_fixture&options=-c%20statement_timeout%3D10000')
    monkeypatch.setattr(backup_module.subprocess, 'Popen', record)
    run_backup('connection option fixture')
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    expected_connection = conninfo_to_dict(_pg_uri(settings.db_uri))
    expected_password = expected_connection.pop('password')
    connection = conninfo_to_dict(observed['command'][observed['command'].index('--dbname') + 1])
    assert connection['sslmode'] == 'prefer'
    assert connection['application_name'] == 'backup_fixture'
    assert connection['options'] == '-c statement_timeout=10000'
    assert 'password' not in connection
    assert expected_password
    assert observed['env']['PGPASSWORD'] == expected_password
    # CI legitimately uses 'postgres' for both password and username. Compare
    # the entire password-free argument list, rather than rejecting that text
    # when it occurs in a public connection field. No extra secret argument fits.
    assert observed['command'] == ['pg_dump', '--dbname', make_conninfo(**expected_connection)]
