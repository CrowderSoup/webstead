from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("page/<slug:slug>/", views.page, name="page"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
    path("sitemap.xml", views.sitemap, name="sitemap"),
]
