"""HTTP-level checks: admin-only backup workflow, RBAC, and the verify endpoint."""
from fastapi.testclient import TestClient

from src.main import app
from src.config.database import Session
from src.models.entities import User
from src.utils.security import password_hash
from tests import factory


def _token(client, email, role):
    with Session() as db:
        if not db.query(User).filter_by(email=email).first():
            db.add(User(email=email, password=password_hash('Demo2026!'), role=role))
            db.commit()
    res = client.post('/auth/login', json={'email': email, 'password': 'Demo2026!'})
    assert res.status_code == 200, res.text
    return res.json()['token']


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def test_full_backup_restore_verify_flow_over_http(env):
    with TestClient(app) as client:
        admin = _token(client, 'admin@qingyu.test', 'admin')
        factory.make_sample(quantity=120)
        transfer_id = factory.make_transfer(1, quantity=30)
        factory.make_completed_trial(transfer_id, initial=120)

        created = client.post('/backups', headers=_auth(admin))
        assert created.status_code == 201
        backup = created.json()
        assert backup['status'] == 'completed'
        assert backup['manifest']['tables_total'] == 7

        listing = client.get('/backups', headers=_auth(admin))
        assert listing.status_code == 200 and len(listing.json()) == 1

        restored = client.post(f"/backups/{backup['id']}/restore", headers=_auth(admin))
        assert restored.status_code == 201
        job = restored.json()
        assert job['status'] == 'available'
        assert job['verification']['passed'] is True
        assert job['verification']['failures'] == []

        reverify = client.post(f"/restores/{job['id']}/verify", headers=_auth(admin))
        assert reverify.status_code == 200
        assert reverify.json()['verification']['passed'] is True

        detail = client.get(f"/restores/{job['id']}", headers=_auth(admin))
        assert detail.status_code == 200
        assert detail.json()['progress']['trials']['rows'] == 1


def test_backup_routes_are_admin_only(env):
    with TestClient(app) as client:
        viewer = _token(client, 'viewer@qingyu.test', 'viewer')
        operator = _token(client, 'operator@qingyu.test', 'operator')
        assert client.post('/backups', headers=_auth(viewer)).status_code == 403
        assert client.get('/backups', headers=_auth(operator)).status_code == 403
        assert client.post('/backups/1/restore', headers=_auth(viewer)).status_code == 403
        assert client.post('/restores/1/verify', headers=_auth(operator)).status_code == 403
        # Unauthenticated requests are rejected too.
        assert client.get('/backups').status_code == 403 or client.get('/backups').status_code == 401


def test_corrupt_backup_surface_failure_reason_over_http(env):
    with TestClient(app) as client:
        admin = _token(client, 'admin@qingyu.test', 'admin')
        sample_id = factory.make_sample(quantity=60)
        transfer_id = factory.make_transfer(sample_id, quantity=10)
        factory.make_completed_trial(transfer_id, initial=60)
        backup = client.post('/backups', headers=_auth(admin)).json()

        target = env.backup_dir / backup['backup_code'] / 'transfers.jsonl'
        target.write_text(target.read_text(encoding='utf-8') + '{"id": 999}\n', encoding='utf-8')

        res = client.post(f"/backups/{backup['id']}/restore", headers=_auth(admin))
        assert res.status_code == 201
        body = res.json()
        assert body['status'] == 'failed'
        assert 'transfers.jsonl' in body['error']
        assert body['verification']['passed'] is False
        assert body['verification']['failures']
