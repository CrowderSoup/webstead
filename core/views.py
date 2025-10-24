import markdown

from django.shortcuts import render, get_object_or_404

from .models import Page
from blog.models import Post

def index(request):
    recent_blog_posts = Post.objects.order_by('-published_on')[:5]

    return render(request, 'core/index.html', { "recent_posts": recent_blog_posts })

def page(request, slug):
    page = get_object_or_404(
        Page.objects.only("title", "content", "slug", "published_on"),
        slug=slug,
    )

    return render(request, 'core/page.html', { "page": page })
