"""Test harness: SQLite primary + separate SQLite isolated restore DB + fake Redis.

The production code keeps PostgreSQL syntax/dialect branches untouched; these
tests exercise the same service layer with file-backed SQLite so the full
backup/restore lifecycle is reproducible without docker.
"""
import os, sys, tempfile, types, shutil
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix='br-test-'))
sys.path.insert(0, str(BACKEND))

PRIMARY_DB = WORK / 'primary.db'
RESTORE_DB = WORK / 'restore.db'
BACKUP_DIR = WORK / 'backups'

os.environ['DATABASE_URL'] = f'sqlite:///{PRIMARY_DB}'
os.environ['RESTORE_DATABASE_URL'] = f'sqlite:///{RESTORE_DB}'
os.environ['BACKUP_DIR'] = str(BACKUP_DIR)
os.environ['SEED_DEMO'] = 'false'
os.environ['JWT_SECRET'] = 'test-secret'


class FakeRedis:
    _shared = None

    def __init__(self):
        self.lists = {}

    @classmethod
    def from_url(cls, *a, **k):
        # Mirror redis-py's constructor while keeping one shared in-memory store.
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])
    def blpop(self, key, timeout=0):
        data = self.lists.get(key)
        if not data:
            return None
        return (key, data.pop(0))
    def ping(self):
        return True


fake_redis = FakeRedis()
FakeRedis._shared = fake_redis
fake_redis_module = types.ModuleType('redis')
fake_redis_module.Redis = FakeRedis
sys.modules['redis'] = fake_redis_module


def _enable_wal(dbapi_conn, _record):
    # WAL gives the snapshot read transaction a fixed point-in-time view while
    # another connection commits concurrently (production still uses PostgreSQL
    # REPEATABLE READ for the same guarantee).
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.execute('PRAGMA busy_timeout=10000')
    cur.close()


# Register once at import time, before any engine connection is opened.
from sqlalchemy import event  # noqa: E402
from src.config.database import Base, engine  # noqa: E402
from src.config import restore_db  # noqa: E402
event.listen(engine, 'connect', _enable_wal)


@pytest.fixture()
def env():
    # Fresh databases for every test.
    engine.dispose()
    for db_file in (PRIMARY_DB, RESTORE_DB):
        db_file.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm'):
            db_file.with_name(db_file.name + suffix).unlink(missing_ok=True)
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    BACKUP_DIR.mkdir(parents=True)
    fake_redis.lists.clear()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    isolated = restore_db.restore_engine()
    Base.metadata.drop_all(isolated)
    isolated.dispose()
    yield types.SimpleNamespace(work=WORK, primary=PRIMARY_DB, restore=RESTORE_DB,
                                backup_dir=BACKUP_DIR, redis=fake_redis)
