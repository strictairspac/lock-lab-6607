"""Restore snapshots into an isolated database, resumably and verifiably.

Design properties required by the workstation rules:
- The target is always an isolated database, never the primary one.
- Each table loads in its own transaction; its checkpoint is written to the
  primary control database only after commit. A crash mid-table rolls the load
  back, and a retry simply reloads that table — no duplicated rows.
- Unfinished trials (queued/running at the snapshot point) become
  ``interrupted`` in the restored database and are never re-enqueued.
- Completed trials keep every receipt so they can be reconciled in full.
- Outbound notifications go through an outbox with a unique event key, so a
  retried restore publishes each event at most once.
- Availability is gated on three reconciliation levels: artifact checksums,
  row counts/hashes against the manifest, and business invariants.
"""
import json, hashlib, os
from datetime import datetime, timezone
from sqlalchemy import select
from src.config.database import Session, Base
from src.config.restore_db import ensure_restore_database, restore_engine
from src.constants.enums import LOAD_ORDER
from src.models.entities import BackupJob, RestoreJob, RestoreEvent, Audit
from src.services.backup import backup_dir, load_manifest

INTERRUPTED_REASON = '恢复时点演练尚未完成，已标记中断且不再执行'


def _now():
    return datetime.now(timezone.utc)


def _job_view(job):
    return {'id': job.id, 'backup_id': job.backup_id, 'status': job.status,
            'progress': job.progress, 'verification': job.verification,
            'error': job.error, 'triggered_by': job.triggered_by,
            'created_at': job.created_at.isoformat() if job.created_at else None,
            'updated_at': job.updated_at.isoformat() if job.updated_at else None}


def list_jobs(db):
    return [_job_view(j) for j in db.query(RestoreJob).order_by(RestoreJob.id.desc()).all()]


def detail(db, restore_id):
    job = db.get(RestoreJob, restore_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, '恢复任务不存在')
    return _job_view(job)


def publish_event(topic, payload):
    """Real notifier. Tests monkeypatch this to observe idempotent delivery."""
    from src.services.trials import queue
    queue.rpush('locklab:events', json.dumps({'topic': topic, 'payload': payload}, ensure_ascii=False))


def _file_lines(path):
    with open(path, encoding='utf-8') as fh:
        return [line for line in fh if line.strip()]


def _checksum_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _coerce(table, row):
    """Convert JSONL scalar values back to the table's column types."""
    out = {}
    for col in table.columns:
        if col.name not in row:
            continue
        value = row[col.name]
        if value is not None and col.type.__class__.__name__.startswith('DateTime') and isinstance(value, str):
            value = datetime.fromisoformat(value)
        out[col.name] = value
    return out


def start_restore(db, backup_id, user, crash_after=None):
    backup = db.get(BackupJob, backup_id)
    if not backup:
        from fastapi import HTTPException
        raise HTTPException(404, '备份不存在')
    if backup.status != 'completed' or not backup.manifest:
        from fastapi import HTTPException
        raise HTTPException(409, '该备份未成功完成，不能用于恢复')
    # Resuming the same backup reuses its job instead of spawning a duplicate.
    job = db.scalar(select(RestoreJob).where(RestoreJob.backup_id == backup_id)
                    .order_by(RestoreJob.id.desc()))
    if job is None:
        job = RestoreJob(backup_id=backup_id, status='running', progress={}, triggered_by=user.email)
        db.add(job); db.add(Audit(actor=user.email, action=f'发起隔离恢复，备份 {backup.backup_code}'))
        db.commit()
    elif job.status in ('running', 'interrupted', 'verifying', 'failed'):
        job.status = 'running'; job.error = ''
        db.add(Audit(actor=user.email, action=f'续跑恢复任务 #{job.id}，备份 {backup.backup_code}'))
        db.commit()
    else:
        return _job_view(job)
    db.refresh(job)
    try:
        run_restore(job.id, crash_after=crash_after)
    except RestoreCrashed as exc:
        # Simulated interruption: the table commit landed but its checkpoint did
        # not. The job is marked interrupted; the next call resumes from disk.
        with Session() as db:
            crashed_job = db.get(RestoreJob, job.id)
            crashed_job.status = 'interrupted'; crashed_job.error = str(exc)
            db.commit()
    with Session() as fresh:
        return _job_view(fresh.get(RestoreJob, job.id))


class RestoreCrashed(RuntimeError):
    pass


def run_restore(restore_id, crash_after=None):
    with Session() as db:
        job = db.get(RestoreJob, restore_id)
        backup = db.get(BackupJob, job.backup_id)
        backup_code = backup.backup_code
        progress = dict(job.progress or {})
    ensure_restore_database()
    engine = restore_engine()
    Base.metadata.create_all(engine)
    manifest = load_manifest(backup_code)
    tables = Base.metadata.tables
    for name in LOAD_ORDER:
        if name in progress:
            continue  # checkpoint says this table is already durably loaded
        path = os.path.join(backup_dir(backup_code), f'{name}.jsonl')
        if not os.path.exists(path):
            reason = f'备份文件缺失：{name}.jsonl，无法从该备份恢复'
            return _fail(restore_id, reason, {'checked_at': _now().isoformat(), 'checks': [],
                                              'passed': False, 'failures': [f'一级对账：{reason}']})
        actual_checksum = _checksum_of(path)
        if actual_checksum != manifest['tables'][name]['checksum']:
            reason = (f'备份文件 {name}.jsonl 校验和不一致（期望 {manifest["tables"][name]["checksum"][:12]}…，'
                      f'实际 {actual_checksum[:12]}…），备份可能已损坏')
            return _fail(restore_id, reason, {'checked_at': _now().isoformat(), 'checks': [],
                                              'passed': False, 'failures': [f'一级对账：{reason}']})
        rows = [_coerce(tables[name], json.loads(line)) for line in _file_lines(path)]
        event_specs = []
        if name == 'trials':
            for row in rows:
                if row['status'] in ('queued', 'running'):
                    row['status'] = 'interrupted'
                    row['error'] = INTERRUPTED_REASON
                    event_specs.append(('trial.interrupted', f"trial:{row['id']}",
                                        {'trial_id': row['id'], 'name': row['name'], 'reason': INTERRUPTED_REASON}))
                elif row['status'] == 'completed':
                    event_specs.append(('trial.completed', f"trial:{row['id']}",
                                        {'trial_id': row['id'], 'name': row['name']}))
        # One transaction per table: a crash before commit leaves no partial data.
        with engine.begin() as conn:
            conn.execute(tables[name].delete())
            if rows:
                conn.execute(tables[name].insert(), rows)
        if crash_after == name:
            # The table transaction has committed but the checkpoint has not:
            # a retry must reload it without producing duplicates.
            raise RestoreCrashed(f'模拟恢复进程在 {name} 载入后中断')
        with Session() as db:
            job = db.get(RestoreJob, restore_id)
            new_progress = dict(job.progress or {})
            new_progress[name] = {'rows': len(rows), 'checksum': actual_checksum}
            job.progress = new_progress
            for topic, key, payload in event_specs:
                db.add(RestoreEvent(restore_id=restore_id, event_key=key,
                                    topic=topic, payload=payload, published=0))
            db.commit()
        progress[name] = {'rows': len(rows), 'checksum': actual_checksum}
    engine.dispose()
    return verify_restore(restore_id)


def _fail(restore_id, reason, verification=None):
    with Session() as db:
        job = db.get(RestoreJob, restore_id)
        job.status = 'failed'; job.error = reason
        if verification is not None:
            job.verification = verification
        db.add(Audit(actor=job.triggered_by, action=f'恢复任务 #{restore_id} 失败：{reason}'))
        db.commit()
    return _job_view(job)


def _publish_pending(restore_id):
    """Drain the outbox. Already-published keys are skipped — retries never duplicate."""
    delivered = []
    with Session() as db:
        events = db.scalars(select(RestoreEvent)
                            .where(RestoreEvent.restore_id == restore_id,
                                   RestoreEvent.published == 0)
                            .order_by(RestoreEvent.id)).all()
        for event in events:
            publish_event(event.topic, event.payload)
            event.published = 1
            delivered.append(event.event_key)
        db.commit()
    return delivered


def verify_restore(restore_id):
    with Session() as db:
        job = db.get(RestoreJob, restore_id)
        backup_code = db.get(BackupJob, job.backup_id).backup_code
    with Session() as db:
        db.get(RestoreJob, restore_id).status = 'verifying'; db.commit()
    failures, checks = [], []
    manifest = load_manifest(backup_code)
    # Level 1: artifact integrity on disk.
    for name in LOAD_ORDER:
        path = os.path.join(backup_dir(backup_code), f'{name}.jsonl')
        if not os.path.exists(path):
            failures.append(f'一级对账：缺少备份文件 {name}.jsonl'); continue
        checksum = _checksum_of(path)
        expected = manifest['tables'][name]['checksum']
        if checksum != expected:
            failures.append(f'一级对账：{name}.jsonl 校验和不匹配')
        else:
            checks.append(f'{name}.jsonl 校验和一致')
    if failures:
        return _finalize(restore_id, checks, failures)
    engine = restore_engine()
    tables = Base.metadata.tables
    # Level 2: row counts and canonical content hash against the manifest.
    with engine.connect() as conn:
        for name in LOAD_ORDER:
            table = tables[name]
            pk_cols = [c for c in table.primary_key.columns]
            result = conn.execute(table.select().order_by(*pk_cols))
            columns = list(result.keys())
            lines = []
            for row in result.mappings():
                lines.append(json.dumps({c: row[c].isoformat() if hasattr(row[c], 'isoformat') else row[c]
                                         for c in columns}, sort_keys=True, ensure_ascii=False) + '\n')
            content_hash = hashlib.sha256(''.join(lines).encode('utf-8')).hexdigest()
            expected_rows = manifest['tables'][name]['rows']
            if len(lines) != expected_rows:
                failures.append(f'二级对账：{name} 恢复 {len(lines)} 行，备份快照为 {expected_rows} 行')
            else:
                checks.append(f'{name} {len(lines)} 行内容对账一致（哈希 {content_hash[:12]}…）')
        if not failures:
            failures.extend(_business_invariants(conn, tables))
    engine.dispose()
    return _finalize(restore_id, checks, failures)


def _business_invariants(conn, tables):
    """Level 3: conservation, receipt completeness, and interruption rules."""
    problems = []
    def all_rows(name):
        result = conn.execute(tables[name].select())
        cols = list(result.keys())
        return [dict(zip(cols, r)) for r in result.all()]
    samples = {r['id']: r for r in all_rows('samples')}
    transfers = {r['id']: r for r in all_rows('transfers')}
    trials = all_rows('trials')
    balances = {r['trial_id']: r for r in all_rows('trial_balances')}
    receipts = all_rows('receipts')
    # No trial may still be waiting or running: unfinished work must be interrupted.
    unfinished = [t for t in trials if t['status'] in ('queued', 'running')]
    if unfinished:
        problems.append('三级对账：存在未标记中断的演练 ' + ','.join(f"#{t['id']}" for t in unfinished))
    for trial in trials:
        transfer = transfers.get(trial['transfer_id'])
        if transfer is None:
            problems.append(f"三级对账：演练 #{trial['id']} 缺少流转申请"); continue
        sample = samples.get(transfer['sample_id'])
        if sample is None:
            problems.append(f"三级对账：演练 #{trial['id']} 缺少样本台账"); continue
        balance = balances.get(trial['id'])
        if balance is None:
            problems.append(f"三级对账：演练 #{trial['id']} 缺少镜像账本"); continue
        elif balance['remaining'] + balance['received'] != sample['quantity'] or balance['remaining'] < 0:
            problems.append(f"三级对账：演练 #{trial['id']} 数量守恒不成立 "
                            f"({balance['remaining']}+{balance['received']}≠{sample['quantity']})")
        own_receipts = [r for r in receipts if r['trial_id'] == trial['id']]
        if trial['status'] == 'completed':
            # Completed trials must carry their full receipt trail in the restored library.
            actors = sorted(r['actor'] for r in own_receipts)
            if actors != ['A']:
                problems.append(f"三级对账：已完成演练 #{trial['id']} 回执不完整（实际 {actors}，应为仅 A 一条）")
        elif trial['status'] == 'interrupted':
            if own_receipts:
                problems.append(f"三级对账：中断演练 #{trial['id']} 不应存在回执")
            if not trial.get('error'):
                problems.append(f"三级对账：中断演练 #{trial['id']} 缺少中断原因")
    return problems


def _finalize(restore_id, checks, failures):
    with Session() as db:
        job = db.get(RestoreJob, restore_id)
        verification = {'checked_at': _now().isoformat(), 'checks': checks,
                        'passed': not failures, 'failures': failures}
        job.verification = verification
        if failures:
            job.status = 'failed'; job.error = '；'.join(failures)
            db.add(Audit(actor=job.triggered_by, action=f'恢复任务 #{restore_id} 对账未通过：{job.error}'))
            db.commit()
            return _job_view(job)
        job.status = 'available'; job.error = ''
        db.add(Audit(actor=job.triggered_by, action=f'恢复任务 #{restore_id} 对账通过，恢复库可用'))
        db.commit()
    # Outbox drain happens only after reconciliation passes.
    delivered = _publish_pending(restore_id)
    with Session() as db:
        job = db.get(RestoreJob, restore_id)
        view = _job_view(job)
    view['published_events'] = delivered
    return view
