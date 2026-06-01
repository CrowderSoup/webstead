"""
Mastodon Celery tasks.

publish_post_to_mastodon(post_id)   — triggered on post first-publish
poll_mastodon_timeline()            — scheduled every 15 min
poll_mastodon_notifications()       — scheduled every 15 min
"""

import logging
import re

from celery import shared_task
from django.conf import settings
from django.db import IntegrityError
from mastodon import MastodonNetworkError, MastodonRatelimitError, MastodonServerError

from ._utils import _get
from .subscriptions import ensure_managed_subscription

logger = logging.getLogger(__name__)


def _should_syndicate(post) -> bool:
    """
    Determine whether this post should be published to Mastodon.

    Resolution order:
      1. post.mastodon_syndicate is not None → use it directly
      2. Look up MastodonSyndicationDefault for post.kind
      3. Default to False
    """
    if post.mastodon_syndicate is not None:
        return bool(post.mastodon_syndicate)

    from .models import MastodonSyndicationDefault
    try:
        default = MastodonSyndicationDefault.objects.get(post_kind=post.kind)
        return default.publish
    except MastodonSyndicationDefault.DoesNotExist:
        return False


def _build_canonical_url(post) -> str:
    """Return the absolute URL for a post using the site's domain from SiteConfiguration."""
    try:
        from core.models import SiteConfiguration
        site = SiteConfiguration.get_solo()
        domain = (
            getattr(site, "site_url", None)
            or getattr(site, "domain", None)
            or getattr(site, "base_url", None)
            or getattr(settings, "MICROSUB_BASE_URL", "")
        )
        if domain:
            domain = domain.rstrip("/")
            if not domain.startswith(("http://", "https://")):
                domain = f"https://{domain}"
            return f"{domain}{post.get_absolute_url()}"
    except Exception:
        pass
    return post.get_absolute_url()


def _timeline_content_status(status):
    reblog = _get(status, "reblog")
    return reblog if reblog else status


def _should_store_timeline_status(status, account) -> bool:
    from .models import MastodonAccount

    reply_filter = account.timeline_reply_filter or MastodonAccount.TIMELINE_REPLIES_ALL
    if reply_filter == MastodonAccount.TIMELINE_REPLIES_ALL:
        return True

    content_status = _timeline_content_status(status)
    in_reply_to_id = _get(content_status, "in_reply_to_id")
    if not in_reply_to_id:
        return True

    if reply_filter == MastodonAccount.TIMELINE_REPLIES_HIDE:
        return False

    if reply_filter == MastodonAccount.TIMELINE_REPLIES_SELF_THREADS:
        author = _get(content_status, "account")
        author_id = str(_get(author, "id") or "")
        reply_to_account_id = str(_get(content_status, "in_reply_to_account_id") or "")
        return bool(author_id and reply_to_account_id and author_id == reply_to_account_id)

    return True


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def publish_post_to_mastodon(self, post_id: int):
    """
    Publish a Webstead Post to Mastodon.

    Triggered from micropub.webmention.queue_webmentions_for_post after a post
    is first published.  Idempotent: if a MastodonPost record already exists
    for this post, the task exits early.
    """
    from django.db import close_old_connections
    from blog.models import Post
    from .models import MastodonAccount, MastodonPost

    close_old_connections()

    try:
        post = Post.objects.prefetch_related("tags", "attachments__asset").get(id=post_id)
    except Post.DoesNotExist:
        logger.warning("publish_post_to_mastodon: post %s not found", post_id)
        return

    # Guard: deleted or unpublished
    if post.deleted or not post.is_published():
        return

    # Guard: idempotency — already syndicated
    if MastodonPost.objects.filter(post=post).exists():
        logger.debug("publish_post_to_mastodon: post %s already syndicated", post_id)
        return

    # Guard: syndication enabled?
    if not _should_syndicate(post):
        logger.debug("publish_post_to_mastodon: post %s skipped (syndication off)", post_id)
        return

    # Guard: active connected account
    account = MastodonAccount.get_active()
    if not account:
        logger.warning("publish_post_to_mastodon: no active Mastodon account")
        return

    try:
        from .client import get_client
        from .formatting import format_post

        canonical_url = _build_canonical_url(post)
        toot_text, cw_text = format_post(post, account.max_toot_chars, canonical_url)

        client = get_client(account)

        # Handle media uploads for photo posts (Mastodon allows max 4)
        media_ids = []
        if post.kind == "photo":
            for attachment in list(post.photo_attachments)[:4]:
                try:
                    media = client.media_post(
                        attachment.asset.file,
                        description=attachment.asset.alt_text or "",
                    )
                    media_ids.append(media["id"])
                except Exception as exc:
                    logger.warning(
                        "publish_post_to_mastodon: media upload failed for post %s: %s",
                        post_id, exc,
                    )

        # Determine in_reply_to_id if this is a reply to a Mastodon post
        in_reply_to_id = None
        if post.kind == "reply" and post.in_reply_to:
            try:
                mp = MastodonPost.objects.filter(mastodon_url=post.in_reply_to).first()
                if mp:
                    # Reply to a post we syndicated ourselves
                    in_reply_to_id = mp.mastodon_id
                else:
                    # Fallback: extract the numeric status ID from the URL path.
                    # Covers the common patterns:
                    #   https://instance.social/@user/1234567890
                    #   https://instance.social/users/user/statuses/1234567890
                    m = re.search(r"/(\d+)$", post.in_reply_to.rstrip("/"))
                    if m:
                        in_reply_to_id = m.group(1)
            except Exception:
                pass

        status = client.status_post(
            status=toot_text,
            spoiler_text=cw_text or None,
            media_ids=media_ids or None,
            in_reply_to_id=in_reply_to_id,
            visibility="public",
        )

        mastodon_id = str(status["id"])
        mastodon_url = status.get("url") or status.get("uri", "")

        MastodonPost.objects.create(
            post=post,
            mastodon_id=mastodon_id,
            mastodon_url=mastodon_url,
        )

        # Record syndication URL in post.mf2
        mf2 = post.mf2 if isinstance(post.mf2, dict) else {}
        syndication = mf2.get("syndication", [])
        if mastodon_url not in syndication:
            syndication.append(mastodon_url)
            mf2["syndication"] = syndication
            post.mf2 = mf2
            post.save(update_fields=["mf2"])

        logger.info(
            "publish_post_to_mastodon: published post %s → %s", post_id, mastodon_url
        )

    except (MastodonNetworkError, MastodonRatelimitError, MastodonServerError) as exc:
        logger.warning(
            "publish_post_to_mastodon: retriable error for post %s: %s", post_id, exc
        )
        raise self.retry(exc=exc)
    except Exception:
        logger.exception("publish_post_to_mastodon: non-retriable error for post %s", post_id)
    finally:
        close_old_connections()


@shared_task(ignore_result=True)
def poll_mastodon_timeline():
    """
    Fetch the Mastodon home timeline since the last seen status ID and write
    new statuses as microsub.Entry objects into the configured timeline channel.

    Scheduled every 15 minutes by Celery Beat.
    """
    from django.db import close_old_connections
    close_old_connections()

    from .models import MastodonAccount
    account = MastodonAccount.get_active()
    if not account:
        logger.debug("poll_mastodon_timeline: no active account")
        return

    channel = account.timeline_channel
    if not channel:
        logger.debug("poll_mastodon_timeline: no timeline channel configured")
        return
    subscription = ensure_managed_subscription(account, "timeline")
    if not subscription:
        logger.debug("poll_mastodon_timeline: no managed timeline feed available")
        return

    from .client import get_client, status_to_jf2
    from microsub.views import _store_entries
    client = get_client(account)

    fetch_kwargs = {"limit": 40}
    if account.last_timeline_id:
        fetch_kwargs["since_id"] = account.last_timeline_id

    try:
        statuses = client.timeline_home(**fetch_kwargs)
    except Exception as exc:
        logger.exception("poll_mastodon_timeline: fetch failed: %s", exc)
        return

    if not statuses:
        logger.debug("poll_mastodon_timeline: no new statuses")
        return

    created_count = 0
    latest_id = account.last_timeline_id

    for status in reversed(statuses):  # oldest-first so we process in order
        status_id = str(_get(status, "id") or "")
        if not status_id:
            continue

        created_at = _get(status, "created_at")
        account_obj = _get(status, "account")
        author_url = str(_get(account_obj, "url") or "")

        uid = f"mastodon:status:{status_id}"
        jf2 = status_to_jf2(status)

        if not _should_store_timeline_status(status, account):
            if not latest_id or status_id > latest_id:
                latest_id = status_id
            continue

        jf2["_uid"] = uid
        if author_url:
            source = jf2.get("_source")
            if not isinstance(source, dict):
                source = {}
            source["url"] = source.get("url") or author_url
            jf2["_source"] = source

        try:
            created_count += _store_entries(channel, subscription, [jf2])
        except IntegrityError:
            pass
        except Exception as exc:
            logger.warning(
                "poll_mastodon_timeline: failed to create entry %s: %s", uid, exc
            )
            continue

        # Track the highest status ID seen (IDs are snowflakes — string comparison works)
        if not latest_id or status_id > latest_id:
            latest_id = status_id

    if latest_id != account.last_timeline_id:
        account.last_timeline_id = latest_id
        account.save(update_fields=["last_timeline_id"])

    logger.info("poll_mastodon_timeline: created %d new entries in channel '%s'", created_count, channel.name)
    close_old_connections()


@shared_task(ignore_result=True)
def poll_mastodon_notifications():
    """
    Fetch Mastodon notifications since the last seen notification ID.

    - favourites / reblogs on Webstead posts → incoming webmentions
    - mentions / replies                      → microsub entries in notifications channel
    - follows                                 → microsub entries in notifications channel
    - all notifications                       → microsub entries in notifications channel
                                                (if notifications channel configured)

    Scheduled every 15 minutes by Celery Beat.
    """
    from django.db import close_old_connections
    from micropub.models import Webmention
    close_old_connections()

    from .models import MastodonAccount, MastodonPost
    account = MastodonAccount.get_active()
    if not account:
        logger.debug("poll_mastodon_notifications: no active account")
        return

    from .client import get_client, status_to_jf2, strip_html
    from microsub.views import _store_entries
    client = get_client(account)

    fetch_kwargs = {"limit": 40}
    if account.last_notification_id:
        fetch_kwargs["since_id"] = account.last_notification_id

    try:
        notifications = client.notifications(**fetch_kwargs)
    except Exception as exc:
        logger.exception("poll_mastodon_notifications: fetch failed: %s", exc)
        return

    if not notifications:
        logger.debug("poll_mastodon_notifications: no new notifications")
        return

    notifications_channel = account.notifications_channel
    subscription = ensure_managed_subscription(account, "notifications") if notifications_channel else None
    webmention_count = 0
    entry_count = 0
    latest_id = account.last_notification_id

    for notif in reversed(notifications):  # oldest-first
        notif_id = str(_get(notif, "id") or "")
        if not notif_id:
            continue

        notif_type = _get(notif, "type") or ""  # favourite, reblog, mention, follow, poll, etc.
        notif_account = _get(notif, "account")  # the person who acted
        notif_status = _get(notif, "status")    # the status involved (if any)
        created_at = _get(notif, "created_at")

        actor_url = str(_get(notif_account, "url") or "")
        actor_name = str(_get(notif_account, "display_name") or _get(notif_account, "username") or "")
        actor_avatar = str(_get(notif_account, "avatar") or "")

        # Track latest notification ID
        if not latest_id or notif_id > latest_id:
            latest_id = notif_id

        # --- Webmentions for favourites / reblogs of our posts ---
        if notif_type in ("favourite", "reblog") and notif_status:
            status_url = str(_get(notif_status, "url") or _get(notif_status, "uri") or "")
            status_id = str(_get(notif_status, "id") or "")

            # Find the local post that was syndicated to this toot
            local_post = None
            try:
                mp = MastodonPost.objects.filter(mastodon_id=status_id).select_related("post").first()
                if mp and mp.post and not mp.post.deleted:
                    local_post = mp.post
            except Exception:
                pass

            if local_post:
                canonical_url = _build_canonical_url(local_post)
                mention_type = Webmention.LIKE if notif_type == "favourite" else Webmention.REPOST
                # Source: actor's profile URL (for likes) or their boost URL (for reblogs)
                source_url = actor_url
                if notif_type == "reblog":
                    reblog_url = str(_get(notif_status, "url") or "")
                    # For reblogs, the notif_status IS the boost — find the actor's version
                    # Prefer the reblogging account's status URI if available
                    source_url = reblog_url or actor_url

                try:
                    _, created = Webmention.objects.get_or_create(
                        source=source_url,
                        target=canonical_url,
                        defaults={
                            "mention_type": mention_type,
                            "status": Webmention.ACCEPTED,
                            "target_post": local_post,
                            "is_incoming": True,
                            "error": "",
                        },
                    )
                    if created:
                        webmention_count += 1
                        logger.debug(
                            "poll_mastodon_notifications: %s webmention %s → %s",
                            mention_type, source_url, canonical_url,
                        )
                except Exception as exc:
                    logger.warning(
                        "poll_mastodon_notifications: webmention creation failed for notif %s: %s",
                        notif_id, exc,
                    )

        # --- Microsub entries for all notification types ---
        if notifications_channel and subscription:
            uid = f"mastodon:notification:{notif_id}"
            jf2 = _notification_to_jf2(
                notif_type=notif_type,
                notif_account=notif_account,
                notif_status=notif_status,
                actor_url=actor_url,
                actor_name=actor_name,
                actor_avatar=actor_avatar,
                created_at=created_at,
                status_to_jf2_fn=status_to_jf2,
                strip_html_fn=strip_html,
            )
            jf2["_uid"] = uid
            if actor_url:
                source = jf2.get("_source")
                if not isinstance(source, dict):
                    source = {}
                source["url"] = source.get("url") or actor_url
                jf2["_source"] = source

            try:
                entry_count += _store_entries(notifications_channel, subscription, [jf2])
            except IntegrityError:
                pass
            except Exception as exc:
                logger.warning(
                    "poll_mastodon_notifications: entry creation failed for notif %s: %s",
                    notif_id, exc,
                )

    if latest_id != account.last_notification_id:
        account.last_notification_id = latest_id
        account.save(update_fields=["last_notification_id"])

    logger.info(
        "poll_mastodon_notifications: %d webmentions, %d microsub entries",
        webmention_count, entry_count,
    )
    close_old_connections()


def _notification_to_jf2(
    *,
    notif_type: str,
    notif_account,
    notif_status,
    actor_url: str,
    actor_name: str,
    actor_avatar: str,
    created_at,
    status_to_jf2_fn,
    strip_html_fn,
) -> dict:
    """
    Convert a Mastodon notification to a JF2 dict for microsub.Entry.data.

    Notification types handled:
      mention / reply   → entry with content of the mentioning status
      favourite         → like-of pointing at the favourited status URL
      reblog            → repost-of pointing at the original status URL
      follow            → follow notification entry
      poll              → poll-ended entry
      other             → generic entry
    """
    author = {
        "type": "card",
        "name": actor_name,
        "url": actor_url,
        "photo": actor_avatar,
    }

    if notif_type in ("mention",) and notif_status:
        # Full JF2 entry from the status
        jf2 = status_to_jf2_fn(notif_status)
        jf2["_notification_type"] = notif_type
        return jf2

    if notif_type == "favourite" and notif_status:
        status_url = str(_get(notif_status, "url") or _get(notif_status, "uri") or "")
        return {
            "type": "entry",
            "_notification_type": "favourite",
            "like-of": status_url,
            "author": author,
            "published": created_at.isoformat() if created_at else None,
            "url": actor_url,
            "content": {"text": f"{actor_name} favourited your post", "html": ""},
        }

    if notif_type == "reblog" and notif_status:
        # The notif_status for a reblog IS the boost; find original URL
        reblog_content = _get(notif_status, "reblog")
        original_url = str(
            _get(reblog_content, "url") if reblog_content else _get(notif_status, "url") or ""
        )
        return {
            "type": "entry",
            "_notification_type": "reblog",
            "repost-of": original_url,
            "author": author,
            "published": created_at.isoformat() if created_at else None,
            "url": actor_url,
            "content": {"text": f"{actor_name} boosted your post", "html": ""},
        }

    if notif_type == "follow":
        return {
            "type": "entry",
            "_notification_type": "follow",
            "author": author,
            "published": created_at.isoformat() if created_at else None,
            "url": actor_url,
            "content": {"text": f"{actor_name} followed you", "html": ""},
        }

    if notif_type == "poll" and notif_status:
        status_url = str(_get(notif_status, "url") or _get(notif_status, "uri") or "")
        content_html = str(_get(notif_status, "content") or "")
        return {
            "type": "entry",
            "_notification_type": "poll",
            "url": status_url,
            "author": author,
            "published": created_at.isoformat() if created_at else None,
            "content": {
                "html": content_html,
                "text": strip_html_fn(content_html),
            },
        }

    # Generic fallback
    content_html = ""
    url = actor_url
    if notif_status:
        content_html = str(_get(notif_status, "content") or "")
        url = str(_get(notif_status, "url") or actor_url)

    return {
        "type": "entry",
        "_notification_type": notif_type,
        "url": url,
        "author": author,
        "published": created_at.isoformat() if created_at else None,
        "content": {
            "html": content_html,
            "text": strip_html_fn(content_html) if content_html else "",
        },
    }
