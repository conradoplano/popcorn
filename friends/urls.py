from django.urls import path, re_path

from . import views

app_name = "friends"

urlpatterns = [
    path("friends/", views.friends, name="friends"),
    path("friends/add/", views.friend_add, name="add"),
    path("friends/sharing/", views.sharing, name="sharing"),
    path("friends/new-link/", views.new_invite_link, name="new_link"),
    path("friends/requests/<int:pk>/accept/", views.request_accept, name="accept"),
    path("friends/requests/<int:pk>/decline/", views.request_decline, name="decline"),
    path("friends/<int:pk>/remove/", views.friend_remove, name="remove"),
    re_path(r"^friends/(?P<pk>\d+)/(?P<kind>movies|shows)/$", views.friend_list, name="friend"),
    path("join/<str:token>/", views.join, name="join"),
    re_path(r"^p/(?P<token>[\w-]+)/(?P<kind>movies|shows)/$", views.public_list, name="public"),
]
