from django.core.management.base import BaseCommand, CommandError

from core.themes import ThemeUploadError, install_theme_from_git


class Command(BaseCommand):
    help = "Install a theme from a git repository."

    def add_arguments(self, parser):
        parser.add_argument("--git", dest="git_url", help="Git URL for the theme repository.")
        parser.add_argument("--ref", dest="ref", default="", help="Git ref (branch, tag, or commit).")
        parser.add_argument("--slug", dest="slug", help="Theme slug to install.")

    def handle(self, *args, **options):
        git_url = (options.get("git_url") or "").strip()
        ref = (options.get("ref") or "").strip()
        slug = (options.get("slug") or "").strip()

        if not git_url:
            raise CommandError("--git is required.")
        if not slug:
            raise CommandError("--slug is required.")

        try:
            theme = install_theme_from_git(git_url, slug, ref=ref)
        except ThemeUploadError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Installed theme '{theme.label}' ({theme.slug}).")
