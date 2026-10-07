from django.conf import settings


def site(request):
    context = {"site_name": settings.SITE_NAME, "dev_login": settings.DEV_LOGIN, "registration_open": settings.REGISTRATION_OPEN}
    if getattr(request, "user", None) and request.user.is_authenticated:
        from friends.models import FriendRequest

        # Imported titles waiting to be confirmed (Manage tab), and friend requests to answer (Friends tab).
        context["pending_count"] = request.user.pending.count()
        context["friend_requests"] = FriendRequest.objects.filter(
            to_email__iexact=request.user.email, from_user__is_active=True).count()
    return context


def site_url():
    """Where the app is reached, for links in emails: SITE_URL, else the first trusted origin."""
    return (settings.SITE_URL or next(iter(settings.CSRF_TRUSTED_ORIGINS), "")).rstrip("/")
