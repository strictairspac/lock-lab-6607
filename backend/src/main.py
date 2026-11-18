import os,logging,threading
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text
from src.config.database import Base,engine
from src.config.settings import BACKUP_DIR,BACKUP_INTERVAL_MINUTES
from src.models import entities  # noqa: F401  registers tables on Base.metadata
from src.controllers.auth import router as auth
from src.routes import samples,transfers,trials,audit,backups
from src.middlewares.errors import ErrorMiddleware
from src.middlewares.request_id import RequestIdMiddleware

_scheduler_stop=threading.Event()
def _backup_scheduler():
    """Periodic snapshot confirmation. Each tick is independent; failures are
    recorded on the backup job and never stop the loop."""
    interval=BACKUP_INTERVAL_MINUTES*60
    if interval<=0: return
    from src.config.database import Session
    from src.services.backup import create_backup
    class _SystemUser: email='scheduler@qingyu.test'
    while not _scheduler_stop.wait(interval):
        try:
            with Session() as db: create_backup(db,_SystemUser())
        except Exception: logging.exception('scheduled backup failed')

@asynccontextmanager
async def lifespan(app):
    os.makedirs(BACKUP_DIR,exist_ok=True)
    Base.metadata.create_all(engine)
    if os.getenv('SEED_DEMO','true')=='true':
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception: logging.exception('seed failed; manual retry is available')
    thread=threading.Thread(target=_backup_scheduler,name='backup-scheduler',daemon=True)
    thread.start()
    yield
    _scheduler_stop.set()
app=FastAPI(title='实验样本流转并发台',lifespan=lifespan)
app.add_middleware(ErrorMiddleware);app.add_middleware(RequestIdMiddleware)
for r in (auth,samples.router,transfers.router,trials.router,audit.router,backups.router): app.include_router(r)
@app.get('/health')
def health():
    with engine.connect() as c: c.execute(text('SELECT 1'))
    from src.services.trials import queue
    queue.ping()
    return {'status':'healthy'}
