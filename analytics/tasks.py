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
