"""Real gzip/publication with a mocked pg_dump child: unit evidence only."""

import gzip
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import backup


def test_same_second_successes_preserve_distinct_archives(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 1, 2, 3, tzinfo=UTC)

    monkeypatch.setattr(backup, "datetime", Clock)
    monkeypatch.setattr(
        backup,
        "get_settings",
        lambda: SimpleNamespace(
            backup_dir=str(tmp_path), backup_keep=3, db_uri="postgresql://synthetic@localhost/synthetic"
        ),
    )
    payloads = iter([b"first synthetic dump " * 100, b"second synthetic dump " * 100])
    expected = []

    class Dump:
        returncode = 0

        def __init__(self, command, stdout, stderr, env):
            data = next(payloads)
            expected.append(data)
            stdout.write(data)

        def communicate(self, timeout):
            pass

        def poll(self):
            return 0

    monkeypatch.setattr(backup.subprocess, "Popen", Dump)
    paths = [backup.run_backup("synthetic unit"), backup.run_backup("synthetic unit")]
    assert paths[0] != paths[1]
    assert [gzip.decompress(p.read_bytes()) for p in paths] == expected
    assert len(list(tmp_path.glob("training-api-*.sql.gz"))) == 2


@pytest.fixture
def mock_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(
        backup,
        "get_settings",
        lambda: SimpleNamespace(
            backup_dir=str(tmp_path), backup_keep=2, db_uri="postgresql://synthetic@localhost/synthetic"
        ),
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 1, 2, 3, tzinfo=UTC)

    class Dump:
        returncode = 0

        def __init__(self, command, stdout, stderr, env):
            stdout.write(b"synthetic mock dump " * 100)

        def communicate(self, timeout):
            pass

        def poll(self):
            return 0

    monkeypatch.setattr(backup, "datetime", Clock)
    monkeypatch.setattr(backup.subprocess, "Popen", Dump)


def test_preexisting_final_cannot_be_overwritten(mock_dump, tmp_path, monkeypatch):
    suffix = "f" * 32
    monkeypatch.setattr(backup.uuid, "uuid4", lambda: SimpleNamespace(hex=suffix))
    existing = tmp_path / f"training-api-20261009-010203-{suffix}.sql.gz"
    existing.write_bytes(b"original archive")
    with pytest.raises(backup.BackupError, match="publication failed"):
        backup.run_backup("mock collision")
    assert existing.read_bytes() == b"original archive"
    assert not list(tmp_path.glob("*.partial"))
    monkeypatch.setattr(backup.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    assert backup.run_backup("mock collision recovery").exists()


def test_publication_failure_cleans_owned_partial_and_releases_lock(mock_dump, tmp_path, monkeypatch):
    real_link = backup.os.link

    def failed_link(*args):
        raise PermissionError("synthetic publication failure")

    monkeypatch.setattr(backup.os, "link", failed_link)
    with pytest.raises(backup.BackupError, match="publication failed"):
        backup.run_backup("mock failed publication")
    assert not list(tmp_path.glob("training-api-*.sql.gz*"))
    monkeypatch.setattr(backup.os, "link", real_link)
    assert backup.run_backup("mock recovered publication").exists()


def test_preexisting_partial_is_never_truncated_or_removed(mock_dump, tmp_path, monkeypatch):
    suffix = "f" * 32
    monkeypatch.setattr(backup.uuid, "uuid4", lambda: SimpleNamespace(hex=suffix))
    existing = tmp_path / f"training-api-20261009-010203-{suffix}.sql.gz.partial"
    existing.write_bytes(b"unowned partial")
    with pytest.raises(backup.BackupError, match="temporary archive already exists"):
        backup.run_backup("mock partial collision")
    assert existing.read_bytes() == b"unowned partial"
    assert not list(tmp_path.glob("*.sql.gz"))


def test_retention_and_freshness_follow_mtime_not_reverse_uuid(mock_dump, tmp_path, monkeypatch):
    suffixes = iter(["f" * 32, "e" * 32, "a" * 32])
    monkeypatch.setattr(backup.uuid, "uuid4", lambda: SimpleNamespace(hex=next(suffixes)))
    first = backup.run_backup("mock first")
    os.utime(first, ns=(1_000_000_001, 1_000_000_001))
    second = backup.run_backup("mock second")
    os.utime(second, ns=(1_000_000_002, 1_000_000_002))
    third = backup.run_backup("mock third")
    assert not first.exists() and second.exists() and third.exists()
    assert backup._dumps(tmp_path) == [second, third]
    assert third.name < second.name  # Lexical UUID order contradicts freshness.


def test_system_freshness_uses_actual_newest_archive(mock_dump, tmp_path, monkeypatch, client_admin):
    from app.routes import admin

    monkeypatch.setattr(admin, "get_settings", backup.get_settings)
    suffixes = iter(["f" * 32, "a" * 32])
    monkeypatch.setattr(backup.uuid, "uuid4", lambda: SimpleNamespace(hex=next(suffixes)))
    older = backup.run_backup("mock older")
    os.utime(older, ns=(1_000_000_001, 1_000_000_001))
    latest = backup.run_backup("mock newest")
    response = client_admin.get("/api/admin/system")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["backupCount"] == 2
    assert body["backup"]["file"] == latest.name
    assert body["backup"]["sizeBytes"] == latest.stat().st_size
    assert datetime.fromisoformat(body["backup"]["completedAt"]).timestamp() == pytest.approx(
        latest.stat().st_mtime, abs=1e-6
    )


@pytest.mark.parametrize("consumer", ["helper", "admin", "scheduler"])
def test_retention_deletion_during_stat_does_not_break_freshness(
    mock_dump, tmp_path, monkeypatch, client_admin, consumer
):
    import asyncio
    from pathlib import Path

    from app.routes import admin

    old = tmp_path / "training-api-older.sql.gz"
    latest = tmp_path / "training-api-newest.sql.gz"
    old.write_bytes(b"synthetic old archive")
    latest.write_bytes(b"synthetic newest archive")
    real_stat = Path.stat

    def deleting_stat(path, *args, **kwargs):
        if path == old:
            path.unlink(missing_ok=True)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deleting_stat)
    if consumer == "helper":
        assert backup._dumps(tmp_path) == [latest]
    elif consumer == "admin":
        monkeypatch.setattr(admin, "get_settings", backup.get_settings)
        response = client_admin.get("/api/admin/system")
        assert response.status_code == 200
        assert response.json()["backup"]["file"] == latest.name
        assert response.json()["backupCount"] == 1
    else:
        settings = backup.get_settings()
        settings.backup_enabled = True
        settings.backup_time = "02:00"
        monkeypatch.setattr(backup, "get_settings", lambda: settings)
        sleeps = []
        writes = []

        class Finished(Exception):
            pass

        async def bounded_sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise Finished

        monkeypatch.setattr(backup.asyncio, "sleep", bounded_sleep)
        monkeypatch.setattr(backup, "run_backup", lambda reason: writes.append(reason) or latest)
        with pytest.raises(Finished):
            asyncio.run(backup.run_scheduler())
        assert len(sleeps) == 2 and writes == ["scheduled"]


def test_snapshot_does_not_swallow_other_io_errors(tmp_path, monkeypatch):
    from pathlib import Path

    archive = tmp_path / "training-api-denied.sql.gz"
    archive.write_bytes(b"synthetic archive")
    real_stat = Path.stat

    def denied_stat(path, *args, **kwargs):
        if path == archive:
            raise PermissionError("synthetic permission error")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    with pytest.raises(PermissionError, match="synthetic permission"):
        backup._dump_records(tmp_path)


@pytest.mark.parametrize("consumer", ["admin", "scheduler"])
def test_freshness_consumers_use_cached_stat(mock_dump, tmp_path, monkeypatch, client_admin, consumer):
    import asyncio
    from pathlib import Path

    from app.routes import admin

    archive = tmp_path / "training-api-snapshot.sql.gz"
    archive.write_bytes(b"synthetic archive")
    original_stat = archive.stat()
    real_stat = Path.stat
    reads = []

    def one_stat(path, *args, **kwargs):
        if path == archive:
            reads.append(path)
            if len(reads) > 1:
                raise FileNotFoundError("synthetic removal after captured snapshot")
            return original_stat
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", one_stat)
    if consumer == "admin":
        monkeypatch.setattr(admin, "get_settings", backup.get_settings)
        response = client_admin.get("/api/admin/system")
        assert response.status_code == 200
        assert response.json()["backup"]["file"] == archive.name
    else:
        settings = backup.get_settings()
        settings.backup_enabled = True
        settings.backup_time = "02:00"
        monkeypatch.setattr(backup, "get_settings", lambda: settings)

        class Finished(Exception):
            pass

        async def stop_after_snapshot(delay):
            raise Finished

        monkeypatch.setattr(backup.asyncio, "sleep", stop_after_snapshot)
        with pytest.raises(Finished):
            asyncio.run(backup.run_scheduler())
    assert len(reads) == 1


def test_gzip_header_failure_cleans_owned_partial_and_recovers(mock_dump, tmp_path, monkeypatch):
    import errno
    from pathlib import Path

    opened = []
    real_open = Path.open

    def record_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if path.name.endswith(".partial"):
            opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", record_open)
    real_header = gzip.GzipFile._write_gzip_header

    def failed_header(self, *args):
        raise OSError(errno.ENOSPC, "synthetic full disk during gzip header")

    monkeypatch.setattr(gzip.GzipFile, "_write_gzip_header", failed_header)
    with pytest.raises(OSError) as error:
        backup.run_backup("mock gzip header failure")
    assert error.value.errno == errno.ENOSPC
    assert len(opened) == 1 and opened[0].closed
    assert not list(tmp_path.glob("training-api-*.sql.gz*"))
    monkeypatch.setattr(gzip.GzipFile, "_write_gzip_header", real_header)
    assert backup.run_backup("mock header failure recovery").exists()
