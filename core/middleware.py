from django.http import HttpResponsePermanentRedirect, HttpResponseRedirect
from django.utils.deprecation import MiddlewareMixin

from .models import Redirect


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
