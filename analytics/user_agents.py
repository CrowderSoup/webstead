from typing import Any, Optional

import requests

API_URL = "https://api.apicagent.com"
REQUEST_TIMEOUT_SECONDS = 2


def _fetch_user_agent_details(user_agent: str) -> Optional[dict[str, Any]]:
    if not user_agent:
        return None

    try:
        response = requests.get(
            API_URL,
            params={"ua": user_agent},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def enqueue_user_agent_lookup(visit_id: int, user_agent: str) -> None:
    """Fire-and-forget user agent lookup to avoid blocking responses."""
    if not visit_id or not user_agent:
        return

    from analytics.tasks import lookup_user_agent

    lookup_user_agent.delay(visit_id, user_agent)
