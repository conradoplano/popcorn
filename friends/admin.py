from django.contrib import admin

from .models import FriendRequest, Friendship


@admin.register(Friendship)
class FriendshipAdmin(admin.ModelAdmin):
    list_display = ("user", "friend", "created_at")
    search_fields = ("user__email", "friend__email")
    raw_id_fields = ("user", "friend")


@admin.register(FriendRequest)
class FriendRequestAdmin(admin.ModelAdmin):
    list_display = ("from_user", "to_email", "created_at")
    search_fields = ("from_user__email", "to_email")
    raw_id_fields = ("from_user",)
