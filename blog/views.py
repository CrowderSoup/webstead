import markdown

from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.shortcuts import render, get_object_or_404

from .models import Post, Tag

def posts(request):
    query_set = Post.objects.exclude(published_on__isnull=True).order_by("-published_on")
    paginator = Paginator(query_set, 10)
    page_number = request.GET.get("page")

    try:
        posts = paginator.page(page_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts = paginator.page(paginator.num_pages)

    return render(request, 'blog/posts.html', { "posts": posts })

def posts_by_tag(request, tag):
    tag = get_object_or_404(Tag, tag=tag)
    query_set = Post.objects.exclude(published_on__isnull=True).filter(tags=tag).order_by("-published_on")
    paginator = Paginator(query_set, 10)
    page_number = request.GET.get("page")

    try:
        posts = paginator.page(page_number)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        posts.paginator.page(paginator.num_pages)

    return render(request, 'blog/posts_by_tag.html', { "posts": posts, "tag": tag })

def post(request, slug):
    post = get_object_or_404(
        Post.objects.only("title", "content", "slug", "published_on", "tags"),
        slug=slug,
    )

    tags = post.tags.all()

    return render(request, 'blog/post.html', { "post": post })
