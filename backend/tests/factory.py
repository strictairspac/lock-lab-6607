"""Helpers that build realistic sample/transfer/trial data through the ORM."""
from src.config.database import Session
from src.models.entities import Sample, Transfer, Trial, TrialBalance, Receipt, User
from src.utils.security import password_hash


def ensure_users():
    with Session() as db:
        for idx, (role, email) in enumerate((('admin', 'admin@qingyu.test'),
                                             ('operator', 'operator@qingyu.test')), start=1):
            if not db.get(User, idx):
                db.add(User(id=idx, email=email,
                            password=password_hash('Demo2026!'), role=role))
        db.commit()


class _User:
    def __init__(self, email):
        self.email = email


def make_sample(code='QY-T-01', quantity=120):
    with Session() as db:
        sample = Sample(code=code, title='测试样本 ' + code, quantity=quantity, location='冷藏间 T-01')
        db.add(sample); db.commit()
        return sample.id


def make_transfer(sample_id, code='MOVE-T-01', quantity=30, destination='理化检测室'):
    with Session() as db:
        transfer = Transfer(code=code, sample_id=sample_id, destination=destination,
                            quantity=quantity, note='测试申请')
        db.add(transfer); db.commit()
        return transfer.id


def make_completed_trial(transfer_id, initial, name='完成演练'):
    """A completed trial mirroring the worker's post-execute shape: one A receipt,
    conservation balance remaining+received=initial."""
    with Session() as db:
        transfer = db.get(Transfer, transfer_id)
        trial = Trial(name=name, transfer_id=transfer_id, timeout_ms=250, status='completed',
                      result={'sample': 'QY-T-01', 'initial': initial,
                              'remaining': initial - transfer.quantity,
                              'received': transfer.quantity, 'receipt_count': 1,
                              'invariant_passed': True, 'traces': []})
        db.add(trial); db.flush()
        db.add(TrialBalance(trial_id=trial.id, remaining=initial - transfer.quantity,
                            received=transfer.quantity))
        db.add(Receipt(trial_id=trial.id, actor='A', quantity=transfer.quantity))
        db.commit()
        return trial.id


def make_unfinished_trial(transfer_id, status, name):
    with Session() as db:
        transfer = db.get(Transfer, transfer_id)
        sample = db.get(Sample, transfer.sample_id)
        trial = Trial(name=name, transfer_id=transfer_id, timeout_ms=250, status=status)
        db.add(trial); db.flush()
        db.add(TrialBalance(trial_id=trial.id, remaining=sample.quantity, received=0))
        db.commit()
        return trial.id


def rows(engine, table):
    from sqlalchemy import select
    from src.config.database import Base
    with engine.connect() as conn:
        result = conn.execute(select(Base.metadata.tables[table]).order_by('id'))
        cols = list(result.keys())
        return [dict(zip(cols, r)) for r in result.all()]
