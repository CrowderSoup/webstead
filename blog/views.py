import markdown

from django.shortcuts import render, get_object_or_404

from .models import Post

def posts(request):
    posts = Post.objects.all()

    return render(request, 'blog/posts.html', { "posts": posts })

def post(request, slug):
    post = get_object_or_404(
        Post.objects.only("title", "content", "slug", "published_on"),
        slug=slug,
    )

    return render(request, 'blog/post.html', { "post": post })
