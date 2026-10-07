from django.contrib import admin

from .models import AIUsage, Entry, Import, PendingItem, Recommendation, RecommendationSet, Tag


@admin.register(Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ("title", "year", "kind", "status", "rating", "tmdb_id", "user", "added_at")
    list_filter = ("kind", "status", "rating")
    search_fields = ("title", "user__email", "user__name")
    raw_id_fields = ("user",)
    filter_horizontal = ("tags",)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "emoji", "user")
    search_fields = ("name", "user__email")
    raw_id_fields = ("user",)


class PendingItemInline(admin.TabularInline):
    model = PendingItem
    fields = ("line", "kind", "title", "year", "tmdb_id", "sure")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(Import)
class ImportAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "status", "found", "used_ai")
    list_filter = ("status", "used_ai")
    readonly_fields = [f.name for f in Import._meta.fields]
    inlines = [PendingItemInline]


class RecommendationInline(admin.TabularInline):
    model = Recommendation
    fields = ("kind", "title", "year", "tmdb_id", "tag", "reason", "state")
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(RecommendationSet)
class RecommendationSetAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "status", "requested")
    list_filter = ("status", "requested")
    readonly_fields = [f.name for f in RecommendationSet._meta.fields]
    inlines = [RecommendationInline]


@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "model", "cost")
    list_filter = ("kind", "model")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AIUsage._meta.fields]
