from django.urls import path

from .feeds import PostsFeed
from . import views

urlpatterns = [
    path("editor/", views.post_editor, name="post_editor"),
    path("editor/<slug:slug>/", views.post_editor, name="post_editor_edit"),
    path("post-editor-sw.js", views.post_editor_service_worker, name="post_editor_sw"),
    path("post/<slug:slug>/delete/", views.delete_post, name="delete_post"),
    path("post/<slug:slug>/edit/", views.post_editor, name="edit_post"),
    path("", views.posts, name="posts"),
    path("post/<slug:slug>/", views.post, name="post"),
    path("tag/<slug:tag>", views.posts_by_tag, name="posts_by_tag"),
    path("feed/", PostsFeed(), name="posts_feed"),
]
