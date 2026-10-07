import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


def new_token():
    return secrets.token_urlsafe(12)


class UserManager(BaseUserManager):
    """Users are identified by email and never have a usable password."""

    def create_user(self, email, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra_fields)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        return self.create_user(email, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    # Whoever opens /join/<invite_token>/ becomes this user's friend; a new token stops old links working.
    invite_token = models.CharField(max_length=32, unique=True, default=new_token)
    # Opening the app after a while away is a visit; friends' titles since the visit before are new.
    last_visit_at = models.DateTimeField(null=True, blank=True)
    news_since = models.DateTimeField(null=True, blank=True)
    # Where to watch: the country whose streaming services are shown, and the services they have.
    watch_region = models.CharField("country", max_length=2, blank=True, help_text="Empty: the app's default.")
    services = models.JSONField("my streaming services", default=list, blank=True)
    # Other ways you watch that TMDB doesn't list, e.g. "My DVDs" or "Cinema". Added from a title's page or
    # in Manage; removing one there removes it from all titles.
    own_services = models.JSONField("my other services", default=list, blank=True)
    # AI costs money: everyone can import lists straight away, with AI once an admin approves them.
    ai_approved = models.BooleanField("AI approved", default=False)
    ai_daily_limit = models.DecimalField(
        "AI limit per day (USD)", max_digits=6, decimal_places=2, null=True, blank=True,
        help_text="Empty: the app's default. 0: no AI.",
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.name or self.email

    def get_full_name(self):
        return self.name or self.email

    def get_short_name(self):
        return self.name.split(" ")[0] if self.name else self.email.split("@")[0]

    @property
    def all_services(self):
        """The streaming services you have and your other services, in that order."""
        seen = {s.lower() for s in self.services}
        return self.services + [s for s in self.own_services if s.lower() not in seen]

    @property
    def region(self):
        return self.watch_region or settings.WATCH_REGION



class LoginCodeRequest(models.Model):
    """One login code requested for an email address (known or not), to limit how many are sent."""

    email = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} at {self.created_at:%Y-%m-%d %H:%M}"
