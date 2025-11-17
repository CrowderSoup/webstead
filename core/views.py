import markdown

from django.shortcuts import render, get_object_or_404

from .models import Page, Elsewhere
from blog.models import Post

def index(request):
    recent_blog_posts = Post.objects.filter(kind=Post.ARTICLE).exclude(published_on__isnull=True).order_by('-published_on')[:5]
    elsewhere = Elsewhere.objects.all()

    return render(request, 'core/index.html', { "recent_posts": recent_blog_posts, "elsewhere": elsewhere })

def page(request, slug):
    page = get_object_or_404(
        Page.objects.only("title", "content", "slug", "published_on"),
        slug=slug,
    )

    return render(request, 'core/page.html', { "page": page })
