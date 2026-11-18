import os
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+psycopg://locklab:locklab_demo_pw@localhost:17742/locklab')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.getenv('JWT_SECRET', 'local-lock-lab-change-before-public-use')
# Backup artifacts (JSONL snapshots, manifests, restore progress).
BACKUP_DIR = os.getenv('BACKUP_DIR', '/artifacts/backups')
# Isolated restore target. When unset it is derived from DATABASE_URL:
# PostgreSQL -> a sibling database named locklab_restore on the same server;
# SQLite -> a separate file next to the primary database file.
RESTORE_DATABASE_URL = os.getenv('RESTORE_DATABASE_URL', '')
# Lightweight periodic snapshot schedule in minutes (0 disables the loop).
BACKUP_INTERVAL_MINUTES = int(os.getenv('BACKUP_INTERVAL_MINUTES', '0'))
