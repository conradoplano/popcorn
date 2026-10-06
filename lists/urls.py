from django.urls import path

from . import views

app_name = "lists"

urlpatterns = [
    path("", views.home, name="home"),
    path("movies/", views.movies, name="movies"),
    path("shows/", views.shows, name="shows"),
    path("entry/<int:pk>/", views.entry_edit, name="entry"),
    path("entry/<int:pk>/delete/", views.entry_delete, name="entry_delete"),
    path("entry/<int:pk>/rate/", views.entry_rate, name="entry_rate"),
    path("entry/<int:pk>/status/", views.entry_status, name="entry_status"),
    path("entry/<int:pk>/copy/", views.entry_copy, name="entry_copy"),
]
