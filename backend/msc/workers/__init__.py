"""Background workers."""

from threading import Thread


def start_workers():
    """Start all background workers in daemon threads."""
    from msc.workers.sync_worker import start_sync_worker
    from msc.workers.backup_worker import start_backup_worker

    sync_thread = Thread(target=start_sync_worker, daemon=True)
    sync_thread.start()

    backup_thread = Thread(target=start_backup_worker, daemon=True)
    backup_thread.start()
