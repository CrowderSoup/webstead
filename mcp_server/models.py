from django.db import models


class McpRequestLog(models.Model):
    """One row per MCP request, successful or not: the audit trail for
    everything an agent does through /mcp."""

    OK = "ok"
    TOOL_ERROR = "tool_error"
    ERROR = "error"
    STATUS_CHOICES = [(OK, "OK"), (TOOL_ERROR, "Tool error"), (ERROR, "Protocol error")]

    created_at = models.DateTimeField(auto_now_add=True)
    method = models.CharField(max_length=64, blank=True)
    tool = models.CharField(max_length=128, blank=True)
    arguments = models.JSONField(default=dict, blank=True)  # redacted
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    http_status = models.PositiveSmallIntegerField()
    message = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    protocol_version = models.CharField(max_length=32, blank=True)
    token = models.ForeignKey(
        "indieauth.IndieAuthAccessToken",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    client_id = models.CharField(max_length=2000, blank=True)
    client_name = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self):
        return f"{self.method} {self.tool} -> {self.status}"
