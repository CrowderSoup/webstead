from django.urls import path

from . import views

urlpatterns = [
    path("micropub", views.MicropubView.as_view(), name="micropub-endpoint"),
    path("micropub/media", views.MicropubMediaView.as_view(), name="micropub-media"),
    path("indieauth/login", views.IndieAuthLoginView.as_view(), name="indieauth-login"),
    path("indieauth/callback", views.IndieAuthCallbackView.as_view(), name="indieauth-callback"),
    path("webmention/submit", views.WebmentionSubmitView.as_view(), name="webmention-submit"),
    path("webmention", views.WebmentionView.as_view(), name="webmention-endpoint"),
]
