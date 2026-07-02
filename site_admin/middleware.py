import json

from django.contrib.messages import get_messages


def _normalize_trigger_header(raw_value):
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        events = [event.strip() for event in raw_value.split(",") if event.strip()]
        return {event: True for event in events}
    if isinstance(parsed, dict):
        return parsed
    return {}


class SiteAdminHtmxMessagesMiddleware:
    """Send Django messages to HTMX responses as HX-Trigger events."""

    EVENT_NAME = "site_admin:messages"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.headers.get("HX-Request") != "true":
            return response

        match = getattr(request, "resolver_match", None)
        if not match or match.namespace != "site_admin":
            return response

        # Preserve queued messages for the final redirected page.
        if 300 <= response.status_code < 400:
            return response

        messages_payload = []
        for message in get_messages(request):
            messages_payload.append(
                {
                    "level": message.level_tag or "info",
                    "text": str(message),
                }
            )

        if not messages_payload:
            return response

        triggers = _normalize_trigger_header(response.headers.get("HX-Trigger"))
        triggers[self.EVENT_NAME] = messages_payload
        response.headers["HX-Trigger"] = json.dumps(triggers)
        return response
