"""Point-in-time snapshot backups.

All business tables are exported inside ONE transaction so samples, transfer
applications and trials share the same commit point. PostgreSQL uses
REPEATABLE READ (a fixed snapshot for the whole transaction); SQLite is already
serializable. Each table becomes a JSONL file with a SHA-256 checksum recorded
in manifest.json, which later drives restore reconciliation.
"""
import json, hashlib, os, uuid
from datetime import datetime, timezone
from sqlalchemy import text
from src.config.database import engine, Base
from src.config.settings import BACKUP_DIR
from src.constants.enums import SNAPSHOT_TABLES, BACKUP_STATES
from src.models.entities import BackupJob, Audit


def _canonical(value):
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def _hash_lines(lines):
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode('utf-8'))
    return digest.hexdigest()


def backup_dir(backup_code):
    return os.path.join(BACKUP_DIR, backup_code)


def manifest_path(backup_code):
    return os.path.join(backup_dir(backup_code), 'manifest.json')


def load_manifest(backup_code):
    with open(manifest_path(backup_code), encoding='utf-8') as fh:
        return json.load(fh)


# Test/observability seam: invoked once the snapshot transaction has established
# its read snapshot, before any business table is exported. None in production.
SNAPSHOT_STARTED_HOOK = None


def list_jobs(db):
    rows = []
    for j in db.query(BackupJob).order_by(BackupJob.id.desc()).all():
        rows.append({'id': j.id, 'backup_code': j.backup_code, 'status': j.status,
                     'snapshot_at': j.snapshot_at.isoformat() if j.snapshot_at else None,
                     'error': j.error, 'manifest': j.manifest,
                     'created_at': j.created_at.isoformat() if j.created_at else None,
                     'finished_at': j.finished_at.isoformat() if j.finished_at else None})
    return rows


def create_backup(db, user):
    code = f"BK-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    job = BackupJob(backup_code=code, status=BACKUP_STATES[0], triggered_by=user.email)
    db.add(job); db.add(Audit(actor=user.email, action=f'发起样本库备份 {code}'))
    db.commit()
    try:
        manifest = _take_snapshot(code)
        job.status = 'completed'; job.manifest = manifest
        job.snapshot_at = datetime.fromisoformat(manifest['snapshot_at'])
        job.finished_at = datetime.now(timezone.utc)
        db.add(Audit(actor=user.email, action=f'备份完成 {code}，快照 {manifest["tables_total"]} 张表'))
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(BackupJob, job.id)
        job.status = 'failed'; job.error = f'快照导出失败：{exc}'
        job.finished_at = datetime.now(timezone.utc)
        db.add(Audit(actor=user.email, action=f'备份失败 {code}：{exc}'))
        db.commit()
    db.refresh(job)
    return job


def _take_snapshot(backup_code):
    target = backup_dir(backup_code)
    os.makedirs(os.path.join(target, 'tmp'), exist_ok=True)
    tables_meta = Base.metadata.tables
    manifest = {'format': 1, 'backup_code': backup_code, 'tables': {}, 'table_order': list(SNAPSHOT_TABLES)}
    overall = hashlib.sha256()
    with engine.connect() as conn:
        if conn.dialect.name == 'postgresql':
            # Fixed snapshot: every subsequent read in this transaction sees the
            # same commit point, even while registrations and trials keep running.
            conn.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
        snapshot_at = conn.execute(text('SELECT CURRENT_TIMESTAMP')).scalar()
        # The read transaction now holds its snapshot (PostgreSQL REPEATABLE READ
        # fixes it at the first query; SQLite WAL fixes it at the first read).
        if SNAPSHOT_STARTED_HOOK is not None:
            SNAPSHOT_STARTED_HOOK()
        manifest['snapshot_at'] = snapshot_at.isoformat() if hasattr(snapshot_at, 'isoformat') else str(snapshot_at)
        for name in SNAPSHOT_TABLES:
            table = tables_meta[name]
            pk_cols = [c for c in table.primary_key.columns]
            result = conn.execute(table.select().order_by(*pk_cols))
            columns = list(result.keys())
            lines = []
            for row in result.mappings():
                lines.append(json.dumps({c: _canonical(row[c]) for c in columns},
                                        sort_keys=True, ensure_ascii=False) + '\n')
            checksum = _hash_lines(lines)
            tmp_file = os.path.join(target, 'tmp', f'{name}.jsonl')
            final_file = os.path.join(target, f'{name}.jsonl')
            with open(tmp_file, 'w', encoding='utf-8') as fh:
                fh.writelines(lines)
            os.replace(tmp_file, final_file)
            manifest['tables'][name] = {'rows': len(lines), 'checksum': checksum, 'columns': columns}
            overall.update(f'{name}:{checksum}\n'.encode('utf-8'))
    manifest['checksum'] = overall.hexdigest()
    manifest['tables_total'] = len(SNAPSHOT_TABLES)
    tmp_manifest = os.path.join(target, 'tmp', 'manifest.json')
    with open(tmp_manifest, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_manifest, os.path.join(target, 'manifest.json'))
    os.rmdir(os.path.join(target, 'tmp'))
    return manifest
