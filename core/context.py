from django.conf import settings


def site(request):
    return {"site_name": settings.SITE_NAME, "dev_login": settings.DEV_LOGIN, "registration_open": settings.REGISTRATION_OPEN}


def site_url():
    """Where the app is reached, for links in emails: SITE_URL, else the first trusted origin."""
    return (settings.SITE_URL or next(iter(settings.CSRF_TRUSTED_ORIGINS), "")).rstrip("/")
