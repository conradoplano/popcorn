from django.contrib import admin

from .models import LoginCodeRequest, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "name", "movies_public", "shows_public", "is_active", "is_staff", "last_login")
    list_filter = ("is_active", "is_staff", "movies_public", "shows_public")
    search_fields = ("email", "name")
    readonly_fields = ("last_login", "date_joined", "invite_token", "public_token")
    fieldsets = (
        (None, {"fields": ("email", "name")}),
        ("Sharing", {"fields": ("movies_public", "shows_public", "invite_token", "public_token")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
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
