from __future__ import annotations

from django.core.management.base import BaseCommand

from blog.activity import activity_from_mf2
from blog.models import ActivityFlyover, Post
from blog.tasks import enqueue_activity_flyover


class Command(BaseCommand):
    help = "Enqueue flyover generation tasks for activity posts with GPX data."

    def handle(self, *args, **options):
        queryset = Post.objects.filter(kind=Post.ACTIVITY, deleted=False)
        queued = 0

        for post in queryset:
            activity = activity_from_mf2(post)
            if not activity.get("track_url") and not post.gpx_attachment:
                continue
            flyover, _ = ActivityFlyover.objects.get_or_create(post=post)
            if flyover.status == ActivityFlyover.READY and flyover.video:
                continue
            if flyover.enqueued_at:
                continue
            enqueue_activity_flyover(flyover)
            queued += 1

        self.stdout.write(self.style.SUCCESS(f"Queued {queued} flyover job(s)."))
