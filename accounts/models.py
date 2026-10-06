import secrets

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
    # Public lists are at /p/<public_token>/..., readable by anyone with the link, signed in or not.
    public_token = models.CharField(max_length=32, unique=True, default=new_token)
    movies_public = models.BooleanField("Movies are public", default=False)
    shows_public = models.BooleanField("TV shows are public", default=False)

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

    def is_public(self, kind):
        return self.movies_public if kind == "movie" else self.shows_public


class LoginCodeRequest(models.Model):
    """One login code requested for an email address (known or not), to limit how many are sent."""

    email = models.EmailField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} at {self.created_at:%Y-%m-%d %H:%M}"
