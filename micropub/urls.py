from django.urls import path

from . import views

urlpatterns = [
    path("micropub", views.MicropubView.as_view(), name="micropub-endpoint"),
    path("micropub/media", views.MicropubMediaView.as_view(), name="micropub-media"),
    path("webmention", views.WebmentionView.as_view(), name="webmention-endpoint"),
]
