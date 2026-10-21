"""Guarded outbound HTTP for URLs that come from outside the site.

Webmention sources, feed URLs, IndieAuth `me` URLs, Micropub photo URLs and
the endpoints discovered from them are all attacker-controllable, so fetching
them with a plain urlopen/requests.get lets anyone make this server request
internal addresses (the Docker network, Redis, the cloud metadata service).

Everything here allows only http(s), rejects credentials in the URL, requires
every resolved address to be globally routable, and re-checks each redirect
hop. safe_urlopen also checks the address the socket actually connected to,
so a DNS answer that changes between the check and the connect (rebinding)
is still refused.
"""

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

import requests

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class UnsafeURLError(urllib.error.URLError):
    """Raised instead of fetching a URL that isn't a public http(s) URL.

    Subclasses URLError so existing `except URLError` handlers around
    urlopen calls treat it like any other fetch failure.
    """


def is_global_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_host(hostname: str) -> set[str]:
    return {
        sockaddr[0]
        for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    }


def is_public_host(hostname: str | None) -> bool:
    """True when every address the hostname resolves to is globally routable."""
    if not hostname:
        return False
    host = hostname.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            addresses = resolve_host(host)
        except (socket.gaierror, UnicodeError):
            return False
    if not addresses:
        return False
    return all(is_global_address(address) for address in addresses)


def validate_outbound_url(url: str) -> str:
    """Return url unchanged if it is safe to fetch, else raise UnsafeURLError."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UnsafeURLError("Invalid URL.") from exc
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError("Only http:// and https:// URLs can be fetched.")
    if not parsed.hostname:
        raise UnsafeURLError("URL must include a hostname.")
    if parsed.username or parsed.password:
        raise UnsafeURLError("Credentials in URLs are not allowed.")
    if not is_public_host(parsed.hostname):
        raise UnsafeURLError("URL must point to a public host.")
    return url


def _check_peer(sock) -> None:
    address = sock.getpeername()[0]
    if not is_global_address(address):
        sock.close()
        raise UnsafeURLError("Connected to a non-public address.")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_outbound_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    # Built by hand rather than with build_opener(), which would also install
    # the file:, ftp:, data: and proxy handlers.
    opener = urllib.request.OpenerDirector()
    for handler in (
        _GuardedHTTPHandler(),
        _GuardedHTTPSHandler(),
        _GuardedRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


_opener = _build_opener()


def safe_urlopen(url_or_request, data=None, timeout=10):
    """urllib.request.urlopen restricted to public http(s) hosts."""
    url = (
        url_or_request.full_url
        if isinstance(url_or_request, urllib.request.Request)
        else url_or_request
    )
    validate_outbound_url(url)
    return _opener.open(url_or_request, data=data, timeout=timeout)


def safe_get(url: str, *, max_redirects: int = MAX_REDIRECTS, **kwargs) -> requests.Response:
    """requests.get restricted to public http(s) hosts, checked on every redirect hop."""
    kwargs.pop("allow_redirects", None)
    current_url = validate_outbound_url(url)
    for _ in range(max_redirects + 1):
        response = requests.get(current_url, allow_redirects=False, **kwargs)
        location = response.headers.get("Location")
        if response.status_code not in _REDIRECT_STATUS_CODES or not location:
            return response
        response.close()
        current_url = validate_outbound_url(urljoin(current_url, location))
    raise UnsafeURLError("Too many redirects.")
