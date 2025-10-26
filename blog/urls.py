from django.urls import path

from . import views

urlpatterns = [
    path("", views.posts, name="posts"),
    path("post/<slug:slug>/", views.post, name="post"),
    path("tag/<slug:tag>", views.posts_by_tag, name="posts_by_tag"),
]
