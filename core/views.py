import markdown

from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.templatetags.static import static
from django.urls import reverse

from .models import Page, SiteConfiguration
from .og import absolute_url, first_attachment_image_url, summarize_markdown
from blog.models import Post, Tag

def index(request):
    recent_blog_posts = Post.objects.filter(kind=Post.ARTICLE).exclude(published_on__isnull=True).order_by('-published_on')[:5]

    return render(request, 'core/index.html', { "recent_posts": recent_blog_posts })

def page(request, slug):
    page = get_object_or_404(
        Page.objects.select_related("author").prefetch_related("author__hcards", "attachments__asset"),
        slug=slug,
    )
    og_image, og_image_alt = first_attachment_image_url(page.attachments.all())

    return render(
        request,
        "core/page.html",
        {
            "page": page,
            "og_title": page.title,
            "og_description": summarize_markdown(page.content, length=200),
            "og_image": absolute_url(request, og_image),
            "og_image_alt": og_image_alt or page.title,
            "og_url": request.build_absolute_uri(reverse("page", kwargs={"slug": page.slug})),
            "og_type": "article",
        },
    )


def robots_txt(request):
    config = SiteConfiguration.get_solo()
    return HttpResponse(config.robots_txt, content_type="text/plain")


def favicon(request):
    config = SiteConfiguration.get_solo()
    if config.favicon_id and config.favicon and config.favicon.file:
        return redirect(config.favicon.file.url)
    return redirect(static("favicon.svg"))


def sitemap(request):
    static_route_names = [
        "index",
        "posts",
        "posts_feed",
        "robots_txt",
        "sitemap",
        "micropub-endpoint",
        "micropub-media",
        "webmention-endpoint",
        "analytics-leave",
    ]

    urls = set()
    for name in static_route_names:
        try:
            path = reverse(name)
        except Exception:
            continue
        urls.add(request.build_absolute_uri(path))

    pages = Page.objects.all()
    posts = Post.objects.exclude(published_on__isnull=True).filter(deleted=False)
    tags = Tag.objects.all()

    for page in pages:
        urls.add(request.build_absolute_uri(reverse("page", kwargs={"slug": page.slug})))

    for post in posts:
        urls.add(request.build_absolute_uri(post.get_absolute_url()))

    for tag in tags:
        urls.add(request.build_absolute_uri(reverse("posts_by_tag", kwargs={"tag": tag.tag})))

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in sorted(urls):
        xml_lines.append("  <url>")
        xml_lines.append(f"    <loc>{url}</loc>")
        xml_lines.append("  </url>")
    xml_lines.append("</urlset>")

    return HttpResponse("\n".join(xml_lines), content_type="application/xml")


def server_error(request):
    return render(request, "500.html", status=500)
