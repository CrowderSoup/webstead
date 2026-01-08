from django.urls import path

from .feeds import PostsFeed
from . import views

urlpatterns = [
    path("post/<slug:slug>/delete/", views.delete_post, name="delete_post"),
    path("", views.posts, name="posts"),
    path("post/<slug:slug>/", views.post, name="post"),
    path("tag/<slug:tag>", views.posts_by_tag, name="posts_by_tag"),
    path("tags/suggest/", views.tag_suggestions, name="tag_suggestions"),
    path("feed/", PostsFeed(), name="posts_feed"),
]
