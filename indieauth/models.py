from django.conf import settings
from django.db import models


class IndieAuthClient(models.Model):
    client_id = models.URLField(max_length=2000, unique=True)
    name = models.CharField(max_length=255, blank=True, default="")
    logo_url = models.URLField(max_length=2000, blank=True, default="")
    redirect_uris = models.JSONField(default=list, blank=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    fetch_error = models.TextField(blank=True, default="")

    def __str__(self):
        return self.name or self.client_id


class IndieAuthAuthorizationCode(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)
    code_challenge = models.CharField(max_length=255)
    code_challenge_method = models.CharField(max_length=32, default="S256")
    client_id = models.URLField(max_length=2000)
    redirect_uri = models.URLField(max_length=2000)
    me = models.URLField(max_length=2000)
    scope = models.TextField(blank=True, default="")
    # RFC 8707 resource indicator the token will be bound to (e.g. the MCP endpoint).
    resource = models.CharField(max_length=2000, blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.client_id} -> {self.me}"


class IndieAuthAccessToken(models.Model):
    # Personal access tokens are minted in the admin rather than through an
    # IndieAuth client, so they get this client_id instead of a URL.
    PERSONAL_CLIENT_ID = "urn:webstead:pat"

    token_hash = models.CharField(max_length=64, unique=True)
    # A URL for IndieAuth clients, PERSONAL_CLIENT_ID for personal tokens.
    client_id = models.CharField(max_length=2000)
    me = models.URLField(max_length=2000)
    scope = models.TextField(blank=True, default="")
    # Audience (RFC 8707). Empty for IndieAuth/Micropub tokens; the MCP
    # endpoint's URL for tokens issued to MCP clients over OAuth.
    resource = models.CharField(max_length=2000, blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name or self.client_id} -> {self.me}"

    @property
    def is_personal(self):
        return self.client_id == self.PERSONAL_CLIENT_ID

    @property
    def scopes(self):
        return set(self.scope.split())

    def mark_used(self):
        """Record use, at most once a minute so busy clients don't write on every call."""
        from django.utils import timezone

        now = timezone.now()
        if self.last_used_at is None or now - self.last_used_at > timezone.timedelta(minutes=1):
            type(self).objects.filter(pk=self.pk).update(last_used_at=now)
            self.last_used_at = now


class IndieAuthRefreshToken(models.Model):
    """A rotating refresh token for one connection (an IndieAuthAccessToken row).

    Refreshing swaps the connection's access token hash in place, so a
    connection stays one row (with its revisions and logs) for its whole
    life, and revoking it in the admin ends the refresh chain too. Each
    refresh token works once; presenting a used one again revokes the
    connection, since it means the token leaked.
    """

    token_hash = models.CharField(max_length=64, unique=True)
    access_token = models.ForeignKey(
        IndieAuthAccessToken, on_delete=models.CASCADE, related_name="refresh_tokens"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"refresh for {self.access_token}"


class IndieAuthConsent(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    client_id = models.URLField(max_length=2000)
    scope = models.TextField(blank=True, default="")  # what the client asked for
    resource = models.CharField(max_length=2000, blank=True, default="")
    # Exactly what the user approved, including an explicitly empty grant.
    granted_scope = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ["user", "client_id", "scope", "resource"]

    def __str__(self):
        return f"{self.user_id} {self.client_id}"


class IndieAuthRequestLog(models.Model):
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField()
    error = models.TextField(blank=True)
    request_headers = models.JSONField(default=dict)
    request_query = models.JSONField(default=dict)
    request_body = models.TextField(blank=True)
    response_body = models.TextField(blank=True)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    content_type = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.method} {self.path} -> {self.status_code}"
