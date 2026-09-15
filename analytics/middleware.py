import time

from django.db import DatabaseError, transaction
from django.utils.deprecation import MiddlewareMixin

from .utils import get_client_ip  # you write these

# Paths that shouldn't pay for visit tracking: the Django admin, and
# machine-to-machine endpoints (webhooks) that other services call under a
# tight response-time budget.
EXCLUDED_PATH_PREFIXES = ("/admin", "/strava/webhook")


class AnalyticsMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.path.startswith(EXCLUDED_PATH_PREFIXES):
            return
        request._analytics_start_ts = time.time()

    def process_response(self, request, response):
        try:
            if request.path.startswith(EXCLUDED_PATH_PREFIXES):
                return response

            started_ts = getattr(request, "_analytics_start_ts", None)
            if started_ts is None:
                return response

            duration = int(time.time() - started_ts)

            session_key = getattr(request, "session", None) and request.session.session_key
            if session_key is None and hasattr(request, "session"):
                # Ensure session exists
                request.session.save()
                session_key = request.session.session_key

            user_agent = request.META.get("HTTP_USER_AGENT", "")
            ip = get_client_ip(request)

            # The rest (geolocation HTTP call, bot-pattern check, Visit write)
            # is slow and must not delay the response — do it in Celery.
            from .tasks import record_visit

            record_visit.delay(
                session_key=session_key,
                user_id=request.user.id if request.user.is_authenticated else None,
                ip=ip,
                user_agent=user_agent,
                path=request.path,
                referrer=request.META.get("HTTP_REFERER", ""),
                duration=duration,
                response_status_code=response.status_code,
            )
        except DatabaseError:
            # Clear rollback flag so analytics hiccups don't poison the request transaction.
            conn = transaction.get_connection()
            if conn.in_atomic_block:
                transaction.set_rollback(False)
        except Exception:
            # don't break the site if analytics fails
            pass

        return response
