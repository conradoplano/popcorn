from django.contrib import admin

from .models import LoginCodeRequest, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "ai_approved", "is_active", "is_staff", "last_visit_at")
    list_filter = ("is_active", "is_staff", "ai_approved")
    search_fields = ("email", "name")
    readonly_fields = ("last_login", "last_visit_at", "news_since", "date_joined", "invite_token")
    fieldsets = (
        (None, {"fields": ("email", "name")}),
        ("Friends", {"fields": ("invite_token",)}),
        ("Where to watch", {"fields": ("watch_region", "services", "own_services")}),
        ("AI", {"fields": ("ai_approved", "ai_daily_limit")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Dates", {"fields": ("last_login", "last_visit_at", "news_since", "date_joined")}),
    )
    filter_horizontal = ("groups",)

    def save_model(self, request, obj, form, change):
        obj.email = obj.email.lower()
        if not change:
            obj.set_unusable_password()
        super().save_model(request, obj, form, change)


@admin.register(LoginCodeRequest)
class LoginCodeRequestAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at")
    search_fields = ("email",)
