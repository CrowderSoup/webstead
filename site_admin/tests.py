import json
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from analytics.models import (
    UserAgentBotRule,
    UserAgentFalsePositive,
    UserAgentIgnore,
    Visit,
)
from blog.models import Comment, Post
from core.models import (
    HCard,
    HCardPhoto,
    Menu,
    Page,
    RequestErrorLog,
    SiteConfiguration,
    ThemeInstall,
)
from core.themes import ThemeDefinition, ThemeUpdateResult
from core.test_utils import build_test_theme
from files.models import Attachment, File
from indieauth.models import (
    IndieAuthAccessToken,
    IndieAuthAuthorizationCode,
    IndieAuthClient,
    IndieAuthConsent,
)
from micropub.models import Webmention
from widgets.models import WidgetInstance


class SiteAdminAccessTests(TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="reader",
            email="reader@example.com",
            password="password",
        )
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_admin_bar_requires_staff(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("site_admin:admin_bar"))

        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:admin_bar"))

        self.assertEqual(response.status_code, 200)

    def test_dashboard_requires_staff(self):
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertRedirects(
            response,
            f"{reverse('site_admin:login')}?next={reverse('site_admin:dashboard')}",
        )

        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertEqual(response.status_code, 200)

    def test_admin_bar_hides_theme_toggle_on_site(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:admin_bar"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-admin-theme-toggle")
        self.assertNotContains(response, "Dark mode")
        self.assertNotContains(response, "Light mode")

    def test_admin_bar_theme_toggle_only_once(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        matches = re.findall(
            r"<button[^>]*class=\"[^\"]*site-admin-bar__menu-action[^\"]*\"[^>]*data-admin-theme-toggle",
            content,
        )
        self.assertEqual(len(matches), 1)

    def test_dashboard_includes_global_toast_container(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="site-admin-toast-stack"')
        self.assertContains(response, 'id="site-admin-messages-data"')

    def test_dashboard_mobile_menu_includes_analytics_and_settings_links(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("site_admin:analytics_dashboard")}" class="site-admin-bar__mobile-link">Analytics</a>',
            html=False,
        )
        self.assertContains(
            response,
            f'href="{reverse("site_admin:site_settings")}" class="site-admin-bar__mobile-link">Site settings</a>',
            html=False,
        )


class SiteAdminPageTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_page_create_htmx_redirects_to_edit(self):
        self.client.force_login(self.staff)
        published_on = timezone.localtime(timezone.now()).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(
            reverse("site_admin:page_create"),
            {
                "title": "About",
                "content": "Hello world",
                "published_on": published_on,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn("/admin/pages/", response["HX-Redirect"])

        page = self.staff.page_set.first()
        self.assertIsNotNone(page)
        self.assertEqual(page.author, self.staff)

    def test_page_edit_saves_uploaded_photo(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "photo.jpg", b"fake-image-data", content_type="image/jpeg"
                )
                asset = File.objects.create(
                    kind=File.IMAGE, file=upload, owner=self.staff
                )
                page = Page.objects.create(
                    title="Gallery Test",
                    slug="gallery-test",
                    content="Hello",
                    published_on=timezone.now(),
                    author=self.staff,
                )
                published_on = timezone.localtime(page.published_on).strftime(
                    "%Y-%m-%dT%H:%M"
                )
                response = self.client.post(
                    reverse("site_admin:page_edit", kwargs={"slug": page.slug}),
                    {
                        "title": page.title,
                        "content": page.content,
                        "published_on": published_on,
                        "uploaded_ids": [str(asset.id)],
                        "uploaded_alts": ["A nice photo"],
                        "uploaded_captions": ["Caption text"],
                        "uploaded_positions": ["0"],
                    },
                )

                self.assertEqual(response.status_code, 302)
                page.refresh_from_db()
                attachment = page.attachments.filter(role="photo").first()
                self.assertIsNotNone(attachment)
                self.assertEqual(attachment.asset, asset)

    def test_page_edit_removes_existing_photo(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "photo.jpg", b"fake-image-data", content_type="image/jpeg"
                )
                asset = File.objects.create(
                    kind=File.IMAGE, file=upload, owner=self.staff
                )
                page = Page.objects.create(
                    title="Remove Photo Test",
                    slug="remove-photo-test",
                    content="Hello",
                    published_on=timezone.now(),
                    author=self.staff,
                )
                attachment = Attachment.objects.create(
                    content_object=page, asset=asset, role="photo", sort_order=0
                )
                published_on = timezone.localtime(page.published_on).strftime(
                    "%Y-%m-%dT%H:%M"
                )
                response = self.client.post(
                    reverse("site_admin:page_edit", kwargs={"slug": page.slug}),
                    {
                        "title": page.title,
                        "content": page.content,
                        "published_on": published_on,
                        "existing_remove_ids": [str(asset.id)],
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertFalse(Attachment.objects.filter(id=attachment.id).exists())

    def test_page_is_gallery_saves(self):
        self.client.force_login(self.staff)
        page = Page.objects.create(
            title="Gallery Flag Test",
            slug="gallery-flag-test",
            content="Hello",
            published_on=timezone.now(),
            author=self.staff,
        )
        published_on = timezone.localtime(page.published_on).strftime(
            "%Y-%m-%dT%H:%M"
        )
        response = self.client.post(
            reverse("site_admin:page_edit", kwargs={"slug": page.slug}),
            {
                "title": page.title,
                "content": page.content,
                "published_on": published_on,
                "is_gallery": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        page.refresh_from_db()
        self.assertTrue(page.is_gallery)

    def test_gallery_page_shows_slider_in_template(self):
        with (
            tempfile.TemporaryDirectory() as themes_root,
            tempfile.TemporaryDirectory() as media_root,
        ):
            with override_settings(THEMES_ROOT=themes_root, MEDIA_ROOT=media_root):
                from core.themes import clear_template_caches

                page_template = (
                    "{% load author %}"
                    "{% if page.is_gallery %}"
                    "{% with photos=page.attachments.all %}"
                    '{% include "blog/_photo_gallery.html" %}'
                    "{% endwith %}"
                    "{% endif %}"
                )
                gallery_template = (
                    "{% for photo in photos %}"
                    '<section class="photo-slider">{{ photo.asset.alt_text }}</section>'
                    "{% endfor %}"
                )
                build_test_theme(
                    "gallery-theme",
                    themes_root,
                    extra_files=[
                        ("templates/core/page.html", page_template),
                        ("templates/blog/_photo_gallery.html", gallery_template),
                    ],
                )
                config = SiteConfiguration.get_solo()
                config.active_theme = "gallery-theme"
                config.save()
                clear_template_caches()

                try:
                    upload = SimpleUploadedFile(
                        "photo.jpg", b"fake-image-data", content_type="image/jpeg"
                    )
                    asset = File.objects.create(
                        kind=File.IMAGE, file=upload, owner=self.staff
                    )
                    page = Page.objects.create(
                        title="Slider Page",
                        slug="slider-page",
                        content="",
                        published_on=timezone.now(),
                        author=self.staff,
                        is_gallery=True,
                    )
                    Attachment.objects.create(
                        content_object=page,
                        asset=asset,
                        role="photo",
                        sort_order=0,
                    )

                    response = self.client.get(
                        reverse("page", kwargs={"slug": page.slug})
                    )

                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, "photo-slider")
                finally:
                    config.active_theme = ""
                    config.save()
                    clear_template_caches()


class SiteAdminAnalyticsTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def _create_visit(self, **kwargs):
        visit = Visit.objects.create(
            path=kwargs.get("path", "/"),
            user_agent=kwargs.get("user_agent", "TestAgent/1.0"),
            response_status_code=kwargs.get("response_status_code"),
        )
        started_at = kwargs.get("started_at")
        if started_at:
            Visit.objects.filter(id=visit.id).update(started_at=started_at)
            visit.refresh_from_db()
        return visit

    def test_analytics_pages_require_staff(self):
        urls = [
            reverse("site_admin:analytics_user_agents"),
            reverse("site_admin:analytics_bot_detection"),
            reverse("site_admin:analytics_ignored_user_agents"),
            reverse("site_admin:analytics_errors_by_user_agent"),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertRedirects(
                response,
                f"{reverse('site_admin:login')}?next={url}",
            )

        self.client.force_login(self.staff)
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_delete_error_visits(self):
        self.client.force_login(self.staff)
        Visit.objects.create(path="/missing", response_status_code=404)
        Visit.objects.create(path="/missing", response_status_code=404)
        Visit.objects.create(path="/missing", response_status_code=500)

        response = self.client.post(
            reverse("site_admin:analytics_delete_error"),
            {"path": "/missing", "status": "404"},
        )

        self.assertRedirects(response, reverse("site_admin:analytics_dashboard"))
        self.assertEqual(
            Visit.objects.filter(path="/missing", response_status_code=404).count(),
            0,
        )
        self.assertEqual(
            Visit.objects.filter(path="/missing", response_status_code=500).count(),
            1,
        )

    def test_user_agent_search_filters_and_counts(self):
        self.client.force_login(self.staff)
        today = timezone.localdate()
        in_range = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        out_of_range = in_range - timedelta(days=60)

        self._create_visit(user_agent="BotCrawler/2.0", started_at=in_range)
        self._create_visit(user_agent="BotCrawler/2.0", started_at=in_range)
        self._create_visit(user_agent="Mozilla/5.0", started_at=in_range)
        self._create_visit(user_agent="BotCrawler/2.0", started_at=out_of_range)

        response = self.client.get(
            reverse("site_admin:analytics_user_agents"),
            {
                "q": "bot",
                "start": (today - timedelta(days=7)).isoformat(),
                "end": today.isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        user_agents = response.context["user_agents"]
        self.assertEqual(len(user_agents), 1)
        self.assertEqual(user_agents[0]["user_agent"], "BotCrawler/2.0")
        self.assertEqual(user_agents[0]["count"], 2)

    def test_ignored_user_agents_list_and_unignore(self):
        self.client.force_login(self.staff)
        UserAgentIgnore.objects.create(user_agent="TestBot/1.0")
        UserAgentIgnore.objects.create(user_agent="SkipMe/2.0")

        response = self.client.get(reverse("site_admin:analytics_ignored_user_agents"))
        self.assertEqual(response.status_code, 200)
        ignored = list(response.context["ignored_user_agents"])
        self.assertEqual(len(ignored), 2)

        response = self.client.post(
            reverse("site_admin:analytics_unignore_user_agent"),
            {"user_agent": "TestBot/1.0"},
        )
        self.assertRedirects(
            response,
            reverse("site_admin:analytics_ignored_user_agents"),
        )
        self.assertFalse(
            UserAgentIgnore.objects.filter(user_agent="TestBot/1.0").exists()
        )

    def test_ignored_user_agents_export(self):
        self.client.force_login(self.staff)
        UserAgentIgnore.objects.create(user_agent="Alpha/1.0")
        UserAgentIgnore.objects.create(user_agent="Beta/2.0")

        response = self.client.get(
            reverse("site_admin:analytics_ignored_user_agents_export")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        content = response.content.decode("utf-8").strip().splitlines()
        self.assertEqual(content, ["Alpha/1.0", "Beta/2.0"])

    def test_errors_by_user_agent_counts_and_filters(self):
        self.client.force_login(self.staff)
        today = timezone.localdate()
        in_range = timezone.make_aware(datetime.combine(today, datetime.min.time()))

        self._create_visit(user_agent="ErrorBot/1.0", response_status_code=404, started_at=in_range)
        self._create_visit(user_agent="ErrorBot/1.0", response_status_code=404, started_at=in_range)
        self._create_visit(user_agent="ErrorBot/1.0", response_status_code=500, started_at=in_range)
        self._create_visit(user_agent="OtherBot/2.0", response_status_code=500, started_at=in_range)

        response = self.client.get(
            reverse("site_admin:analytics_errors_by_user_agent"),
            {
                "class": "4xx",
                "start": (today - timedelta(days=1)).isoformat(),
                "end": today.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        user_agent_counts = response.context["user_agent_counts"]
        self.assertEqual(len(user_agent_counts), 1)
        self.assertEqual(user_agent_counts[0]["user_agent"], "ErrorBot/1.0")
        self.assertEqual(user_agent_counts[0]["count"], 2)

        response = self.client.get(
            reverse("site_admin:analytics_errors_by_user_agent"),
            {
                "class": "5xx",
                "start": (today - timedelta(days=1)).isoformat(),
                "end": today.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        user_agent_counts = response.context["user_agent_counts"]
        self.assertEqual(len(user_agent_counts), 2)
        counts_by_agent = {row["user_agent"]: row["count"] for row in user_agent_counts}
        self.assertEqual(counts_by_agent["ErrorBot/1.0"], 1)
        self.assertEqual(counts_by_agent["OtherBot/2.0"], 1)

    def test_redirects_moved_to_analytics(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:redirect_list"))
        self.assertEqual(response.status_code, 200)

        response = self.client.get("/admin/settings/redirects/")
        self.assertEqual(response.status_code, 404)

    def test_ignore_user_agent_respects_next_target(self):
        self.client.force_login(self.staff)
        safe_next = reverse("site_admin:analytics_user_agents")
        response = self.client.post(
            reverse("site_admin:analytics_ignore_user_agent"),
            {"user_agent": "NextBot/1.0", "next": safe_next},
        )
        self.assertRedirects(response, safe_next)

        response = self.client.post(
            reverse("site_admin:analytics_ignore_user_agent"),
            {"user_agent": "BadNext/1.0", "next": "https://example.com/phish"},
        )
        self.assertRedirects(response, reverse("site_admin:analytics_dashboard"))

    def test_bulk_ignore_user_agents(self):
        self.client.force_login(self.staff)
        today = timezone.localdate()
        in_range = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        self._create_visit(user_agent="BotA/1.0", started_at=in_range)
        self._create_visit(user_agent="BotB/1.0", started_at=in_range)
        self._create_visit(user_agent="Mozilla/5.0", started_at=in_range)
        response = self.client.post(
            reverse("site_admin:analytics_ignore_user_agents_bulk"),
            {
                "q": "bot",
                "start": (today - timedelta(days=1)).isoformat(),
                "end": today.isoformat(),
                "ignore_all": "1",
            },
        )
        self.assertRedirects(
            response,
            reverse("site_admin:analytics_user_agents"),
        )
        self.assertTrue(UserAgentIgnore.objects.filter(user_agent="BotA/1.0").exists())
        self.assertTrue(UserAgentIgnore.objects.filter(user_agent="BotB/1.0").exists())
        self.assertFalse(
            UserAgentIgnore.objects.filter(user_agent="Mozilla/5.0").exists()
        )

    def test_bot_detection_rule_save_and_test(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:analytics_bot_detection"),
            {
                "action": "save_rule",
                "enabled": "1",
                "pattern": r"(?i)bot|crawler",
            },
        )
        self.assertRedirects(response, reverse("site_admin:analytics_bot_detection"))
        rule = UserAgentBotRule.get_current()
        self.assertTrue(rule.enabled)
        self.assertEqual(rule.pattern, r"(?i)bot|crawler")

        response = self.client.post(
            reverse("site_admin:analytics_bot_detection"),
            {
                "action": "test_rule",
                "enabled": "1",
                "pattern": r"(?i)bot|crawler",
                "test_user_agent": "CrawlerProbe/1.0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["test_result"])

    def test_mark_and_unmark_false_positive_user_agent(self):
        self.client.force_login(self.staff)
        UserAgentBotRule.objects.create(enabled=True, pattern=r"(?i)bot")
        visit = self._create_visit(user_agent="NoiseBot/1.0")
        Visit.objects.filter(id=visit.id).update(is_suspected_bot=True, suspected_bot_pattern_version=1)

        response = self.client.post(
            reverse("site_admin:analytics_mark_false_positive_user_agent"),
            {"user_agent": "NoiseBot/1.0"},
        )
        self.assertRedirects(response, reverse("site_admin:analytics_bot_detection"))
        self.assertTrue(
            UserAgentFalsePositive.objects.filter(user_agent="NoiseBot/1.0").exists()
        )
        visit.refresh_from_db()
        self.assertFalse(visit.is_suspected_bot)

        response = self.client.post(
            reverse("site_admin:analytics_unmark_false_positive_user_agent"),
            {"user_agent": "NoiseBot/1.0"},
        )
        self.assertRedirects(response, reverse("site_admin:analytics_bot_detection"))
        self.assertFalse(
            UserAgentFalsePositive.objects.filter(user_agent="NoiseBot/1.0").exists()
        )
        visit.refresh_from_db()
        self.assertTrue(visit.is_suspected_bot)

    def test_htmx_redirect_does_not_consume_messages(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:analytics_unignore_user_agent"),
            {},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn("HX-Trigger", response.headers)

        follow_response = self.client.get(reverse("site_admin:analytics_ignored_user_agents"))
        self.assertContains(follow_response, "Provide a user agent to unignore.")


class SiteAdminWidgetTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="widget-editor",
            email="widgets@example.com",
            password="password",
            is_staff=True,
        )

    def test_widget_list_prefers_configured_title(self):
        self.client.force_login(self.staff)
        WidgetInstance.objects.create(
            widget_type="text",
            area="footer",
            order=1,
            config={"title": "IndieWeb WebRing"},
        )

        response = self.client.get(reverse("site_admin:widget_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IndieWeb WebRing")
        self.assertContains(response, "Text / HTML Block")
        self.assertNotContains(response, ">text<", html=False)

    def test_widget_list_falls_back_to_widget_label_without_title(self):
        self.client.force_login(self.staff)
        WidgetInstance.objects.create(
            widget_type="recent_posts",
            area="footer",
            order=2,
            config={},
        )

        response = self.client.get(reverse("site_admin:widget_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recent Posts")
        self.assertNotContains(response, ">recent_posts<", html=False)


class SiteAdminPostTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_photo_post_requires_caption_or_photo(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:post_create"),
            {
                "kind": Post.PHOTO,
                "content": "",
                "title": "",
                "slug": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn(
            "Add a caption or at least one photo for photo posts.",
            form.non_field_errors(),
        )
        self.assertEqual(Post.objects.count(), 0)

    def test_like_post_auto_fills_content(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:post_create"),
            {
                "kind": Post.LIKE,
                "like_of": "https://example.com",
                "content": "",
                "title": "",
                "slug": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        post = Post.objects.get()
        self.assertEqual(post.content, "Liked https://example.com")
        self.assertIsNotNone(post.published_on)

    def test_post_create_queues_webmentions_on_commit(self):
        self.client.force_login(self.staff)
        with (
            mock.patch("site_admin.views.queue_webmentions_for_post") as queue_mock,
            mock.patch("site_admin.views.transaction.on_commit") as on_commit_mock,
            mock.patch("micropub.webmention.send_webmentions_for_post") as send_mock,
        ):
            response = self.client.post(
                reverse("site_admin:post_create"),
                {
                    "kind": Post.NOTE,
                    "content": "Queued webmentions",
                    "title": "",
                    "slug": "",
                },
            )

            self.assertEqual(response.status_code, 302)
            on_commit_mock.assert_called_once()
            queue_mock.assert_not_called()
            send_mock.assert_not_called()

            callback = on_commit_mock.call_args.args[0]
            callback()
            queue_mock.assert_called_once()

            args, kwargs = queue_mock.call_args
            self.assertIsInstance(args[0], Post)
            self.assertTrue(args[1].endswith(args[0].get_absolute_url()))
            self.assertTrue(kwargs.get("include_bridgy"))
            self.assertIn("settings_obj", kwargs)

    def test_post_form_renders_single_in_reply_to_field(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:post_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="in_reply_to"', count=1)

    def test_rsvp_post_uses_in_reply_to_as_event_url(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:post_create"),
            {
                "kind": Post.RSVP,
                "rsvp_value": "yes",
                "in_reply_to": "https://events.example.com/meetup",
                "content": "",
                "title": "",
                "slug": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        post = Post.objects.get()
        self.assertEqual(post.kind, Post.RSVP)
        self.assertEqual(post.content, "RSVP yes to https://events.example.com/meetup")


class SiteAdminProfilePhotoTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_profile_upload_and_delete_photo(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "profile.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                response = self.client.post(
                    reverse("site_admin:profile_upload_photo"),
                    {"photo": upload},
                )

                self.assertEqual(response.status_code, 200)
                payload = json.loads(response.content.decode())
                asset_id = payload["id"]

                self.assertTrue(File.objects.filter(id=asset_id).exists())

                response = self.client.post(
                    reverse("site_admin:profile_delete_photo"),
                    {"id": asset_id},
                )

                self.assertEqual(response.status_code, 200)
                payload = json.loads(response.content.decode())
                self.assertEqual(payload["status"], "deleted")
                self.assertFalse(File.objects.filter(id=asset_id).exists())

    def test_profile_delete_photo_blocks_in_use_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "profile.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                hcard = HCard.objects.create(user=self.staff, name="Editor")
                HCardPhoto.objects.create(
                    hcard=hcard,
                    asset=asset,
                    value=asset.file.url,
                    sort_order=0,
                )

                response = self.client.post(
                    reverse("site_admin:profile_delete_photo"),
                    {"id": asset.id},
                )

                self.assertEqual(response.status_code, 409)
                payload = json.loads(response.content.decode())
                self.assertEqual(
                    payload["error"],
                    "File is still used in a profile photo.",
                )
                self.assertTrue(File.objects.filter(id=asset.id).exists())


class SiteAdminProfileEditTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def _formset_management_data(self, prefix, *, total=0, initial=0):
        return {
            f"{prefix}-TOTAL_FORMS": str(total),
            f"{prefix}-INITIAL_FORMS": str(initial),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }

    def test_profile_edit_saves_photos(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "profile.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )

                data = {
                    "name": "Editor",
                    "uid": "https://example.com/",
                    "uploaded_ids": str(asset.id),
                    "uploaded_positions": "0",
                    **self._formset_management_data("urls"),
                    **self._formset_management_data("emails"),
                }
                response = self.client.post(reverse("site_admin:profile_edit"), data)

                self.assertRedirects(response, reverse("site_admin:profile_edit"))
                hcard = HCard.objects.get(user=self.staff)
                self.assertTrue(
                    HCardPhoto.objects.filter(hcard=hcard, asset=asset).exists()
                )

    def test_site_settings_success_uses_django_messages(self):
        self.client.force_login(self.staff)
        main_menu = Menu.objects.create(title="Main")
        footer_menu = Menu.objects.create(title="Footer")
        response = self.client.post(
            reverse("site_admin:site_settings"),
            {
                "title": "Updated title",
                "main_menu": str(main_menu.id),
                "footer_menu": str(footer_menu.id),
                "default_feed_kinds": "article,note",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Settings saved.")

    def test_profile_edit_reports_photo_sync_errors(self):
        self.client.force_login(self.staff)
        data = {
            "name": "Editor",
            "uid": "https://example.com/",
            **self._formset_management_data("urls"),
            **self._formset_management_data("emails"),
        }

        with (
            mock.patch(
                "site_admin.views._sync_profile_photos", side_effect=Exception("boom")
            ),
            mock.patch("site_admin.views.logger") as logger_mock,
        ):
            response = self.client.post(reverse("site_admin:profile_edit"), data)

        self.assertEqual(response.status_code, 200)
        logger_mock.exception.assert_called_once()
        self.assertIn(
            "Unable to save your profile right now. Please try again.",
            response.context["form"].non_field_errors(),
        )


class SiteAdminFileDeleteTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_file_delete_removes_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "asset.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )

                response = self.client.post(
                    reverse("site_admin:file_delete", kwargs={"file_id": asset.id})
                )

                self.assertRedirects(response, reverse("site_admin:file_list"))
                self.assertFalse(File.objects.filter(id=asset.id).exists())

    def test_file_delete_blocks_in_use_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "asset.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                post = Post.objects.create(
                    title="Post A",
                    slug="post-a",
                    kind=Post.ARTICLE,
                    content="Hello A",
                    published_on=timezone.now(),
                )
                Attachment.objects.create(
                    content_object=post,
                    asset=asset,
                    role="photo",
                )

                response = self.client.post(
                    reverse("site_admin:file_delete", kwargs={"file_id": asset.id})
                )

                self.assertContains(
                    response,
                    "File is still attached to content.",
                    status_code=409,
                )
                self.assertContains(response, post.title, status_code=409)
                self.assertTrue(File.objects.filter(id=asset.id).exists())

    def test_delete_post_photo_blocks_in_use_asset(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "post.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                post = Post.objects.create(
                    title="Post",
                    slug="post",
                    kind=Post.ARTICLE,
                    content="Hello",
                    published_on=timezone.now(),
                )
                Attachment.objects.create(
                    content_object=post,
                    asset=asset,
                    role="photo",
                )

                response = self.client.post(
                    reverse("site_admin:post_delete_photo"),
                    {"id": asset.id},
                )

                self.assertEqual(response.status_code, 409)
                payload = json.loads(response.content.decode())
                self.assertEqual(
                    payload["error"],
                    "File is still attached to content.",
                )
                self.assertTrue(File.objects.filter(id=asset.id).exists())

    def test_post_edit_removes_attachment_without_deleting_shared_file(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                upload = SimpleUploadedFile(
                    "post.jpg",
                    b"fake-image-data",
                    content_type="image/jpeg",
                )
                asset = File.objects.create(
                    kind=File.IMAGE,
                    file=upload,
                    owner=self.staff,
                )
                post_a = Post.objects.create(
                    title="Post A",
                    slug="post-a",
                    kind=Post.ARTICLE,
                    content="Hello A",
                    published_on=timezone.now(),
                )
                post_b = Post.objects.create(
                    title="Post B",
                    slug="post-b",
                    kind=Post.ARTICLE,
                    content="Hello B",
                    published_on=timezone.now(),
                )
                Attachment.objects.create(
                    content_object=post_a,
                    asset=asset,
                    role="photo",
                )
                Attachment.objects.create(
                    content_object=post_b,
                    asset=asset,
                    role="photo",
                )

                response = self.client.post(
                    reverse("site_admin:post_edit", kwargs={"slug": post_a.slug}),
                    {
                        "title": post_a.title,
                        "slug": post_a.slug,
                        "kind": post_a.kind,
                        "content": post_a.content,
                        "published_on": timezone.now().strftime("%Y-%m-%dT%H:%M"),
                        "existing_remove_ids": [str(asset.id)],
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertTrue(File.objects.filter(id=asset.id).exists())
                self.assertEqual(
                    Attachment.objects.filter(asset=asset).count(),
                    1,
                )


class SiteAdminThemeFileTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_theme_file_edit_rejects_disallowed_extension(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as themes_root:
            with override_settings(THEMES_ROOT=themes_root):
                build_test_theme("demo", themes_root)
                response = self.client.post(
                    reverse("site_admin:theme_file_edit", kwargs={"slug": "demo"}),
                    {
                        "theme": "demo",
                        "path": "templates/base.html",
                        "content": "test",
                        "new_entry_name": "bad.exe",
                        "new_file": "1",
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertFalse(
                    (Path(themes_root) / "demo" / "templates" / "bad.exe").exists()
                )


class SiteAdminThemeInstallTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="theme-admin",
            email="theme-admin@example.com",
            password="password",
            is_staff=True,
        )

    def test_theme_install_from_git_validates_form(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:theme_settings"),
            {
                "action": "install_git",
                "git_url": "not-a-url",
                "slug": "demo",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["git_form"].errors)

    def test_theme_install_from_git_invokes_helper(self):
        self.client.force_login(self.staff)
        theme = ThemeDefinition(slug="demo", path=Path("/tmp/demo"), label="Demo")
        with mock.patch("site_admin.views.install_theme_from_git", return_value=theme) as install:
            response = self.client.post(
                reverse("site_admin:theme_settings"),
                {
                    "action": "install_git",
                    "git_url": "https://example.com/demo.git",
                    "ref": "main",
                    "slug": "demo",
                },
            )

        install.assert_called_once_with("https://example.com/demo.git", "demo", ref="main")
        self.assertEqual(response.status_code, 302)

    def test_theme_update_from_git_invokes_helper(self):
        self.client.force_login(self.staff)
        install = ThemeInstall.objects.create(
            slug="demo",
            source_type=ThemeInstall.SOURCE_GIT,
            source_url="https://example.com/demo.git",
            source_ref="main",
        )
        result = ThemeUpdateResult(slug="demo", ref="main", commit="abc123", updated=True)
        with mock.patch("site_admin.views.update_theme_from_git", return_value=result) as update:
            response = self.client.post(
                reverse("site_admin:theme_install_detail", kwargs={"slug": install.slug}),
                {"ref": "main"},
            )

        update.assert_called_once_with(install, ref="main")
        self.assertEqual(response.status_code, 302)

    def test_theme_settings_save_persists_values(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as themes_root:
            with override_settings(THEMES_ROOT=themes_root):
                metadata = {
                    "label": "Demo",
                    "slug": "demo",
                    "settings": {
                        "fields": {
                            "accent_color": {"type": "color", "default": "#111111"},
                            "show_banner": {"type": "boolean", "default": True},
                        }
                    },
                }
                build_test_theme("demo", themes_root, metadata=metadata)
                settings_obj = SiteConfiguration.get_solo()
                settings_obj.active_theme = "demo"
                settings_obj.save()

                response = self.client.post(
                    reverse("site_admin:theme_settings"),
                    {
                        "action": "save_theme_settings",
                        "accent_color": "#222222",
                        "show_banner": "on",
                    },
                )

        self.assertEqual(response.status_code, 302)
        settings_obj.refresh_from_db()
        self.assertEqual(
            settings_obj.theme_settings.get("demo"),
            {"accent_color": "#222222", "show_banner": True},
        )

    def test_theme_storage_healthcheck_defaults_read_only(self):
        self.client.force_login(self.staff)
        result = {
            "ok": True,
            "read_ok": True,
            "write_ok": False,
            "write_test": False,
            "errors": [],
        }
        with mock.patch("site_admin.views.theme_storage_healthcheck", return_value=result) as healthcheck:
            response = self.client.post(
                reverse("site_admin:theme_settings"),
                {"action": "theme_storage_healthcheck"},
            )

        healthcheck.assert_called_once_with(write_test=False)
        self.assertEqual(response.status_code, 302)
        messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(any("healthcheck" in message.lower() for message in messages))


class SiteAdminWebmentionModerationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_approve_pending_webmention(self):
        mention = Webmention.objects.create(
            source="https://source.example",
            target="https://testserver/blog/post/hello/",
            status=Webmention.PENDING,
        )
        response = self.client.post(
            reverse("site_admin:webmention_approve", kwargs={"mention_id": mention.id})
        )

        self.assertEqual(response.status_code, 302)
        mention.refresh_from_db()
        self.assertEqual(mention.status, Webmention.ACCEPTED)

    def test_reject_pending_webmention(self):
        mention = Webmention.objects.create(
            source="https://source.example",
            target="https://testserver/blog/post/hello/",
            status=Webmention.PENDING,
        )
        response = self.client.post(
            reverse("site_admin:webmention_reject", kwargs={"mention_id": mention.id})
        )

        self.assertEqual(response.status_code, 302)
        mention.refresh_from_db()
        self.assertEqual(mention.status, Webmention.REJECTED)

    def test_pending_outgoing_webmention_cannot_be_moderated(self):
        mention = Webmention.objects.create(
            source="https://testserver/blog/post/hello/",
            target="https://external.example/post/1/",
            status=Webmention.PENDING,
        )
        response = self.client.get(
            reverse("site_admin:webmention_detail", kwargs={"mention_id": mention.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_moderate"])

    def test_delete_webmention_message_bootstraps_on_list_page(self):
        mention = Webmention.objects.create(
            source="https://source.example/post",
            target="https://testserver/blog/post/hello/",
            status=Webmention.REJECTED,
            is_incoming=True,
        )
        response = self.client.post(
            reverse("site_admin:webmention_delete", kwargs={"mention_id": mention.id}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request["PATH_INFO"], reverse("site_admin:webmention_list"))
        self.assertContains(response, "Webmention deleted.")


class SiteAdminCommentModerationTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )
        self.post = Post.objects.create(
            title="Comment Post",
            slug="comment-post",
            content="text",
            published_on=timezone.now(),
        )

    def test_comment_approve_marks_status(self):
        self.client.force_login(self.staff)
        comment = Comment.objects.create(
            post=self.post,
            author_name="Ada",
            content="Hello",
            status=Comment.PENDING,
        )

        with mock.patch("site_admin.views.submit_ham") as submit_ham:
            response = self.client.post(
                reverse("site_admin:comment_approve", kwargs={"comment_id": comment.id})
            )

        self.assertEqual(response.status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.status, Comment.APPROVED)
        submit_ham.assert_called_once()

    def test_comment_mark_spam_updates_status(self):
        self.client.force_login(self.staff)
        comment = Comment.objects.create(
            post=self.post,
            author_name="Ada",
            content="Hello",
            status=Comment.PENDING,
        )

        with mock.patch("site_admin.views.submit_spam") as submit_spam:
            response = self.client.post(
                reverse("site_admin:comment_mark_spam", kwargs={"comment_id": comment.id})
            )

        self.assertEqual(response.status_code, 302)
        comment.refresh_from_db()
        self.assertEqual(comment.status, Comment.SPAM)
        submit_spam.assert_called_once()


class SiteAdminErrorLogTests(TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def test_error_log_list_filters(self):
        self.client.force_login(self.staff)
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.developer_tools_enabled = True
        settings_obj.save()
        micropub_log = RequestErrorLog.objects.create(
            source=RequestErrorLog.SOURCE_MICROPUB,
            method="POST",
            path="/micropub",
            status_code=400,
            error="invalid_request",
            request_headers={},
            request_query={},
            request_body="access_token=bad",
            response_body="",
        )
        indieauth_log = RequestErrorLog.objects.create(
            source=RequestErrorLog.SOURCE_INDIEAUTH,
            method="POST",
            path="/indieauth/token",
            status_code=401,
            error="invalid_token",
            request_headers={},
            request_query={},
            request_body="",
            response_body="bad token",
        )

        response = self.client.get(
            reverse("site_admin:error_log_list"),
            {"source": "micropub", "status_code": "400", "q": "invalid_request"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"].object_list), [micropub_log])

        response = self.client.get(
            reverse("site_admin:error_log_list"),
            {"q": "bad token"},
        )
        self.assertEqual(list(response.context["page_obj"].object_list), [indieauth_log])

    def test_error_log_list_orders_by_created_at(self):
        self.client.force_login(self.staff)
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.developer_tools_enabled = True
        settings_obj.save()
        older_log = RequestErrorLog.objects.create(
            source=RequestErrorLog.SOURCE_MICROPUB,
            method="POST",
            path="/micropub",
            status_code=415,
            error="unsupported",
            request_headers={},
            request_query={},
            request_body="",
            response_body="",
        )
        newer_log = RequestErrorLog.objects.create(
            source=RequestErrorLog.SOURCE_INDIEAUTH,
            method="POST",
            path="/indieauth/token",
            status_code=400,
            error="invalid_request",
            request_headers={},
            request_query={},
            request_body="",
            response_body="",
        )

        RequestErrorLog.objects.filter(pk=older_log.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )

        response = self.client.get(reverse("site_admin:error_log_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["page_obj"].object_list)[:2], [newer_log, older_log])

    def test_error_log_detail_view(self):
        self.client.force_login(self.staff)
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.developer_tools_enabled = True
        settings_obj.save()
        log_entry = RequestErrorLog.objects.create(
            source=RequestErrorLog.SOURCE_MICROPUB,
            method="POST",
            path="/micropub",
            status_code=400,
            error="invalid_request",
            request_headers={"Content-Type": "application/json"},
            request_query={"q": ["value"]},
            request_body="{\"name\":\"test\"}",
            response_body="{\"error\":\"invalid_request\"}",
        )

        response = self.client.get(
            reverse("site_admin:error_log_detail", kwargs={"log_id": log_entry.id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid_request")


class SiteAdminIndieAuthSettingsTests(TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="reader",
            email="reader@example.com",
            password="password",
        )
        self.staff = get_user_model().objects.create_user(
            username="editor",
            email="editor@example.com",
            password="password",
            is_staff=True,
        )

    def _create_client(self):
        return IndieAuthClient.objects.create(
            client_id="https://app.example.com",
            name="Example App",
            redirect_uris=["https://app.example.com/callback"],
        )

    def _create_token(self, client_id, user, token_hash="a" * 64):
        return IndieAuthAccessToken.objects.create(
            token_hash=token_hash,
            client_id=client_id,
            me="https://example.com",
            scope="read write",
            user=user,
            expires_at=timezone.now() + timedelta(days=7),
        )

    def test_indieauth_settings_requires_staff(self):
        response = self.client.get(reverse("site_admin:indieauth_settings"))

        self.assertRedirects(
            response,
            f"{reverse('site_admin:login')}?next={reverse('site_admin:indieauth_settings')}",
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("site_admin:indieauth_settings"))

        self.assertEqual(response.status_code, 403)

    def test_indieauth_settings_accessible_without_dev_tools(self):
        settings_obj = SiteConfiguration.get_solo()
        settings_obj.developer_tools_enabled = False
        settings_obj.save()

        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:indieauth_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IndieAuth")

    def test_indieauth_settings_redacts_token_hash(self):
        client = self._create_client()
        token_hash = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcd"
        self._create_token(client.client_id, self.staff, token_hash=token_hash)

        self.client.force_login(self.staff)
        response = self.client.get(reverse("site_admin:indieauth_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, token_hash)
        redacted = f"{token_hash[:6]}...{token_hash[-4:]}"
        self.assertContains(response, redacted)

    def test_indieauth_settings_revoke_token(self):
        client = self._create_client()
        token = self._create_token(client.client_id, self.staff)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:indieauth_settings"),
            {"action": "revoke_token", "token_id": token.id},
        )

        self.assertEqual(response.status_code, 302)
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)

    def test_indieauth_settings_delete_client(self):
        client = self._create_client()
        token = self._create_token(client.client_id, self.staff)
        consent = IndieAuthConsent.objects.create(
            user=self.staff,
            client_id=client.client_id,
            scope="read",
        )
        code = IndieAuthAuthorizationCode.objects.create(
            code_hash="b" * 64,
            code_challenge="challenge",
            code_challenge_method="S256",
            client_id=client.client_id,
            redirect_uri="https://app.example.com/callback",
            me="https://example.com",
            scope="read",
            user=self.staff,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("site_admin:indieauth_settings"),
            {"action": "delete_client", "client_pk": client.id},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(IndieAuthClient.objects.filter(pk=client.id).exists())
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        self.assertFalse(IndieAuthConsent.objects.filter(pk=consent.id).exists())
        self.assertFalse(IndieAuthAuthorizationCode.objects.filter(pk=code.id).exists())

    def test_indieauth_client_detail_lists_tokens_and_consents(self):
        client = self._create_client()
        token_hash = "c" * 64
        self._create_token(client.client_id, self.staff, token_hash=token_hash)
        IndieAuthConsent.objects.create(
            user=self.staff,
            client_id=client.client_id,
            scope="read",
        )

        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("site_admin:indieauth_client_detail", kwargs={"client_pk": client.id})
        )

        self.assertEqual(response.status_code, 200)
        redacted = f"{token_hash[:6]}...{token_hash[-4:]}"
        self.assertContains(response, redacted)
        self.assertContains(response, self.staff.get_username())
