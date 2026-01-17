import json

from django.core.management.base import BaseCommand, CommandError

from core.themes import theme_storage_healthcheck


class Command(BaseCommand):
    help = "Check connectivity and permissions for theme storage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--write-test",
            action="store_true",
            help="Perform a write/delete round-trip in the theme storage prefix.",
        )

    def handle(self, *args, **options):
        result = theme_storage_healthcheck(write_test=options.get("write_test", False))
        self.stdout.write(json.dumps(result))
        if not result.get("ok"):
            raise CommandError("Theme storage healthcheck failed.")
