from django.core.management.base import BaseCommand

from microsub.models import Subscription


class Command(BaseCommand):
    help = "Poll active Microsub feed subscriptions for new entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--channel",
            dest="channel",
            default=None,
            help="Only poll subscriptions in this channel uid.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Re-poll even recently-fetched feeds.",
        )
        parser.add_argument(
            "--doctor",
            action="store_true",
            default=False,
            help="Re-parse feeds and update existing entries with corrected data.",
        )

    def handle(self, *args, **options):
        from microsub.tasks import poll_subscription

        qs = Subscription.objects.filter(is_active=True).select_related("channel")
        if options["channel"]:
            qs = qs.filter(channel__uid=options["channel"])

        for sub in qs:
            try:
                result = poll_subscription.apply(
                    args=[sub.id],
                    kwargs={"force": options["force"], "doctor": options["doctor"]},
                )
            except Exception as exc:
                self.stdout.write(self.style.WARNING(f"{sub.url}: error — {exc}"))
                continue
            if not result.failed() and result.result and not result.result.get("skipped"):
                new = result.result.get("new", 0)
                updated = result.result.get("updated", 0)
                url = result.result.get("url", sub.url)
                self.stdout.write(f"{url}: {new} new" + (f", {updated} updated" if updated else ""))
