from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import ThemeInstall
from core.theme_sync import reconcile_installed_themes


class Command(BaseCommand):
    help = "Reconcile installed themes against local files and storage."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Limit reconciliation to a single theme slug.")
        parser.add_argument("--dry-run", action="store_true", help="Report actions without modifying data.")
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero when any theme fails reconciliation.",
        )

    def handle(self, *args, **options):
        slug = options.get("slug")
        dry_run = options.get("dry_run", False)
        strict = options.get("strict", False)

        slugs = None
        if slug:
            if not ThemeInstall.objects.filter(slug=slug).exists():
                raise CommandError(f"No installed theme found for slug '{slug}'.")
            slugs = [slug]

        results = reconcile_installed_themes(slugs=slugs, dry_run=dry_run)
        success_count = len([result for result in results if result.status == ThemeInstall.STATUS_SUCCESS])
        failed_count = len([result for result in results if result.status == ThemeInstall.STATUS_FAILED])
        total_count = len(results)

        self.stdout.write(
            f"Reconciled {total_count} theme(s): {success_count} succeeded, {failed_count} failed."
        )

        if strict and failed_count:
            raise CommandError("One or more themes failed to reconcile.")
