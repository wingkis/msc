"""Background workers."""

from threading import Thread


def start_workers():
    """Start all background workers in daemon threads."""
    from msc.config import get_settings
    from msc.workers.sync_worker import start_sync_worker
    from msc.workers.backup_worker import start_backup_worker
    from msc.workers.summarize_worker import start_summarize_worker
    from msc.workers.maintenance_worker import start_maintenance_worker

    sync_thread = Thread(target=start_sync_worker, daemon=True)
    sync_thread.start()

    backup_thread = Thread(target=start_backup_worker, daemon=True)
    backup_thread.start()

    digest_thread = Thread(target=start_summarize_worker, daemon=True)
    digest_thread.start()

    # Time decay: no feature flag of its own — MSC_STALE_AFTER_DAYS=0 disables
    # it inside the loop, which keeps "off" meaning one thing in one place.
    maintenance_thread = Thread(target=start_maintenance_worker, daemon=True)
    maintenance_thread.start()

    if get_settings().rag_enabled:
        from msc.workers.rag_worker import start_rag_worker
        rag_thread = Thread(target=start_rag_worker, daemon=True)
        rag_thread.start()

    if get_settings().pic_enabled:
        from msc.workers.pic_worker import start_pic_worker
        pic_thread = Thread(target=start_pic_worker, daemon=True)
        pic_thread.start()
