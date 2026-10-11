"""Scheduled maintenance for the MCP audit log."""
from datetime import timedelta

from celery import shared_task

# Every call is logged. Drop rows older than this so the table stays small.
RETENTION_DAYS = 90


@shared_task
def prune_mcp_request_logs() -> int:
    """Delete MCP request logs older than RETENTION_DAYS.

    Scheduled daily by Celery Beat, same pattern as publish_due_posts.
    Models are imported inside the task: Celery loads this module before
    Django apps are ready.
    """
    from django.utils import timezone

    from mcp_server.models import McpRequestLog

    cutoff = timezone.now() - timedelta(days=RETENTION_DAYS)
    deleted, _ = McpRequestLog.objects.filter(created_at__lt=cutoff).delete()
    return deleted
