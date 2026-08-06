from django.http import HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.utils.deprecation import MiddlewareMixin

from .models import Redirect


class HealthCheckMiddleware(MiddlewareMixin):
    """Answer /healthz before ALLOWED_HOSTS validation runs.

    Kamal-proxy health-checks a freshly deployed container directly by its
    Docker container ID/IP (e.g. Host: ed940c7a5a56:8000), which will never
    match ALLOWED_HOSTS. Must sit ahead of CommonMiddleware, which is where
    that validation (request.get_host()) happens.
    """

    def process_request(self, request):
        if request.path == "/healthz":
            return HttpResponse("ok", content_type="text/plain")
        return None


class RedirectMiddleware(MiddlewareMixin):
    def process_request(self, request):
        redirect = Redirect.objects.filter(from_path=request.path).first()
        if redirect is None:
            return None

        if redirect.redirect_type == Redirect.PERMANENTLY:
            return HttpResponsePermanentRedirect(redirect.to_path)

        response = HttpResponseRedirect(redirect.to_path)
        response.status_code = 307
        return response
