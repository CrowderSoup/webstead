from celery import shared_task


@shared_task
def reconcile_themes() -> None:
    from core.apps import _run_startup_reconcile

    _run_startup_reconcile()


@shared_task
def sync_themes() -> None:
    from core.apps import _run_startup_sync

    _run_startup_sync()
