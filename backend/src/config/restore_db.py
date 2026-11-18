"""Isolated restore-database provisioning.

The restore target is always a *separate* database, never the primary one:
- PostgreSQL: a sibling database (default ``locklab_restore``) on the same server.
- SQLite: a separate file (default ``restore.db``) next to the primary file.

Tests may point RESTORE_DATABASE_URL at an in-memory/static SQLite URL instead.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from src.config.settings import DATABASE_URL, RESTORE_DATABASE_URL


def restore_url() -> str:
    if RESTORE_DATABASE_URL:
        return RESTORE_DATABASE_URL
    url = make_url(DATABASE_URL)
    if url.drivername.startswith('postgresql'):
        return url.set(database='locklab_restore').render_as_string(hide_password=False)
    # SQLite: file path lives in url.database.
    if url.database and url.database not in (':memory:',):
        base = url.database
        sibling = base[:-3] + '.restore.db' if base.endswith('.db') else base + '.restore.db'
        return f'sqlite:///{sibling}'
    return 'sqlite:///./restore.db'


def ensure_restore_database() -> str:
    """Create the isolated database if missing, then return its URL."""
    target = restore_url()
    url = make_url(target)
    if url.drivername.startswith('postgresql'):
        # Connect to the maintenance database to issue CREATE DATABASE (cannot run
        # inside a transaction block, so autocommit is required).
        admin_url = url.set(database='postgres').render_as_string(hide_password=False)
        admin_engine = create_engine(admin_url, isolation_level='AUTOCOMMIT', pool_pre_ping=True)
        try:
            with admin_engine.connect() as conn:
                exists = conn.execute(
                    text('SELECT 1 FROM pg_database WHERE datname = :name'),
                    {'name': url.database},
                ).scalar()
                if not exists:
                    conn.execute(text(f'CREATE DATABASE "{url.database}"'))
        finally:
            admin_engine.dispose()
    else:
        # SQLite creates the file on first connect; in-memory URLs need no setup.
        engine = create_engine(target)
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
        finally:
            engine.dispose()
    return target


def restore_engine():
    return create_engine(restore_url(), pool_pre_ping=True)
