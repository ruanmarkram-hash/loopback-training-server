"""Server-managed database backups: pg_dump into settings.backup_dir.

Three triggers share run_backup():
- nightly at settings.backup_time — run_scheduler(), started from the app's
  lifespan (main.py), with a catch-up pass when the newest dump is stale
  (a home server that was off at backup time still gets its nightly)
- before pending migrations on startup — `python -m app.backup pre-migrate`
  in scripts/start.sh, so a failed upgrade migration is always recoverable
- on demand — POST /api/admin/backup (the System screen's "Back up now")

Dumps are `training-api-<stamp>[-<tag>].sql.gz`, the same shape a host-side
cron produces, so the System screen's freshness report and the retention
sweep treat both alike. Retention keeps the newest settings.backup_keep
files matching that pattern — anything else in the directory is untouched.

BACKUP_ENABLED=false turns off the scheduler and the pre-migrate dump for
installs that manage backups themselves; the manual endpoint still works
(an explicit human action), as does `python -m app.backup now`.
"""

import asyncio
import fcntl
import gzip
import logging
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger("uvicorn.error")

_DUMP_GLOB = "training-api-*.sql.gz"
# An empty-but-valid dump of this schema is a few KB of DDL; anything smaller
# means pg_dump died before writing the schema.
_MIN_PLAUSIBLE_BYTES = 1024
_PG_DUMP_TIMEOUT_S = 600


class BackupError(Exception):
    """A backup could not be made; the message is safe to show an admin."""


def _pg_uri(url: str) -> str:
    """SQLAlchemy URL → libpq URI (postgresql+psycopg:// → postgresql://)."""
    return re.sub(r"^postgresql\+[^:]+", "postgresql", url)


def _dump_records(backup_dir: Path) -> list[tuple[Path, os.stat_result]]:
    """One freshness snapshot; retention may remove an enumerated archive."""
    records = []
    for path in backup_dir.glob(_DUMP_GLOB):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        records.append((path, stat))
    return sorted(records, key=lambda record: (record[1].st_mtime_ns, record[0].name))


def _dumps(backup_dir: Path) -> list[Path]:
    return [path for path, _ in _dump_records(backup_dir)]


def run_backup(reason: str, *, tag: str = "") -> Path:
    settings = get_settings()
    backup_dir = Path(settings.backup_dir)
    if not backup_dir.is_dir():
        raise BackupError(f"backup directory {backup_dir} does not exist (is the volume mounted?)")
    if not os.access(backup_dir, os.W_OK):
        raise BackupError(f"backup directory {backup_dir} is not writable (mounted read-only?)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    final = backup_dir / f"training-api-{stamp}-{uuid.uuid4().hex}{'-' + tag if tag else ''}.sql.gz"
    partial = final.with_name(final.name + ".partial")

    with open(backup_dir / ".backup.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("a backup is already in progress") from None

        logger.info("backup (%s): dumping to %s", reason, final.name)
        # stderr goes to a temp file, not a pipe: a failing pg_dump can write
        # more than a pipe buffer holds, which would deadlock the stdout read.
        # Both outputs are files, so communicate's deadline covers the whole child
        # process even when the network stalls before it produces a first byte.
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        connection = conninfo_to_dict(_pg_uri(settings.db_uri))
        password = connection.pop("password", "")
        env = {**os.environ, "PGPASSWORD": password}
        # Preserve every libpq URI option (TLS verification, certificates,
        # options, timeouts, multi-host settings) in a password-free conninfo.
        command = ["pg_dump", "--dbname", make_conninfo(**connection)]
        with tempfile.TemporaryFile() as raw, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(command, stdout=raw, stderr=err, env=env)
            total = 0
            partial_owned = False
            try:
                try:
                    proc.communicate(timeout=_PG_DUMP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                    raise BackupError("pg_dump timed out; backup not retained") from None
                if proc.returncode != 0:
                    err.seek(0)
                    stderr = err.read().decode(errors="replace").strip()
                    raise BackupError(
                        f"pg_dump failed: {stderr.splitlines()[-1] if stderr else f'exit code {proc.returncode}'}"
                    )
                raw.seek(0)
                with partial.open("xb") as file:
                    partial_owned = True
                    with gzip.GzipFile(fileobj=file, mode="wb") as gz:
                        while chunk := raw.read(1024 * 1024):
                            gz.write(chunk)
                            total += len(chunk)
                if total < _MIN_PLAUSIBLE_BYTES:
                    raise BackupError(f"dump implausibly small ({total} bytes uncompressed); not keeping it")
                try:
                    os.link(partial, final)
                except OSError as exc:
                    raise BackupError("backup publication failed; existing archives preserved") from exc
                partial.unlink()
            except BaseException as exc:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if partial_owned:
                    partial.unlink(missing_ok=True)
                if isinstance(exc, FileExistsError):
                    raise BackupError("backup temporary archive already exists; preserved") from exc
                raise

        # Retention: newest backup_keep dumps stay, older ones go.
        for old in _dumps(backup_dir)[: -settings.backup_keep]:
            old.unlink(missing_ok=True)
            logger.info("backup retention: pruned %s", old.name)

    return final


def _seconds_until(hhmm: str) -> float:
    hour, minute = (int(p) for p in hhmm.split(":"))
    now = datetime.now()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


async def run_scheduler() -> None:
    """Nightly-backup loop; started as a lifespan task, exits if not applicable."""
    settings = get_settings()
    if not settings.backup_enabled:
        logger.info("server-managed backups disabled (BACKUP_ENABLED=false)")
        return
    backup_dir = Path(settings.backup_dir)
    if not backup_dir.is_dir() or not os.access(backup_dir, os.W_OK):
        logger.warning(
            "backup directory %s is missing or read-only — scheduled backups disabled "
            "(mount it read-write, or set BACKUP_ENABLED=false to silence this)",
            backup_dir,
        )
        return

    first = True
    while True:
        # Catch-up pass: if the newest dump is stale (server was off at backup
        # time), don't wait for tonight. Once only — a persistently failing
        # backup must not retry every minute.
        dumps = _dump_records(backup_dir)
        stale = not dumps or (datetime.now(timezone.utc).timestamp() - dumps[-1][1].st_mtime) > 26 * 3600
        delay = 60 if (first and stale) else _seconds_until(settings.backup_time)
        first = False
        await asyncio.sleep(delay)
        try:
            path = await asyncio.to_thread(run_backup, "scheduled")
            logger.info("backup (scheduled): wrote %s (%d bytes)", path.name, path.stat().st_size)
        except BackupError as e:
            logger.warning("backup (scheduled) failed: %s", e)
        except Exception:
            logger.exception("backup (scheduled) failed unexpectedly")


def pre_migrate() -> None:
    """Fail closed before changing a nonempty schema without a verified backup."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, inspect, text

    settings = get_settings()
    engine = create_engine(settings.db_uri)
    try:
        with engine.connect() as conn:
            tables = inspect(conn).get_table_names()
            if not tables:
                print("pre-migration backup: skipped (fresh database, nothing to protect)")
                return
            if "alembic_version" not in tables:
                raise BackupError("Existing unmanaged schema has no Alembic revision; migration refused")
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if current == head:
        print("pre-migration backup: skipped (schema already at head)")
        return
    if not settings.backup_enabled:
        raise BackupError("Pending schema migration requires a backup; BACKUP_ENABLED is false")
    path = run_backup(f"pre-migrate {current} -> {head}", tag="premigrate")
    print(f"pre-migration backup: {path.name} ({current} -> {head})")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "now"
    if cmd == "pre-migrate":
        pre_migrate()
    elif cmd == "now":
        try:
            out = run_backup("manual CLI")
            print(f"wrote {out} ({out.stat().st_size} bytes)")
        except BackupError as e:
            sys.exit(f"backup failed: {e}")
    else:
        sys.exit("usage: python -m app.backup [now|pre-migrate]")
