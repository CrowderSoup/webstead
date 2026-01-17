from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from django.conf import settings


MAX_THEME_ERROR_LENGTH = 500


def truncate_error(message: str, *, max_length: int = MAX_THEME_ERROR_LENGTH) -> str:
    if not message:
        return ""
    if len(message) <= max_length:
        return message
    return message[: max_length - 3].rstrip() + "..."


def duration_ms(start_time: float) -> int:
    elapsed = time.monotonic() - start_time
    return max(0, int(elapsed * 1000))


def log_theme_operation(
    logger: logging.Logger,
    *,
    theme_slug: str,
    operation: str,
    source_type: str,
    ref: str,
    status: str,
    duration_ms_value: int,
    detail: str = "",
    error: str = "",
    dry_run: bool = False,
    emit_metrics: bool = True,
) -> None:
    fields = {
        "theme_slug": theme_slug or "-",
        "operation": operation or "-",
        "source_type": source_type or "-",
        "ref": ref or "-",
        "status": status or "-",
        "duration_ms": duration_ms_value,
    }
    extras = {
        "detail": detail or "",
        "error": error or "",
        "dry_run": dry_run,
    }
    message = _format_theme_log(fields, extras)
    level = logging.WARNING if str(status).lower() == "failed" else logging.INFO
    logger.log(level, message)
    if emit_metrics and not dry_run:
        _record_theme_metrics(status, duration_ms_value)


def _format_theme_log(fields: dict[str, Any], extras: dict[str, Any]) -> str:
    ordered_keys = ["theme_slug", "operation", "source_type", "ref", "status", "duration_ms"]
    segments = ["theme_sync"]
    for key in ordered_keys:
        segments.append(f"{key}={json.dumps(fields.get(key))}")
    for key, value in extras.items():
        if value is None or value == "" or value is False:
            continue
        segments.append(f"{key}={json.dumps(value)}")
    return " ".join(segments)


def _record_theme_metrics(status: str, duration_ms_value: int) -> None:
    client = _metrics_client()
    if not client:
        return
    metric_status = str(status).lower()
    if metric_status == "success":
        _increment_metric(client, "theme_sync_success_total", 1)
    elif metric_status == "failed":
        _increment_metric(client, "theme_sync_failure_total", 1)
    _timing_metric(client, "theme_sync_duration_ms", duration_ms_value)


def _metrics_client() -> Optional[Any]:
    try:
        return getattr(settings, "METRICS_CLIENT", None) or getattr(settings, "METRICS", None)
    except Exception:
        return None


def _increment_metric(client: Any, name: str, value: int) -> None:
    try:
        if hasattr(client, "incr"):
            client.incr(name, value)
        elif hasattr(client, "increment"):
            client.increment(name, value)
    except Exception:
        return


def _timing_metric(client: Any, name: str, value: int) -> None:
    try:
        if hasattr(client, "timing"):
            client.timing(name, value)
        elif hasattr(client, "observe"):
            client.observe(name, value)
    except Exception:
        return
