from fastapi import APIRouter, Depends
from src.config.database import db_session
from src.utils.security import admin
from src.services import backup as backup_service
from src.services import restore as restore_service

router = APIRouter()


@router.get('/backups')
def backups(db=Depends(db_session), user=Depends(admin)):
    return backup_service.list_jobs(db)


@router.post('/backups', status_code=201)
def create_backup(db=Depends(db_session), user=Depends(admin)):
    job = backup_service.create_backup(db, user)
    return backup_service.list_jobs(db)[0] if job else None


@router.get('/restores')
def restores(db=Depends(db_session), user=Depends(admin)):
    return restore_service.list_jobs(db)


@router.post('/backups/{backup_id}/restore', status_code=201)
def start_restore(backup_id: int, db=Depends(db_session), user=Depends(admin)):
    return restore_service.start_restore(db, backup_id, user)


@router.get('/restores/{restore_id}')
def restore_detail(restore_id: int, db=Depends(db_session), user=Depends(admin)):
    return restore_service.detail(db, restore_id)


@router.post('/restores/{restore_id}/verify')
def verify(restore_id: int, user=Depends(admin)):
    return restore_service.verify_restore(restore_id)
