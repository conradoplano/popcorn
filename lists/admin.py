from django.contrib import admin

from .models import Entry


@admin.register(Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ("title", "year", "kind", "status", "rating", "user", "added_at")
    list_filter = ("kind", "status", "rating")
    search_fields = ("title", "user__email", "user__name")
    raw_id_fields = ("user",)
