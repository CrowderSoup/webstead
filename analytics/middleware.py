import time

from django.db import DatabaseError, transaction
from django.utils.deprecation import MiddlewareMixin

from .models import Visit
from .utils import get_client_ip, geolocate_ip  # you write these

class AnalyticsMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.path.startswith("/admin"):
            return
        request._analytics_start_ts = time.time()

    def process_response(self, request, response):
        try:
            if request.path.startswith("/admin"):
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

            ip = get_client_ip(request)
            geo = geolocate_ip(ip) if ip else {}

            visit = Visit.objects.create(
                session_key=session_key,
                user=request.user if request.user.is_authenticated else None,
                ip_address=ip,
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                path=request.path,
                referrer=request.META.get("HTTP_REFERER", ""),
                duration_seconds=duration,
                country=geo.get("country", ""),
                region=geo.get("region", ""),
                city=geo.get("city", ""),
            )

            request.visit_id = visit.id
        except DatabaseError:
            # Clear rollback flag so analytics hiccups don't poison the request transaction.
            conn = transaction.get_connection()
            if conn.in_atomic_block:
                transaction.set_rollback(False)
        except Exception:
            # don't break the site if analytics fails
            pass

        return response
