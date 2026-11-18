"""End-to-end backup/restore scenarios requested by the workstation team.

Scenario A: a trial finishes while a backup snapshot is running; registrations
keep happening concurrently. The backup must capture one commit point and the
restored isolated library must reconcile against exactly that point.

Scenario B: the restore process crashes halfway (after a table commit but
before its checkpoint). Resuming the same backup must reload without
duplicates, never publish an event twice, and only become available after
three-level reconciliation passes.
"""
import json, threading
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.config.database import Session
from src.config import restore_db
from src.models.entities import Sample, Transfer, Trial, TrialBalance, Receipt, RestoreJob, RestoreEvent
from src.services import backup as backup_service
from src.services import restore as restore_service
from tests import factory


ADMIN = factory._User('admin@qingyu.test')


def _restore_session():
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(restore_db.restore_engine())()


# ---------------------------------------------------------------- scenario A
def test_backup_during_concurrent_trial_and_registration_captures_one_commit_point(env):
    factory.ensure_users()
    sample_id = factory.make_sample(quantity=120)
    transfer_id = factory.make_transfer(sample_id, quantity=30)
    unfinished_id = factory.make_unfinished_trial(transfer_id, 'queued', '排队演练')

    snapshot_open = threading.Event()
    writes_done = threading.Event()

    def concurrent_writer():
        # Block until the backup read transaction has established its snapshot.
        assert snapshot_open.wait(10)
        with Session() as db:
            trial = db.get(Trial, unfinished_id)
            trial.status = 'completed'
            trial.result = {'initial': 120, 'remaining': 90, 'received': 30,
                            'receipt_count': 1, 'invariant_passed': True, 'traces': []}
            balance = db.scalar(select(TrialBalance).where(TrialBalance.trial_id == unfinished_id))
            balance.remaining, balance.received = 90, 30
            db.add(Receipt(trial_id=unfinished_id, actor='A', quantity=30))
            db.add(Sample(code='QY-CONCURRENT', title='备份期间登记的样本', quantity=50, location='柜 C-01'))
            db.commit()
        writes_done.set()

    writer_thread = threading.Thread(target=concurrent_writer)
    writer_thread.start()

    backup_service.SNAPSHOT_STARTED_HOOK = snapshot_open.set
    try:
        with Session() as db:
            job = backup_service.create_backup(db, ADMIN)
    finally:
        backup_service.SNAPSHOT_STARTED_HOOK = None
    writer_thread.join(10)
    assert writes_done.is_set()

    assert job.status == 'completed'
    manifest = job.manifest
    # Snapshot was taken before the competing commit: queued trial, one sample.
    assert manifest['tables']['trials']['rows'] == 1
    assert manifest['tables']['samples']['rows'] == 1
    trials_file = env.backup_dir / job.backup_code / 'trials.jsonl'
    snap = [json.loads(line) for line in trials_file.read_text(encoding='utf-8').splitlines()]
    assert snap[0]['status'] == 'queued'

    # Primary kept moving and now holds the post-backup commit.
    with Session() as db:
        assert db.get(Trial, unfinished_id).status == 'completed'
        assert db.scalar(select(Sample).where(Sample.code == 'QY-CONCURRENT')) is not None

    # Restore lands on the isolated database at the backup commit point.
    with Session() as db:
        view = restore_service.start_restore(db, job.id, ADMIN)
    assert view['status'] == 'available', view['error']

    RSession = sessionmaker(restore_db.restore_engine())
    with RSession() as rdb:
        restored_trials = rdb.scalars(select(Trial)).all()
        assert len(restored_trials) == 1
        # The unfinished trial at snapshot time is interrupted, never re-enqueued.
        assert restored_trials[0].status == 'interrupted'
        assert '中断' in restored_trials[0].error
        assert rdb.scalars(select(Receipt)).all() == []
        # The sample registered during the backup is not in the restored point.
        assert rdb.scalar(select(Sample).where(Sample.code == 'QY-CONCURRENT')) is None
        assert len(rdb.scalars(select(Sample)).all()) == 1

    # Outbox: interrupted-trial event published exactly once.
    with Session() as db:
        events = db.scalars(select(RestoreEvent)).all()
        assert [(e.event_key, e.published) for e in events] == [(f'trial:{unfinished_id}', 1)]
    published = [json.loads(x) for x in env.redis.lists.get('locklab:events', [])]
    assert [p['topic'] for p in published] == ['trial.interrupted']

    # Primary database untouched by the restore.
    with Session() as db:
        assert db.get(Trial, unfinished_id).status == 'completed'


def test_completed_trial_restores_with_full_receipts(env):
    factory.ensure_users()
    sample_id = factory.make_sample(quantity=120)
    transfer_id = factory.make_transfer(sample_id, quantity=30)
    factory.make_completed_trial(transfer_id, initial=120)
    with Session() as db:
        backup = backup_service.create_backup(db, ADMIN)
        view = restore_service.start_restore(db, backup.id, ADMIN)
    assert view['status'] == 'available'
    from sqlalchemy.orm import sessionmaker
    RSession = sessionmaker(restore_db.restore_engine())
    with RSession() as rdb:
        trial = rdb.scalar(select(Trial))
        assert trial.status == 'completed'
        receipts = rdb.scalars(select(Receipt)).all()
        assert len(receipts) == 1 and receipts[0].actor == 'A'
        balance = rdb.scalar(select(TrialBalance))
        assert balance.remaining + balance.received == 120
    with Session() as db:
        topics = [e.topic for e in db.scalars(select(RestoreEvent)).all()]
    assert topics == ['trial.completed']


# ---------------------------------------------------------------- scenario B
def test_restore_crash_resumes_without_duplicate_publish(env):
    factory.ensure_users()
    s1 = factory.make_sample('QY-R-01', 100)
    s2 = factory.make_sample('QY-R-02', 80)
    t1 = factory.make_transfer(s1, 'MOVE-R-01', 20)
    t2 = factory.make_transfer(s2, 'MOVE-R-02', 10)
    completed_id = factory.make_completed_trial(t1, 100, name='已完成演练')
    queued_id = factory.make_unfinished_trial(t2, 'running', name='执行中演练')

    with Session() as db:
        backup = backup_service.create_backup(db, ADMIN)
    assert backup.status == 'completed'

    # First attempt crashes after the samples table commits, before checkpoint.
    with Session() as db:
        view = restore_service.start_restore(db, backup.id, ADMIN, crash_after='samples')
    with Session() as db:
        crashed = db.get(RestoreJob, view['id'])
        assert crashed.status == 'interrupted'  # process died mid-table load
        # users committed and checkpointed before the crash; the samples commit
        # landed in the isolated DB but its checkpoint did not.
        assert set(crashed.progress) == {'users'}

    # The isolated DB may contain the committed samples, but resume must not duplicate.
    from sqlalchemy.orm import sessionmaker
    RSession = sessionmaker(restore_db.restore_engine())
    with RSession() as rdb:
        assert len(rdb.scalars(select(Sample)).all()) == 2
    # No events may have leaked before reconciliation.
    assert env.redis.lists.get('locklab:events', []) == []

    # Simulate a process restart: resume the SAME backup.
    with Session() as db:
        resumed = restore_service.start_restore(db, backup.id, ADMIN)
    assert resumed['status'] == 'available', resumed['error']

    with RSession() as rdb:
        assert len(rdb.scalars(select(Sample)).all()) == 2          # no duplicated rows
        assert len(rdb.scalars(select(Transfer)).all()) == 2
        statuses = {t.id: t.status for t in rdb.scalars(select(Trial)).all()}
        assert statuses[completed_id] == 'completed'
        assert statuses[queued_id] == 'interrupted'
        # Completed trial keeps its receipt; interrupted one has none.
        receipts = rdb.scalars(select(Receipt)).all()
        assert [(r.trial_id, r.actor) for r in receipts] == [(completed_id, 'A')]
        balances = {b.trial_id: b for b in rdb.scalars(select(TrialBalance)).all()}
        assert balances[completed_id].remaining + balances[completed_id].received == 100

    # Outbox drained once; a second resume must not publish anything again.
    first_publish = [json.loads(x) for x in env.redis.lists.get('locklab:events', [])]
    assert sorted(p['topic'] for p in first_publish) == ['trial.completed', 'trial.interrupted']
    with Session() as db:
        assert all(e.published == 1 for e in db.scalars(select(RestoreEvent)).all())

    with Session() as db:
        again = restore_service.start_restore(db, backup.id, ADMIN)
    assert again['status'] == 'available'
    second_publish = [json.loads(x) for x in env.redis.lists.get('locklab:events', [])]
    assert len(second_publish) == len(first_publish)  # retry never re-publishes


def test_verification_reports_concrete_failure_reason(env, monkeypatch):
    factory.ensure_users()
    sample_id = factory.make_sample(quantity=120)
    transfer_id = factory.make_transfer(sample_id, quantity=30)
    factory.make_completed_trial(transfer_id, initial=120)
    with Session() as db:
        backup = backup_service.create_backup(db, ADMIN)

    # Corrupt a backup artifact after the backup completed.
    target = env.backup_dir / backup.backup_code / 'samples.jsonl'
    target.write_text(target.read_text(encoding='utf-8') + '{"tampered": true}\n', encoding='utf-8')

    with Session() as db:
        view = restore_service.start_restore(db, backup.id, ADMIN)
    assert view['status'] == 'failed'
    assert 'samples.jsonl' in view['error'] and '校验和' in view['error']
    # Nothing is published while verification fails.
    assert env.redis.lists.get('locklab:events', []) == []


def test_manual_reverify_available_restore_stays_available(env):
    factory.ensure_users()
    sample_id = factory.make_sample(quantity=60)
    transfer_id = factory.make_transfer(sample_id, quantity=15)
    factory.make_completed_trial(transfer_id, initial=60)
    with Session() as db:
        backup = backup_service.create_backup(db, ADMIN)
        view = restore_service.start_restore(db, backup.id, ADMIN)
    assert view['status'] == 'available'
    again = restore_service.verify_restore(view['id'])
    assert again['status'] == 'available'
    assert again['verification']['passed'] is True
    assert len(again['verification']['checks']) >= 3


def test_empty_database_backup_restores_and_passes_reconciliation(env):
    factory.ensure_users()
    with Session() as db:
        backup = backup_service.create_backup(db, ADMIN)
        assert backup.status == 'completed'
        rows_by_table = {name: meta['rows'] for name, meta in backup.manifest['tables'].items()}
        # No business data yet; only users and the backup audit trail are present.
        assert rows_by_table['users'] == 2
        assert all(rows_by_table[name] == 0
                   for name in ('samples', 'transfers', 'trials', 'trial_balances', 'receipts'))
        view = restore_service.start_restore(db, backup.id, ADMIN)
    assert view['status'] == 'available'
    assert view['verification']['failures'] == []
    assert env.redis.lists.get('locklab:events', []) == []  # no trial events
