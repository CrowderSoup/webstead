from django.test import TestCase
from django.utils import timezone

from .models import Post, Tag


class TagModelTests(TestCase):
    def test_string_representation(self):
        tag = Tag.objects.create(tag="django")
        self.assertEqual(str(tag), "django")


class PostModelTests(TestCase):
    def test_html_renders_markdown(self):
        post = Post.objects.create(
            title="Markdown post",
            slug="markdown-post",
            content="**bold** text",
        )

        rendered = post.html()

        self.assertIn("<strong>bold</strong>", rendered)

    def test_summary_truncates_and_strips_markdown(self):
        content = "**markdown** " + ("body " * 200)
        post = Post.objects.create(
            title="Summary",
            slug="summary",
            content=content,
        )

        summary = post.summary()

        self.assertTrue(summary.endswith("..."))
        self.assertNotIn("**", summary)
        self.assertLessEqual(len(summary), 503)

    def test_is_published_flag(self):
        post = Post.objects.create(
            title="Draft",
            slug="draft",
            content="text",
        )
        self.assertFalse(post.is_published())

        post.published_on = timezone.now()
        self.assertTrue(post.is_published())

    def test_slug_auto_generated_from_title(self):
        post = Post(title="Hello World", content="text")
        post.save()

        self.assertTrue(post.slug.startswith("hello-world-"))
        suffix = post.slug.split("hello-world-", 1)[1]
        self.assertTrue(suffix.isdigit())

    def test_slug_defaults_to_page_when_title_blank(self):
        Post.objects.create(title="Existing", slug="page", content="content", published_on=timezone.now())

        post = Post(title="", content="text")
        post.save()

        self.assertTrue(post.slug.startswith("article-"))
        suffix = post.slug.split("article-", 1)[1]
        self.assertTrue(suffix.isdigit())

