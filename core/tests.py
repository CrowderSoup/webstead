from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Menu, MenuItem, Page, Redirect, SiteConfiguration
from blog.models import Post, Tag


class PageModelTests(TestCase):
    def test_html_renders_markdown(self):
        page = Page.objects.create(
            title="About",
            slug="about",
            content="**bold** text",
            published_on=timezone.now(),
        )

        rendered = page.html()

        self.assertIn("<strong>bold</strong>", rendered)

    def test_slug_generation_handles_duplicates(self):
        Page.objects.create(
            title="Contact",
            slug="contact",
            content="text",
            published_on=timezone.now(),
        )

        duplicate = Page(title="Contact", content="text", published_on=timezone.now())
        duplicate.save()

        self.assertEqual(duplicate.slug, "contact-2")


class MenuItemTests(TestCase):
    def test_items_are_ordered_by_weight(self):
        menu = Menu.objects.create(title="Main")
        second = MenuItem.objects.create(menu=menu, text="Second", url="/second", weight=10)
        first = MenuItem.objects.create(menu=menu, text="First", url="/first", weight=0)
        third = MenuItem.objects.create(menu=menu, text="Third", url="/third", weight=20)

        ordered_text = [item.text for item in MenuItem.objects.filter(menu=menu)]

        self.assertEqual(ordered_text, [first.text, second.text, third.text])


class RedirectMiddlewareTests(TestCase):
    def test_permanent_redirects_to_target_path(self):
        Redirect.objects.create(
            from_path="/old/",
            to_path="/new/",
            redirect_type=Redirect.PERMANENTLY,
        )

        response = self.client.get("/old/")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/new/")

    def test_temporary_redirect_uses_307(self):
        Redirect.objects.create(
            from_path="/temp/",
            to_path="/hot/",
            redirect_type=Redirect.TEMPORARY,
        )

        response = self.client.get("/temp/")

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response["Location"], "/hot/")


class RobotsTxtTests(TestCase):
    def test_returns_configured_content(self):
        settings = SiteConfiguration.get_solo()
        settings.robots_txt = "User-agent: *\nDisallow: /admin/"
        settings.save()

        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertEqual(response.content.decode(), settings.robots_txt)


class SitemapTests(TestCase):
    def test_includes_public_routes_and_excludes_admin(self):
        page = Page.objects.create(
            title="About",
            slug="about",
            content="text",
            published_on=timezone.now(),
        )
        tag = Tag.objects.create(tag="news")
        post = Post.objects.create(
            title="Hello",
            slug="hello",
            content="text",
            kind=Post.ARTICLE,
            published_on=timezone.now(),
        )
        post.tags.add(tag)
        Post.objects.create(
            title="Draft",
            slug="draft",
            content="text",
            kind=Post.ARTICLE,
            published_on=None,
        )

        response = self.client.get("/sitemap.xml")

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/xml"))
        self.assertIn("http://testserver/", body)
        self.assertIn(f"http://testserver{reverse('posts')}", body)
        self.assertIn(f"http://testserver{reverse('page', kwargs={'slug': page.slug})}", body)
        self.assertIn(f"http://testserver{post.get_absolute_url()}", body)
        self.assertIn(f"http://testserver{reverse('posts_by_tag', kwargs={'tag': tag.tag})}", body)
        self.assertNotIn("/admin/", body)
