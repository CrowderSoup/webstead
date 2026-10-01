import markdown

from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.templatetags.static import static
from django.urls import reverse

from .models import Page, SiteConfiguration
from .og import absolute_url, first_attachment_image_url, summarize_markdown
from blog.models import Post, Tag
from blog.views import build_posts_listing_context
from core.themes import get_active_theme_settings

def index(request):
    recent_blog_posts = (
        Post.objects.live()
        .filter(kind=Post.ARTICLE)
        .order_by("-published_on")[:5]
    )
    settings_obj = SiteConfiguration.get_solo()
    home_page = settings_obj.home_page if settings_obj.home_page_id else None
    theme_settings = get_active_theme_settings()
    home_feed_mode = theme_settings.get("home_feed_mode", "blog")
    feed_context = {}
    if home_feed_mode == "home":
        feed_context = build_posts_listing_context(request, include_og=False)

    response = render(
        request,
        "core/index.html",
        {
            "recent_posts": recent_blog_posts,
            "home_page": home_page,
            **feed_context,
        },
    )
    microsub_url = request.build_absolute_uri(reverse("microsub-endpoint"))
    existing_link = response.get("Link", "")
    microsub_link = f'<{microsub_url}>; rel="microsub"'
    response["Link"] = f"{existing_link}, {microsub_link}" if existing_link else microsub_link
    return response

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
    theme_settings = get_active_theme_settings()
    home_feed_mode = theme_settings.get("home_feed_mode", "blog")
    home_feed_redirect = theme_settings.get("home_feed_redirect")
    static_route_names = [
        "index",
        "posts_feed",
        "robots_txt",
        "sitemap",
        "micropub-endpoint",
        "micropub-media",
        "webmention-endpoint",
        "analytics-leave",
    ]
    if not (home_feed_mode == "home" and home_feed_redirect):
        static_route_names.insert(1, "posts")

    urls = set()
    for name in static_route_names:
        try:
            path = reverse(name)
        except Exception:
            continue
        urls.add(request.build_absolute_uri(path))

    pages = Page.objects.all()
    posts = Post.objects.live()
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


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


def server_error(request):
    return render(request, "500.html", status=500)
