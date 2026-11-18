from sqlalchemy import Column, Integer, String, JSON, DateTime, ForeignKey, Text, UniqueConstraint
from datetime import datetime, timezone
from src.config.database import Base
def utcnow(): return datetime.now(timezone.utc)
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False)
    password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Sample(Base):
    __tablename__='samples'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    title=Column(String(160),nullable=False);quantity=Column(Integer,nullable=False);location=Column(String(120),nullable=False)
class Transfer(Base):
    __tablename__='transfers'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False)
    sample_id=Column(Integer,ForeignKey('samples.id'),nullable=False);destination=Column(String(120),nullable=False)
    quantity=Column(Integer,nullable=False);note=Column(String(300),default='')
class Trial(Base):
    __tablename__='trials'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False)
    transfer_id=Column(Integer,ForeignKey('transfers.id'),nullable=False);timeout_ms=Column(Integer,nullable=False)
    status=Column(String(20),default='queued',nullable=False);result=Column(JSON,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc));error=Column(Text,default='')
class TrialBalance(Base):
    __tablename__='trial_balances'
    trial_id=Column(Integer,ForeignKey('trials.id'),primary_key=True);remaining=Column(Integer,nullable=False);received=Column(Integer,nullable=False,default=0)
class Receipt(Base):
    __tablename__='receipts'
    id=Column(Integer,primary_key=True);trial_id=Column(Integer,ForeignKey('trials.id'),nullable=False)
    actor=Column(String(20),nullable=False);quantity=Column(Integer,nullable=False)
    __table_args__=(UniqueConstraint('trial_id','actor'),)
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow)
class BackupJob(Base):
    __tablename__='backup_jobs'
    id=Column(Integer,primary_key=True)
    # Directory name under BACKUP_DIR; natural key so retries reuse the same snapshot location.
    backup_code=Column(String(80),unique=True,nullable=False)
    status=Column(String(20),default='running',nullable=False)
    snapshot_at=Column(DateTime(timezone=True),nullable=True)
    manifest=Column(JSON,nullable=True);error=Column(Text,default='')
    triggered_by=Column(String(120),nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow);finished_at=Column(DateTime(timezone=True),nullable=True)
class RestoreJob(Base):
    __tablename__='restore_jobs'
    id=Column(Integer,primary_key=True)
    backup_id=Column(Integer,ForeignKey('backup_jobs.id'),nullable=False)
    status=Column(String(20),default='running',nullable=False)
    # Per-table load progress: {'samples': {'done': 12, 'checksum': '...'}, ...}
    progress=Column(JSON,nullable=False,default=dict)
    verification=Column(JSON,nullable=True);error=Column(Text,default='')
    triggered_by=Column(String(120),nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow);updated_at=Column(DateTime(timezone=True),default=utcnow,onupdate=utcnow)
class RestoreEvent(Base):
    """Idempotency outbox: a restored event is published at most once across retries."""
    __tablename__='restore_events'
    id=Column(Integer,primary_key=True)
    restore_id=Column(Integer,ForeignKey('restore_jobs.id'),nullable=False)
    event_key=Column(String(160),nullable=False);topic=Column(String(80),nullable=False)
    payload=Column(JSON,nullable=False);published=Column(Integer,default=0,nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow)
    __table_args__=(UniqueConstraint('restore_id','event_key'),)
