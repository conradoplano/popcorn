from django.urls import path

from . import manage, views

app_name = "lists"

urlpatterns = [
    path("", views.home, name="home"),
    path("movies/", views.movies, name="movies"),
    path("shows/", views.shows, name="shows"),
    path("search/", views.search, name="search"),
    path("search/seasons/", views.seasons, name="seasons"),
    path("entry/<int:pk>/", views.entry_edit, name="entry"),
    path("entry/<int:pk>/delete/", views.entry_delete, name="entry_delete"),
    path("entry/<int:pk>/rate/", views.entry_rate, name="entry_rate"),
    path("entry/<int:pk>/status/", views.entry_status, name="entry_status"),
    path("entry/<int:pk>/copy/", views.entry_copy, name="entry_copy"),
    path("entry/<int:pk>/link/", views.entry_link, name="entry_link"),
    path("entry/<int:pk>/quick/", views.entry_quick, name="entry_quick"),
    path("entry/<int:pk>/refresh/", views.entry_refresh, name="entry_refresh"),
    path("recommendations/new/", views.recommendations_new, name="recommendations_new"),
    path("recommendations/<int:pk>/status/", views.recommendations_status, name="recommendations_status"),
    path("recommendations/item/<int:pk>/add/", views.recommendation_add, name="recommendation_add"),
    path("recommendations/item/<int:pk>/dismiss/", views.recommendation_dismiss, name="recommendation_dismiss"),
    path("manage/", manage.manage, name="manage"),
    path("manage/import/<int:pk>/status/", manage.import_status, name="import_status"),
    path("manage/pending/<int:pk>/", manage.pending_update, name="pending_update"),
    path("manage/pending/<int:pk>/pick/", manage.pending_pick, name="pending_pick"),
    path("manage/pending/<int:pk>/discard/", manage.pending_discard, name="pending_discard"),
    path("manage/pending/all/", manage.pending_all, name="pending_all"),
    path("manage/groups/", manage.tag_add, name="tag_add"),
    path("manage/groups/<int:pk>/", manage.tag_update, name="tag_update"),
    path("manage/groups/<int:pk>/delete/", manage.tag_delete, name="tag_delete"),
    path("manage/watch/", manage.watch_settings, name="watch_settings"),
    path("manage/services/", manage.own_service_add, name="own_service_add"),
    path("manage/services/remove/", manage.own_service_remove, name="own_service_remove"),
    path("manage/refresh/", manage.refresh_all, name="refresh_all"),
]
