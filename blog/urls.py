from django.urls import path

from .feeds import PostsFeed
from . import views

urlpatterns = [
    path("post/<slug:slug>/delete/", views.delete_post, name="delete_post"),
    path("post/<slug:slug>/flyover/", views.activity_flyover_status, name="activity_flyover_status"),
    path("", views.posts, name="posts"),
    path("post/<slug:slug>/", views.post, name="post"),
    path("tag/<slug:tag>", views.posts_by_tag, name="posts_by_tag"),
    path("feed/", PostsFeed(), name="posts_feed"),
]
