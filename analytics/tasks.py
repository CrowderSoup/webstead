from typing import Optional

from celery import shared_task


@shared_task
def lookup_user_agent(visit_id: int, user_agent: str) -> None:
    from django.db import close_old_connections

    from analytics.models import Visit
    from analytics.user_agents import _fetch_user_agent_details

    close_old_connections()
    try:
        details = _fetch_user_agent_details(user_agent)
        if details:
            Visit.objects.filter(id=visit_id).update(user_agent_details=details)
    finally:
        close_old_connections()


@shared_task
def record_visit(
    session_key: Optional[str],
    user_id: Optional[int],
    ip: Optional[str],
    user_agent: str,
    path: str,
    referrer: str,
    duration: int,
    response_status_code: int,
) -> None:
    """
    Does the slow part of AnalyticsMiddleware (geolocation HTTP call + DB
    writes) off the request/response cycle, so a page response never waits
    on a third-party API. See analytics/middleware.py.

    Imports are deferred: celery's autodiscover_tasks() imports this module
    before the Django app registry is ready, so a module-level model import
    would raise AppRegistryNotReady.
    """
    from django.db import close_old_connections

    from analytics.bot_detection import should_flag_user_agent
    from analytics.models import UserAgentIgnore, Visit
    from analytics.user_agents import enqueue_user_agent_lookup
    from analytics.utils import geolocate_ip

    close_old_connections()
    try:
        if UserAgentIgnore.objects.filter(user_agent=user_agent).exists():
            return

        is_suspected_bot, pattern_version = should_flag_user_agent(user_agent)
        geo = geolocate_ip(ip) if ip else {}

        visit = Visit.objects.create(
            session_key=session_key,
            user_id=user_id,
            ip_address=ip,
            user_agent=user_agent,
            path=path,
            referrer=referrer,
            duration_seconds=duration,
            country=geo.get("country", ""),
            region=geo.get("region", ""),
            city=geo.get("city", ""),
            response_status_code=response_status_code,
            is_suspected_bot=is_suspected_bot,
            suspected_bot_pattern_version=pattern_version,
        )

        enqueue_user_agent_lookup(visit.id, visit.user_agent)
    finally:
        close_old_connections()
