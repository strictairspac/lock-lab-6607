ROLES = ('admin', 'operator', 'viewer')
RUN_STATES = ('queued', 'running', 'completed', 'failed', 'interrupted')
# Backup lifecycle.
BACKUP_STATES = ('running', 'completed', 'failed')
# Restore lifecycle; verification gates availability.
RESTORE_STATES = ('running', 'interrupted', 'verifying', 'available', 'failed')
# Business tables captured in one snapshot transaction, in FK order.
SNAPSHOT_TABLES = ('users', 'samples', 'transfers', 'trials', 'trial_balances', 'receipts', 'audit_logs')
# Tables rebuilt in the isolated restore database, in dependency order.
LOAD_ORDER = ('users', 'samples', 'transfers', 'trials', 'trial_balances', 'receipts', 'audit_logs')
