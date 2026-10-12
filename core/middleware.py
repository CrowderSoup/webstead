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


class ThemePreviewMiddleware(MiddlewareMixin):
    """Honor ``?theme_preview=`` for public pages. Admin and MCP stay on the live theme."""

    def process_request(self, request):
        request._theme_preview_token = None
        if request.path.startswith(("/admin", "/mcp")):
            return None
        raw = request.GET.get("theme_preview")
        if not raw:
            return None
        from core.theme_preview import set_preview_slug, token_slug
        from core.themes import theme_exists_on_disk

        slug = token_slug(raw)
        if not slug or not theme_exists_on_disk(slug):
            return None
        request._theme_preview_slug = slug
        request._theme_preview_token = set_preview_slug(slug)
        request._skip_analytics = True
        return None

    def process_exception(self, request, exception):
        self._clear(request)
        return None

    def process_response(self, request, response):
        slug = getattr(request, "_theme_preview_slug", None)
        self._clear(request)
        if slug:
            from core.theme_preview import mark_preview_response

            mark_preview_response(response, slug)
        return response

    def _clear(self, request):
        token = getattr(request, "_theme_preview_token", None)
        if token is None:
            return
        from core.theme_preview import reset_preview_slug

        reset_preview_slug(token)
        request._theme_preview_token = None


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
