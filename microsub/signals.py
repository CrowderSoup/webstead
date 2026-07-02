import logging
from urllib.parse import urlparse

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse

logger = logging.getLogger(__name__)


@receiver(post_save, sender="micropub.Webmention")
def webmention_to_notifications(sender, instance, created, **kwargs):
    if not created:
        return
    if not instance.is_incoming:
        return

    from .models import Channel
    from .views import _store_entries

    channel = Channel.objects.filter(uid="notifications").first()
    if not channel:
        return

    parsed = urlparse(instance.target)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    detail_path = reverse("site_admin:webmention_detail", args=[instance.pk])
    admin_url = f"{base_url}{detail_path}"

    jf2 = {
        "type": "entry",
        "url": admin_url,
        "wm-source": instance.source,
        "published": instance.created_at.isoformat(),
        "content": {"text": ""},
        "author": {"type": "card", "url": instance.source},
    }

    if instance.mention_type == "like":
        jf2["like-of"] = instance.target
    elif instance.mention_type == "reply":
        jf2["in-reply-to"] = instance.target
    elif instance.mention_type == "repost":
        jf2["repost-of"] = instance.target

    uid = f"webmention:{instance.pk}"

    try:
        _store_entries(
            channel,
            None,
            [{**jf2, "_uid": uid}],
        )
    except Exception:
        logger.exception("Failed to create notifications entry for webmention %s", instance.pk)
